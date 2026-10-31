"""
RAG 私有知识库 Web 服务

功能：
    - 上传 TXT / Markdown / PDF / Word 资料，解析、切片、向量化并写入 Milvus
    - 列出 / 删除已入库资料
    - 基于知识库检索 + 调用大模型回答，并返回引用来源

启动：
    .venv\\Scripts\\python.exe app.py
然后浏览器打开 http://127.0.0.1:8000
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import queue
import re
import secrets
import shutil
import sqlite3
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import jieba
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session as flask_session,
)
from pymilvus import DataType
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from rag_demo import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    DOCS_DIR,
    MILVUS_URI,
    TOP_K,
    Embedder,
    ask_llm,
    build_prompt,
    chunk_text,
    get_client,
)

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DOCS_DIR.mkdir(parents=True, exist_ok=True)

# Web 端使用独立集合，避免与命令行版 rag_demo 的 schema 冲突
COLLECTION = os.getenv("WEB_COLLECTION", "rag_docs")
PUBLIC_DEPARTMENT = "公共"
SECRET_FILE = BASE_DIR / "data" / ".secret_key"
UPLOAD_PENDING_DIR = BASE_DIR / "data" / "uploads_pending"
OCR_MIN_CHARS = 20
UPLOAD_JOBS: dict[str, dict] = {}
UPLOAD_JOBS_LOCK = threading.Lock()
UPLOAD_QUEUE = queue.Queue()

SUPPORTED_EXTS = {".txt", ".md", ".markdown", ".pdf", ".docx"}
MAX_FILE_BYTES = 20 * 1024 * 1024
INSERT_BATCH = 200
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 检索结果最高分低于该阈值时，视为“知识库没有相关内容”。
# BGE 中文模型下：相关资料通常 0.45~0.8，无关内容通常 0.15~0.3。
SCORE_THRESHOLD = float(os.getenv("RAG_SCORE_THRESHOLD", "0.35"))
# 知识库没有相关内容时是否让千问用通用知识回答；设为 false 则只提示“无相关资料”。
FALLBACK_TO_LLM = os.getenv("RAG_FALLBACK_TO_LLM", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# 混合检索 + 重排
HYBRID_DENSE_TOP_N = int(os.getenv("HYBRID_DENSE_TOP_N", "15"))
HYBRID_BM25_TOP_N = int(os.getenv("HYBRID_BM25_TOP_N", "15"))
RRF_K = int(os.getenv("RRF_K", "60"))
RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", "8"))
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
RERANK_MODEL_PATH = Path(
    os.getenv(
        "RERANK_MODEL_PATH",
        str(BASE_DIR / "models" / "bge-reranker-base"),
    )
)
KEYWORD_COVERAGE_THRESHOLD = float(os.getenv("KEYWORD_COVERAGE_THRESHOLD", "0.5"))

# 登录失败限流（内存中按用户名计数，重启后清零）
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "600"))
LOGIN_LOCK_SECONDS = int(os.getenv("LOGIN_LOCK_SECONDS", "600"))
LOGIN_ATTEMPTS: dict[str, dict] = {}
LOGIN_LOCK = threading.Lock()

jieba.setLogLevel(logging.WARNING)

STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "而", "被", "把", "对",
    "中", "为", "上", "下", "里", "就", "都", "也", "很", "有", "吗", "呢",
    "吧", "啊", "请问", "什么", "怎么", "为什么", "如何", "哪些", "哪个",
    "哪里", "一个", "一下", "可以", "需要", "以及", "我们", "你们", "他们",
    "它", "这", "那", "请", "帮", "我", "你", "他", "她", "the", "a", "an",
    "of", "to", "and", "or", "is", "are", "what", "how", "why", "which",
}
TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


@lru_cache(maxsize=50000)
def _tokenize_cached(text: str) -> tuple[str, ...]:
    cleaned = re.sub(r"\s+", " ", (text or "").lower())
    tokens = []
    for token in jieba.lcut_for_search(cleaned):
        token = token.strip()
        if not token or token in STOPWORDS:
            continue
        if not TOKEN_RE.search(token):
            continue
        if len(token) == 1 and token.isascii():
            continue
        tokens.append(token)
    return tuple(tokens)


def tokenize_text(text: str) -> list[str]:
    return list(_tokenize_cached(text))


def keyword_coverage(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens:
        return 0.0
    return len(set(query_tokens) & set(doc_tokens)) / len(set(query_tokens))


def login_retry_after(username: str) -> int:
    """返回还需要等待的秒数；0 表示允许登录。"""
    key = (username or "").strip().lower()
    now = time.time()
    with LOGIN_LOCK:
        record = LOGIN_ATTEMPTS.get(key)
        if not record:
            return 0
        locked_until = record.get("locked_until", 0)
        if locked_until > now:
            return int(locked_until - now) + 1
        if now - record.get("first_failure", now) > LOGIN_WINDOW_SECONDS:
            LOGIN_ATTEMPTS.pop(key, None)
        return 0


def record_login_failure(username: str) -> None:
    key = (username or "").strip().lower()
    now = time.time()
    with LOGIN_LOCK:
        record = LOGIN_ATTEMPTS.setdefault(
            key, {"count": 0, "first_failure": now}
        )
        if now - record.get("first_failure", now) > LOGIN_WINDOW_SECONDS:
            record.clear()
            record.update({"count": 0, "first_failure": now})
        record["count"] = record.get("count", 0) + 1
        if record["count"] >= LOGIN_MAX_FAILURES:
            record["locked_until"] = now + LOGIN_LOCK_SECONDS


def clear_login_failures(username: str) -> None:
    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.pop((username or "").strip().lower(), None)


# ============================================================
# 文件解析
# ============================================================
def sanitize_filename(raw: str) -> str:
    """保留中文，去掉路径和 Windows 非法字符，防止目录穿越。"""
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise ValueError("文件名无效")
    if len(name) > 150:
        stem, suffix = os.path.splitext(name)
        name = stem[: 150 - len(suffix)] + suffix
    return name


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()


def _get_ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise ValueError(
                "扫描版 PDF 需要 OCR，请安装 rapidocr_onnxruntime 和 pypdfium2"
            ) from exc
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _ocr_pil_image(image) -> str:
    """对单页图片做 OCR，返回识别出的文本。"""
    import numpy as np

    with _OCR_LOCK:
        result, _ = _get_ocr_engine()(np.array(image.convert("RGB")))
    if not result:
        return ""
    return "\n".join(
        item[1] for item in result if len(item) > 1 and item[1]
    )


def _extract_pdf_text(data: bytes, progress_cb=None) -> str:
    """优先读取 PDF 文本层；文本过少的页面自动渲染成图片做 OCR。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("缺少 pypdf，请先安装依赖") from exc

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF 已加密，无法读取")

    total = len(reader.pages)
    pdfium_doc = None
    pages = []
    try:
        for index, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if len(page_text.strip()) < OCR_MIN_CHARS:
                try:
                    import pypdfium2 as pdfium
                except ImportError as exc:
                    raise ValueError(
                        "扫描版 PDF 需要 OCR，请安装 pypdfium2 和 rapidocr_onnxruntime"
                    ) from exc
                if pdfium_doc is None:
                    pdfium_doc = pdfium.PdfDocument(data)
                image = pdfium_doc[index].render(scale=2.0).to_pil()
                ocr_text = _ocr_pil_image(image)
                if ocr_text:
                    page_text = (page_text + "\n" + ocr_text).strip()
            pages.append(page_text)
            if progress_cb:
                progress_cb(
                    f"解析 PDF {index + 1}/{total} 页",
                    int((index + 1) / max(total, 1) * 100),
                )
    finally:
        if pdfium_doc is not None:
            pdfium_doc.close()
    return "\n".join(pages)


def extract_text(filename: str, data: bytes, progress_cb=None) -> str:
    """根据扩展名把上传文件解析成纯文本；扫描版 PDF 自动 OCR。"""
    ext = Path(filename).suffix.lower()

    if ext in {".txt", ".md", ".markdown"}:
        text = _decode_text(data)
        if progress_cb:
            progress_cb("读取文本", 100)
        return text

    if ext == ".pdf":
        return _extract_pdf_text(data, progress_cb)

    if ext == ".docx":
        try:
            import docx
        except ImportError as exc:
            raise ValueError("缺少 python-docx，请先安装依赖") from exc
        document = docx.Document(io.BytesIO(data))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        if progress_cb:
            progress_cb("读取 Word 文档", 100)
        return "\n".join(parts)

    if ext == ".doc":
        raise ValueError("暂不支持旧版 .doc，请另存为 .docx 后上传")

    raise ValueError(f"不支持的文件类型：{ext or '未知'}")


def build_general_prompt(question: str) -> str:
    """知识库没有相关内容时的通用问答 Prompt（不注入检索资料）。"""
    return (
        "你是一个乐于助人的 AI 助手。用户的问题在企业知识库中没有检索到相关资料，"
        "请直接基于你的通用知识回答；如果不确定或涉及实时信息，请明确说明。\n\n"
        f"问题：{question}\n\n"
        "回答："
    )


NO_ANSWER_MARKERS = ("资料中没有提到", "资料未提及", "资料中没有相关", "无法从资料中")


def looks_like_no_answer(answer: str) -> bool:
    """识别“资料里没有答案”这类拒答，用于触发通用回答兜底。"""
    return any(marker in answer for marker in NO_ANSWER_MARKERS)


# ============================================================
# 会话历史（SQLite）
# ============================================================
DB_PATH = BASE_DIR / "data" / "app.db"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _make_title(text: str, limit: int = 30) -> str:
    title = re.sub(r"\s+", " ", text).strip()
    return title[:limit] + ("…" if len(title) > limit else "")


def sanitize_department(raw: str) -> str:
    department = (raw or "").strip() or PUBLIC_DEPARTMENT
    if len(department) > 50:
        raise ValueError("部门名称过长")
    department = INVALID_FILENAME_CHARS.sub("_", department).strip(" .")
    if not department or department in {".", ".."}:
        raise ValueError("部门名称无效")
    return department


class AppStore:
    """SQLite 持久化：用户、文档元数据、会话消息。每个操作独立连接。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    department TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    department TEXT NOT NULL,
                    uploaded_by INTEGER,
                    chunks INTEGER NOT NULL DEFAULT 0,
                    size INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source, department)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    action TEXT NOT NULL,
                    detail TEXT,
                    ip TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_created
                    ON audit_logs(created_at);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    owner_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    sources TEXT,
                    prompt TEXT,
                    mode TEXT,
                    notice TEXT,
                    best_score REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_documents_department
                    ON documents(department, updated_at);
                """
            )

            # 旧库迁移：补 owner_id 字段（必须在建索引之前）
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "owner_id" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN owner_id INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_owner "
                "ON sessions(owner_id, updated_at)"
            )

            # 首次启动创建账号
            user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            if user_count == 0:
                now = _now()
                seeds = [
                    (
                        os.getenv("ADMIN_USERNAME", "admin"),
                        os.getenv("ADMIN_PASSWORD") or "admin123",
                        os.getenv("ADMIN_DEPARTMENT", "管理部"),
                        "admin",
                    )
                ]
                seed_demo = os.getenv("SEED_DEMO_USERS", "true").strip().lower() not in {
                    "0",
                    "false",
                    "no",
                    "off",
                }
                if seed_demo:
                    demo_password = os.getenv("DEMO_PASSWORD") or "demo123"
                    seeds.extend(
                        [
                            ("alice", demo_password, "研发部", "user"),
                            ("bob", demo_password, "财务部", "user"),
                        ]
                    )
                for username, password, department, role in seeds:
                    conn.execute(
                        """
                        INSERT INTO users(username, password_hash, department, role, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            username,
                            generate_password_hash(password),
                            sanitize_department(department),
                            role,
                            now,
                        ),
                    )

            # 旧的匿名会话统一归管理员，避免 owner_id 为空
            admin = conn.execute(
                "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
            ).fetchone()
            if admin:
                conn.execute(
                    "UPDATE sessions SET owner_id = ? WHERE owner_id IS NULL",
                    (admin["id"],),
                )

    # ---------- 用户 ----------
    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def authenticate(self, username: str, password: str) -> dict | None:
        user = self.get_user_by_username((username or "").strip())
        if user and check_password_hash(user["password_hash"], password or ""):
            return user
        return None

    def list_users(self) -> list[dict]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT id, username, department, role, created_at FROM users ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_user(
        self, username: str, password: str, department: str, role: str = "user"
    ) -> dict:
        username = (username or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-一-龥]{2,32}", username):
            raise ValueError("用户名需为 2-32 位字母、数字、下划线或中文")
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        if role not in {"admin", "user"}:
            raise ValueError("角色无效")
        department = sanitize_department(department)
        now = _now()
        try:
            with self._lock, self._db() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO users(username, password_hash, department, role, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (username, generate_password_hash(password), department, role, now),
                )
                user_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已存在") from exc
        return {
            "id": user_id,
            "username": username,
            "department": department,
            "role": role,
            "created_at": now,
        }

    def delete_user(self, user_id: int) -> bool:
        with self._lock, self._db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user is None:
                return False
            if user["role"] == "admin":
                admin_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE role = 'admin'"
                ).fetchone()["c"]
                if admin_count <= 1:
                    raise ValueError("不能删除最后一个管理员")
            conn.execute(
                "DELETE FROM messages WHERE session_id IN "
                "(SELECT id FROM sessions WHERE owner_id = ?)",
                (user_id,),
            )
            conn.execute("DELETE FROM sessions WHERE owner_id = ?", (user_id,))
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return True

    def change_password(self, user_id: int, password: str) -> None:
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(password), user_id),
            )

    def list_departments(self) -> list[str]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT department FROM users UNION SELECT department FROM documents"
            ).fetchall()
        departments = {row["department"] for row in rows if row["department"]}
        departments.add(PUBLIC_DEPARTMENT)
        return sorted(departments)

    # ---------- 文档元数据 ----------
    def list_documents(self, user: dict) -> list[dict]:
        with self._lock, self._db() as conn:
            if user["role"] == "admin":
                rows = conn.execute(
                    """
                    SELECT d.*, u.username AS uploaded_by_name
                    FROM documents d LEFT JOIN users u ON u.id = d.uploaded_by
                    ORDER BY d.updated_at DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT d.*, u.username AS uploaded_by_name
                    FROM documents d LEFT JOIN users u ON u.id = d.uploaded_by
                    WHERE d.department IN (?, ?)
                    ORDER BY d.updated_at DESC
                    """,
                    (PUBLIC_DEPARTMENT, user["department"]),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_document(self, doc_id: str) -> dict | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                """
                SELECT d.*, u.username AS uploaded_by_name
                FROM documents d LEFT JOIN users u ON u.id = d.uploaded_by
                WHERE d.id = ?
                """,
                (doc_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_document(self, source: str, department: str) -> dict | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE source = ? AND department = ?",
                (source, department),
            ).fetchone()
        return dict(row) if row else None

    def upsert_document(
        self,
        doc_id: str,
        source: str,
        department: str,
        uploaded_by: int,
        chunks: int,
        size: int | None,
    ) -> None:
        now = _now()
        with self._lock, self._db() as conn:
            conn.execute(
                "DELETE FROM documents WHERE source = ? AND department = ?",
                (source, department),
            )
            conn.execute(
                """
                INSERT INTO documents
                    (id, source, department, uploaded_by, chunks, size, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (doc_id, source, department, uploaded_by, chunks, size, now, now),
            )

    def delete_document_row(self, doc_id: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    # ---------- 迁移标记 ----------
    def get_meta(self, key: str) -> str | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---------- 审计日志 ----------
    def log_audit(
        self,
        user_id: int | None,
        username: str,
        action: str,
        detail: str = "",
        ip: str = "",
    ) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs(user_id, username, action, detail, ip, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, action, detail, ip, _now()),
            )

    def list_audit(self, limit: int = 100) -> list[dict]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 会话 ----------
    def create_session(self, owner_id: int, title: str = "新对话") -> dict:
        session_id = uuid.uuid4().hex
        now = _now()
        title = title or "新对话"
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO sessions(id, title, created_at, updated_at, owner_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, title, now, now, owner_id),
            )
        return {
            "id": session_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }

    def session_exists(self, session_id: str, owner_id: int | None = None) -> bool:
        with self._lock, self._db() as conn:
            if owner_id is None:
                row = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? AND owner_id = ?",
                    (session_id, owner_id),
                ).fetchone()
        return row is not None

    def get_session_meta(
        self, session_id: str, owner_id: int | None = None
    ) -> dict | None:
        with self._lock, self._db() as conn:
            if owner_id is None:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE id = ? AND owner_id = ?",
                    (session_id, owner_id),
                ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, owner_id: int) -> list[dict]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.title, s.created_at, s.updated_at,
                       COUNT(m.id) AS message_count
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                WHERE s.owner_id = ?
                GROUP BY s.id
                ORDER BY s.updated_at DESC
                """,
                (owner_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: str, owner_id: int) -> dict | None:
        with self._lock, self._db() as conn:
            session = conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND owner_id = ?",
                (session_id, owner_id),
            ).fetchone()
            if session is None:
                return None
            rows = conn.execute(
                """
                SELECT role, content, sources, prompt, mode, notice, best_score, created_at
                FROM messages WHERE session_id = ? ORDER BY id
                """,
                (session_id,),
            ).fetchall()

        messages = []
        for row in rows:
            message = dict(row)
            try:
                message["sources"] = (
                    json.loads(message["sources"]) if message["sources"] else []
                )
            except (TypeError, ValueError):
                message["sources"] = []
            messages.append(message)
        return {"session": dict(session), "messages": messages}

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        sources: list | None = None,
        prompt: str | None = None,
        mode: str | None = None,
        notice: str | None = None,
        best_score: float | None = None,
    ) -> None:
        now = _now()
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (session_id, role, content, sources, prompt, mode, notice, best_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    json.dumps(sources, ensure_ascii=False) if sources else None,
                    prompt,
                    mode,
                    notice,
                    best_score,
                    now,
                ),
            )
            session = conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session and role == "user" and session["title"] in {"", "新对话", None}:
                conn.execute(
                    "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                    (_make_title(content), now, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )

    def delete_session(self, session_id: str, owner_id: int) -> bool:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND owner_id = ?",
                (session_id, owner_id),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return True


store = AppStore(DB_PATH)


# ============================================================
# Milvus 服务层
# ============================================================
class RagService:
    """封装上传入库、资料列表、删除和问答。"""

    def __init__(self) -> None:
        self._client = None
        self._embedder = None
        self._reranker = None
        self._reranker_failed = False
        self._lock = threading.RLock()
        self._rerank_lock = threading.Lock()

    # ---------- 懒加载 ----------
    def _get_client(self):
        if self._client is None:
            try:
                self._client = get_client()
            except SystemExit as exc:
                raise RuntimeError(str(exc)) from None
        return self._client

    def _get_embedder(self) -> Embedder:
        if self._embedder is None:
            try:
                self._embedder = Embedder()
            except SystemExit as exc:
                raise RuntimeError(str(exc)) from None
        return self._embedder

    def _get_reranker(self):
        if not RERANK_ENABLED or self._reranker_failed:
            return None
        if self._reranker is None:
            model_file = RERANK_MODEL_PATH / "onnx" / "model.onnx"
            if not model_file.is_file():
                print(
                    f"[warn] 未找到 reranker 模型：{model_file}，将只使用混合检索"
                )
                self._reranker_failed = True
                return None
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                self._reranker = TextCrossEncoder(
                    model_name="BAAI/bge-reranker-base",
                    specific_model_path=str(RERANK_MODEL_PATH),
                )
                print(f"[rerank] 已加载 {RERANK_MODEL_PATH}")
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._reranker_failed = True
                return None
        return self._reranker

    def _rerank(self, query: str, candidates: list[dict]) -> list[float] | None:
        reranker = self._get_reranker()
        if reranker is None or not candidates:
            return None
        try:
            with self._rerank_lock:
                return [
                    float(score)
                    for score in reranker.rerank(
                        query, [candidate["text"] for candidate in candidates]
                    )
                ]
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._reranker_failed = True
            return None

    def _ensure_collection(self, dim: int) -> None:
        client = self._get_client()
        needs_recreate = False
        if client.has_collection(COLLECTION):
            description = client.describe_collection(COLLECTION)
            fields = {field["name"] for field in description.get("fields", [])}
            needs_recreate = not {"department", "doc_id"}.issubset(fields)
        if needs_recreate:
            client.drop_collection(COLLECTION)
            # 集合被重建时，允许重新从文件迁移
            store.set_meta("legacy_migrated", "0")

        if not client.has_collection(COLLECTION):
            schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
            schema.add_field(field_name="id", datatype=DataType.INT64, is_primary=True)
            schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=dim)
            schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=4096)
            schema.add_field(field_name="source", datatype=DataType.VARCHAR, max_length=256)
            schema.add_field(field_name="department", datatype=DataType.VARCHAR, max_length=64)
            schema.add_field(field_name="doc_id", datatype=DataType.VARCHAR, max_length=64)

            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="vector",
                index_type="HNSW",
                metric_type="COSINE",
                params={"M": 16, "efConstruction": 200},
            )
            client.create_collection(
                collection_name=COLLECTION,
                schema=schema,
                index_params=index_params,
            )
            print(f"[建集合] {COLLECTION}（dim={dim}，带 department/doc_id 字段）")
        client.load_collection(COLLECTION)

    # ---------- 写入 ----------
    def ingest(
        self,
        source: str,
        text: str,
        department: str,
        doc_id: str,
        progress_cb=None,
    ) -> int:
        chunks = chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)
        if not chunks:
            raise ValueError("文档解析后没有有效文本")
        total = len(chunks)

        def report(stage: str, percent: int) -> None:
            if progress_cb:
                progress_cb(stage, max(0, min(100, percent)))

        with self._lock:
            report("文本切片", 5)
            embedder = self._get_embedder()
            vectors = []
            embed_batch = 32
            for start in range(0, total, embed_batch):
                batch = chunks[start:start + embed_batch]
                vectors.extend(embedder.embed(batch))
                done = min(start + embed_batch, total)
                report(f"向量化 {done}/{total}", 5 + int(60 * done / total))

            client = self._get_client()
            self._ensure_collection(len(vectors[0]))

            # 同名文件 + 同部门重复上传：先删旧切片，再写新切片
            report("清理旧切片", 68)
            client.delete(
                collection_name=COLLECTION,
                filter=(
                    f"source == {json.dumps(source, ensure_ascii=False)} "
                    f"and department == {json.dumps(department, ensure_ascii=False)}"
                ),
            )

            rows = [
                {
                    "vector": vec,
                    "text": chunk,
                    "source": source,
                    "department": department,
                    "doc_id": doc_id,
                }
                for chunk, vec in zip(chunks, vectors)
            ]
            for start in range(0, len(rows), INSERT_BATCH):
                client.insert(collection_name=COLLECTION, data=rows[start:start + INSERT_BATCH])
                done = min(start + INSERT_BATCH, len(rows))
                report(
                    f"写入 Milvus {done}/{len(rows)}",
                    70 + int(25 * done / len(rows)),
                )
            client.flush(COLLECTION)
            report("写入完成", 100)
        return len(chunks)

    def delete_vectors(self, doc_id: str) -> None:
        with self._lock:
            client = self._get_client()
            if client.has_collection(COLLECTION):
                client.delete(
                    collection_name=COLLECTION,
                    filter=f"doc_id == {json.dumps(doc_id)}",
                )
                client.flush(COLLECTION)

    # ---------- RAG 问答 ----------
    def answer(self, question: str, user: dict) -> dict:
        dense_hits = []
        all_rows = []
        with self._lock:
            client = self._get_client()
            if client.has_collection(COLLECTION):
                client.load_collection(COLLECTION)
                embedder = self._get_embedder()
                query_vector = embedder.embed_query(question)

                filter_expr = None
                if user["role"] != "admin":
                    # 普通用户只能检索「公共 + 本部门」资料
                    visible = [PUBLIC_DEPARTMENT, user["department"]]
                    filter_expr = (
                        f"department in {json.dumps(visible, ensure_ascii=False)}"
                    )

                search_kwargs = {}
                if filter_expr:
                    search_kwargs["filter"] = filter_expr
                dense_hits = client.search(
                    collection_name=COLLECTION,
                    data=[query_vector],
                    limit=HYBRID_DENSE_TOP_N,
                    output_fields=["id", "text", "source", "department", "doc_id"],
                    search_params={"metric_type": "COSINE", "params": {"ef": 64}},
                    **search_kwargs,
                )[0]
                all_rows = client.query(
                    collection_name=COLLECTION,
                    filter=filter_expr or "id >= 0",
                    output_fields=["id", "text", "source", "department", "doc_id"],
                    limit=16384,
                )

        dense_best = max(
            (float(hit["distance"]) for hit in dense_hits), default=0.0
        )
        query_tokens = tokenize_text(question)

        # ---------- BM25 关键词召回 ----------
        bm25_ranked: list[dict] = []
        coverage = 0.0
        try:
            if all_rows and query_tokens:
                from rank_bm25 import BM25Okapi

                corpus_tokens = [
                    tokenize_text(row.get("text", "")) or ["_"]
                    for row in all_rows
                ]
                bm25 = BM25Okapi(corpus_tokens)
                raw_scores = bm25.get_scores(query_tokens)
                scores = [
                    float(score) if math.isfinite(float(score)) else -1.0
                    for score in raw_scores
                ]
                order = sorted(
                    range(len(scores)), key=lambda idx: scores[idx], reverse=True
                )
                for rank, idx in enumerate(order[:HYBRID_BM25_TOP_N], 1):
                    if scores[idx] <= 0:
                        break
                    row = all_rows[idx]
                    bm25_ranked.append(
                        {
                            "id": str(row.get("id")),
                            "text": row.get("text", ""),
                            "source": row.get("source", ""),
                            "department": row.get("department", ""),
                            "doc_id": row.get("doc_id", ""),
                            "bm25": float(scores[idx]),
                            "bm25Rank": rank,
                        }
                    )
                if bm25_ranked:
                    coverage = keyword_coverage(
                        query_tokens, corpus_tokens[order[0]]
                    )
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            bm25_ranked = []
            coverage = 0.0

        # 相关性判断：向量分够高，或关键词覆盖率够高
        if not all_rows or (
            dense_best < SCORE_THRESHOLD
            and coverage < KEYWORD_COVERAGE_THRESHOLD
        ):
            prompt = build_general_prompt(question)
            answer = ask_llm(prompt) if FALLBACK_TO_LLM else None
            if not FALLBACK_TO_LLM:
                notice = "知识库中未检索到相关内容。"
            elif answer is None:
                notice = "知识库中未检索到相关内容；当前未配置大模型凭证。"
            else:
                notice = "知识库中未检索到相关内容，以下为千问的通用回答（不是企业资料）。"
            return {
                "answer": answer,
                "sources": [],
                "prompt": prompt,
                "mode": "general",
                "notice": notice,
                "bestScore": round(dense_best, 4),
                "keywordCoverage": round(coverage, 4),
                "retrievalMode": "none",
            }

        # ---------- RRF 融合 ----------
        candidates: dict[str, dict] = {}

        def add_candidate(item: dict, source_type: str, rank: int) -> None:
            key = item["id"]
            candidate = candidates.setdefault(
                key,
                {
                    "id": key,
                    "text": item.get("text", ""),
                    "source": item.get("source", ""),
                    "department": item.get("department", ""),
                    "doc_id": item.get("doc_id", ""),
                    "cosine": None,
                    "rrf": 0.0,
                    "matched": set(),
                },
            )
            candidate["rrf"] += 1.0 / (RRF_K + rank)
            candidate["matched"].add(source_type)
            if source_type == "dense":
                candidate["cosine"] = float(item.get("cosine") or 0.0)
            else:
                candidate["bm25"] = float(item.get("bm25") or 0.0)

        for rank, hit in enumerate(dense_hits, 1):
            entity = hit.get("entity", {})
            add_candidate(
                {
                    "id": str(hit.get("id")),
                    "text": entity.get("text", ""),
                    "source": entity.get("source", ""),
                    "department": entity.get("department", ""),
                    "doc_id": entity.get("doc_id", ""),
                    "cosine": float(hit.get("distance") or 0.0),
                },
                "dense",
                rank,
            )
        for rank, item in enumerate(bm25_ranked, 1):
            add_candidate(item, "bm25", rank)

        fused = sorted(
            candidates.values(), key=lambda candidate: candidate["rrf"], reverse=True
        )

        # ---------- Cross-Encoder 重排 ----------
        rerank_pool = fused[:RERANK_TOP_N]
        rerank_scores = self._rerank(question, rerank_pool)
        if rerank_scores and len(rerank_scores) == len(rerank_pool):
            for candidate, score in zip(rerank_pool, rerank_scores):
                candidate["rerankScore"] = score
            rerank_pool.sort(
                key=lambda candidate: candidate["rerankScore"], reverse=True
            )
            final_candidates = rerank_pool[:TOP_K]
            retrieval_mode = "hybrid+rerank"
        else:
            final_candidates = rerank_pool[:TOP_K]
            retrieval_mode = "hybrid"

        sources = []
        for candidate in final_candidates:
            matched = candidate.pop("matched", set())
            matched_by = (
                "both"
                if len(matched) == 2
                else (next(iter(matched)) if matched else "dense")
            )
            final_score = candidate.get("rerankScore", candidate["rrf"])
            sources.append(
                {
                    "source": candidate["source"],
                    "department": candidate["department"],
                    "text": candidate["text"],
                    "score": round(float(final_score), 4),
                    "cosine": (
                        round(candidate["cosine"], 4)
                        if candidate.get("cosine") is not None
                        else None
                    ),
                    "rrfScore": round(candidate["rrf"], 4),
                    "rerankScore": (
                        round(candidate["rerankScore"], 4)
                        if candidate.get("rerankScore") is not None
                        else None
                    ),
                    "matchedBy": matched_by,
                }
            )

        prompt_hits = [
            {"entity": {"source": source["source"], "text": source["text"]}}
            for source in sources
        ]
        prompt = build_prompt(question, prompt_hits)
        # 调用大模型可能较慢，放到锁外执行
        answer = ask_llm(prompt)

        # 兜底：检索分数虽然过线，但资料里确实没有答案（模型返回“资料中没有提到”）
        if FALLBACK_TO_LLM and answer and looks_like_no_answer(answer):
            general_prompt = build_general_prompt(question)
            general_answer = ask_llm(general_prompt)
            if general_answer:
                return {
                    "answer": general_answer,
                    "sources": [],
                    "prompt": general_prompt,
                    "mode": "general",
                    "notice": "知识库资料没有覆盖这个问题，以下为千问的通用回答（不是企业资料）。",
                    "bestScore": round(dense_best, 4),
                    "keywordCoverage": round(coverage, 4),
                    "retrievalMode": "general-fallback",
                }

        return {
            "answer": answer,
            "sources": sources,
            "prompt": prompt,
            "mode": "kb",
            "bestScore": round(dense_best, 4),
            "keywordCoverage": round(coverage, 4),
            "retrievalMode": retrieval_mode,
        }

    # ---------- 健康检查 ----------
    def health(self) -> dict:
        info = {
            "ok": False,
            "milvus": False,
            "collectionExists": False,
            "collection": COLLECTION,
            "milvusUri": MILVUS_URI,
            "embeddingBackend": os.getenv("EMBEDDING_BACKEND", ""),
            "embeddingModel": os.getenv("EMBEDDING_MODEL", ""),
            "llmModel": os.getenv("LLM_MODEL", ""),
            "llmConfigured": bool(
                os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
                or os.getenv("LLM_API_KEY", "").strip()
            ),
            "scoreThreshold": SCORE_THRESHOLD,
            "fallbackToLlm": FALLBACK_TO_LLM,
            "retrievalMode": "hybrid" + ("+rerank" if RERANK_ENABLED else ""),
            "rerankEnabled": RERANK_ENABLED,
            "rerankModelReady": (
                RERANK_MODEL_PATH / "onnx" / "model.onnx"
            ).is_file(),
            "denseTopN": HYBRID_DENSE_TOP_N,
            "bm25TopN": HYBRID_BM25_TOP_N,
            "rrfK": RRF_K,
        }
        try:
            client = self._get_client()
            info["milvus"] = True
            info["collectionExists"] = client.has_collection(COLLECTION)
            info["ok"] = True
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)
        return info


service = RagService()


# ============================================================
# 异步上传任务
# ============================================================
def _job_public(job: dict) -> dict:
    return {
        "id": job["id"],
        "filename": job["filename"],
        "department": job["department"],
        "status": job["status"],
        "progress": job["progress"],
        "stage": job["stage"],
        "chunks": job.get("chunks", 0),
        "error": job.get("error"),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


def _update_job(job_id: str, **fields) -> None:
    with UPLOAD_JOBS_LOCK:
        job = UPLOAD_JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        job["updated_at"] = _now()


def _prune_jobs() -> None:
    cutoff = datetime.now() - timedelta(hours=6)
    with UPLOAD_JOBS_LOCK:
        for job_id, job in list(UPLOAD_JOBS.items()):
            if job.get("status") not in {"done", "error"}:
                continue
            try:
                updated = datetime.strptime(job["updated_at"], "%Y-%m-%d %H:%M:%S")
            except (KeyError, ValueError):
                continue
            if updated < cutoff:
                UPLOAD_JOBS.pop(job_id, None)


def process_upload_job(job_id: str) -> None:
    with UPLOAD_JOBS_LOCK:
        job = dict(UPLOAD_JOBS.get(job_id) or {})
    if not job:
        return

    source = job["filename"]
    department = job["department"]
    user_id = job["user_id"]
    temp_path = Path(job["temp_path"])
    doc_id = uuid.uuid4().hex

    try:
        _update_job(job_id, status="processing", progress=2, stage="读取文件")
        data = temp_path.read_bytes()

        def parse_progress(stage: str, percent: int) -> None:
            _update_job(job_id, stage=stage, progress=5 + int(percent * 0.45))

        text = extract_text(source, data, progress_cb=parse_progress)
        if not text.strip():
            raise ValueError("文件解析后没有可提取文本")

        _update_job(job_id, progress=52, stage="文本切片与向量化")

        def ingest_progress(stage: str, percent: int) -> None:
            _update_job(job_id, stage=stage, progress=52 + int(percent * 0.40))

        chunks = service.ingest(
            source, text, department, doc_id, progress_cb=ingest_progress
        )

        _update_job(job_id, progress=94, stage="保存文件")
        target_dir = DOCS_DIR / department
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_path.replace(target_dir / source)
        shutil.rmtree(temp_path.parent, ignore_errors=True)
        store.upsert_document(
            doc_id, source, department, user_id, chunks, len(data)
        )
        _update_job(
            job_id,
            status="done",
            progress=100,
            stage="完成",
            chunks=chunks,
            doc_id=doc_id,
        )
        user = store.get_user_by_id(user_id)
        store.log_audit(
            user_id,
            user["username"] if user else "",
            "upload_done",
            f"{source} -> {department}, chunks={chunks}",
        )
    except Exception as exc:  # noqa: BLE001
        temp_path.unlink(missing_ok=True)
        shutil.rmtree(temp_path.parent, ignore_errors=True)
        _update_job(job_id, status="error", stage="失败", error=str(exc))
        store.log_audit(user_id, "", "upload_failed", f"{source}: {exc}")
        traceback.print_exc()


def upload_worker() -> None:
    while True:
        job_id = UPLOAD_QUEUE.get()
        try:
            process_upload_job(job_id)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            UPLOAD_QUEUE.task_done()


app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_BYTES * 5
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)
app.json.ensure_ascii = False


def _load_secret_key() -> str:
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    if SECRET_FILE.exists():
        key = SECRET_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_hex(32)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_text(key, encoding="utf-8")
    return key


app.secret_key = _load_secret_key()


def public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "department": user["department"],
        "role": user["role"],
    }


def admin_only():
    if g.user is None or g.user["role"] != "admin":
        return jsonify(error="需要管理员权限"), 403
    return None


@app.before_request
def load_current_user():
    g.user = None
    user_id = flask_session.get("user_id")
    if user_id:
        g.user = store.get_user_by_id(user_id)

    if request.endpoint in {"login_page", "api_login", "static", "api_health"}:
        return None
    if request.path.startswith("/static/"):
        return None
    if g.user is None:
        if request.path.startswith("/api/"):
            return jsonify(error="请先登录"), 401
        return redirect("/login")
    return None


# ============================================================
# 路由
# ============================================================
@app.get("/login")
def login_page():
    if g.user:
        return redirect("/")
    return send_from_directory(WEB_DIR, "login.html")


@app.post("/api/login")
def api_login():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    ip = request.remote_addr or ""

    retry_after = login_retry_after(username)
    if retry_after:
        store.log_audit(None, username, "login_locked", f"retry_after={retry_after}", ip)
        return (
            jsonify(
                error=f"登录失败次数过多，请 {retry_after} 秒后重试",
                retry_after=retry_after,
            ),
            429,
        )

    user = store.authenticate(username, password)
    if user is None:
        record_login_failure(username)
        store.log_audit(None, username, "login_failed", "", ip)
        return jsonify(error="用户名或密码错误"), 401

    clear_login_failures(username)
    flask_session.clear()
    flask_session["user_id"] = user["id"]
    flask_session.permanent = True
    store.log_audit(user["id"], user["username"], "login", "", ip)
    return jsonify(user=public_user(user))


@app.post("/api/logout")
def api_logout():
    store.log_audit(
        g.user["id"], g.user["username"], "logout", "", request.remote_addr or ""
    )
    flask_session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    return jsonify(user=public_user(g.user))


@app.post("/api/me/password")
def api_change_password():
    payload = request.get_json(silent=True) or {}
    current = store.get_user_by_id(g.user["id"])
    if current is None or not check_password_hash(
        current["password_hash"], payload.get("old_password") or ""
    ):
        return jsonify(error="原密码错误"), 400
    try:
        store.change_password(g.user["id"], payload.get("new_password") or "")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    store.log_audit(
        g.user["id"],
        g.user["username"],
        "change_password",
        "self",
        request.remote_addr or "",
    )
    return jsonify(ok=True)


@app.get("/api/departments")
def api_departments():
    if g.user["role"] == "admin":
        departments = store.list_departments()
    else:
        departments = sorted({PUBLIC_DEPARTMENT, g.user["department"]})
    return jsonify(departments=departments)


@app.get("/api/users")
def api_users():
    denied = admin_only()
    if denied:
        return denied
    return jsonify(users=store.list_users())


@app.post("/api/users")
def api_create_user():
    denied = admin_only()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    try:
        user = store.create_user(
            payload.get("username", ""),
            payload.get("password", ""),
            payload.get("department", ""),
            payload.get("role", "user"),
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    store.log_audit(
        g.user["id"],
        g.user["username"],
        "create_user",
        f"user={user['username']} department={user['department']} role={user['role']}",
        request.remote_addr or "",
    )
    return jsonify(user=public_user(user)), 201


@app.post("/api/users/<int:user_id>/password")
def api_reset_user_password(user_id: int):
    denied = admin_only()
    if denied:
        return denied
    target = store.get_user_by_id(user_id)
    if target is None:
        return jsonify(error="用户不存在"), 404
    payload = request.get_json(silent=True) or {}
    try:
        store.change_password(user_id, payload.get("new_password") or "")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    store.log_audit(
        g.user["id"],
        g.user["username"],
        "reset_password",
        f"target={target['username']}",
        request.remote_addr or "",
    )
    return jsonify(ok=True)


@app.get("/api/audit")
def api_audit():
    denied = admin_only()
    if denied:
        return denied
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    return jsonify(logs=store.list_audit(limit))


@app.delete("/api/users/<int:user_id>")
def api_delete_user(user_id: int):
    denied = admin_only()
    if denied:
        return denied
    if user_id == g.user["id"]:
        return jsonify(error="不能删除当前登录账号"), 400
    target = store.get_user_by_id(user_id)
    try:
        deleted = store.delete_user(user_id)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not deleted:
        return jsonify(error="用户不存在"), 404
    store.log_audit(
        g.user["id"],
        g.user["username"],
        "delete_user",
        f"target={target['username'] if target else user_id}",
        request.remote_addr or "",
    )
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/api/health")
def api_health():
    return jsonify(service.health())


@app.get("/api/documents")
def api_documents():
    documents = store.list_documents(g.user)
    for document in documents:
        document["canDelete"] = (
            g.user["role"] == "admin"
            or document.get("uploaded_by") == g.user["id"]
        )
        document["ext"] = Path(document["source"]).suffix.lower().lstrip(".")
    return jsonify(documents=documents)


@app.post("/api/documents")
def api_upload():
    files = request.files.getlist("files") or request.files.getlist("file")
    if not files:
        return jsonify(error="没有收到文件"), 400

    # 普通用户只能上传到本部门；管理员可以选择部门
    if g.user["role"] == "admin":
        department = sanitize_department(request.form.get("department", ""))
    else:
        department = g.user["department"]

    results = []
    for storage in files:
        original = storage.filename or "未命名"
        temp_path = None
        try:
            source = sanitize_filename(original)
            ext = Path(source).suffix.lower()
            if ext not in SUPPORTED_EXTS:
                raise ValueError(f"不支持的文件类型：{ext or '未知'}")

            data = storage.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("文件超过 20MB 限制")
            if not data:
                raise ValueError("文件内容为空")

            text = extract_text(source, data)
            target_dir = DOCS_DIR / department
            target_dir.mkdir(parents=True, exist_ok=True)
            temp_path = target_dir / f".upload-{uuid.uuid4().hex}.tmp"
            temp_path.write_bytes(data)

            doc_id = uuid.uuid4().hex
            chunks = service.ingest(source, text, department, doc_id)
            temp_path.replace(target_dir / source)
            store.upsert_document(
                doc_id,
                source,
                department,
                g.user["id"],
                chunks,
                len(data),
            )
            results.append(
                {
                    "id": doc_id,
                    "source": source,
                    "department": department,
                    "chunks": chunks,
                    "ok": True,
                }
            )
        except Exception as exc:  # noqa: BLE001
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            results.append({"source": original, "ok": False, "error": str(exc)})

    return jsonify(results=results)


@app.delete("/api/documents/<doc_id>")
def api_delete_document(doc_id: str):
    document = store.get_document(doc_id)
    if document is None:
        return jsonify(error="资料不存在"), 404
    if (
        g.user["role"] != "admin"
        and document.get("uploaded_by") != g.user["id"]
    ):
        return jsonify(error="无权删除该资料"), 403

    service.delete_vectors(doc_id)
    for path in (
        DOCS_DIR / document["department"] / document["source"],
        DOCS_DIR / document["source"],
    ):
        if path.is_file():
            path.unlink()
            break
    store.delete_document_row(doc_id)
    store.log_audit(
        g.user["id"],
        g.user["username"],
        "delete_document",
        f"{document['source']} ({document['department']})",
        request.remote_addr or "",
    )
    return jsonify(ok=True)


@app.post("/api/upload-jobs")
def api_create_upload_jobs():
    files = request.files.getlist("files") or request.files.getlist("file")
    if not files:
        return jsonify(error="没有收到文件"), 400

    if g.user["role"] == "admin":
        department = sanitize_department(request.form.get("department", ""))
    else:
        department = g.user["department"]

    _prune_jobs()
    created = []
    for storage in files:
        original = storage.filename or "未命名"
        try:
            source = sanitize_filename(original)
            ext = Path(source).suffix.lower()
            if ext not in SUPPORTED_EXTS:
                raise ValueError(f"不支持的文件类型：{ext or '未知'}")

            data = storage.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("文件超过 20MB 限制")
            if not data:
                raise ValueError("文件内容为空")

            job_id = uuid.uuid4().hex
            job_dir = UPLOAD_PENDING_DIR / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            temp_path = job_dir / source
            temp_path.write_bytes(data)

            now = _now()
            job = {
                "id": job_id,
                "filename": source,
                "department": department,
                "user_id": g.user["id"],
                "status": "queued",
                "progress": 0,
                "stage": "排队中",
                "chunks": 0,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "temp_path": str(temp_path),
            }
            with UPLOAD_JOBS_LOCK:
                UPLOAD_JOBS[job_id] = job
            UPLOAD_QUEUE.put(job_id)
            store.log_audit(
                g.user["id"],
                g.user["username"],
                "upload",
                f"{source} -> {department}",
                request.remote_addr or "",
            )
            created.append(_job_public(job))
        except Exception as exc:  # noqa: BLE001
            created.append(
                {
                    "id": None,
                    "filename": original,
                    "department": department,
                    "status": "error",
                    "progress": 0,
                    "stage": "失败",
                    "chunks": 0,
                    "error": str(exc),
                }
            )

    return jsonify(jobs=created), 202


@app.get("/api/upload-jobs")
def api_upload_jobs():
    with UPLOAD_JOBS_LOCK:
        jobs = [
            _job_public(dict(job))
            for job in UPLOAD_JOBS.values()
            if job["user_id"] == g.user["id"] or g.user["role"] == "admin"
        ]
    jobs.sort(key=lambda item: item["created_at"], reverse=True)
    return jsonify(jobs=jobs[:50])


@app.get("/api/upload-jobs/<job_id>")
def api_upload_job(job_id: str):
    with UPLOAD_JOBS_LOCK:
        job = UPLOAD_JOBS.get(job_id)
        if job is None or (
            job["user_id"] != g.user["id"] and g.user["role"] != "admin"
        ):
            return jsonify(error="任务不存在"), 404
        return jsonify(job=_job_public(dict(job)))


@app.get("/api/sessions")
def api_sessions():
    return jsonify(sessions=store.list_sessions(g.user["id"]))


@app.get("/api/sessions/<session_id>")
def api_session(session_id: str):
    data = store.get_session(session_id, g.user["id"])
    if data is None:
        return jsonify(error="会话不存在"), 404
    return jsonify(data)


@app.delete("/api/sessions/<session_id>")
def api_delete_session(session_id: str):
    if not store.delete_session(session_id, g.user["id"]):
        return jsonify(error="会话不存在"), 404
    return jsonify(ok=True)


@app.post("/api/chat")
def api_chat():
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify(error="问题不能为空"), 400

    session_id = (payload.get("session_id") or "").strip()
    if not session_id or not store.session_exists(session_id, g.user["id"]):
        session_id = store.create_session(g.user["id"])["id"]
    store.append_message(session_id, "user", question)

    try:
        result = service.answer(question, g.user)
    except Exception as exc:  # noqa: BLE001
        store.append_message(
            session_id, "assistant", f"抱歉，出错了：{exc}", mode="error"
        )
        traceback.print_exc()
        return (
            jsonify(error=f"{type(exc).__name__}: {exc}", session_id=session_id),
            500,
        )

    store.append_message(
        session_id,
        "assistant",
        result.get("answer") or "",
        sources=result.get("sources"),
        prompt=result.get("prompt"),
        mode=result.get("mode"),
        notice=result.get("notice"),
        best_score=result.get("bestScore"),
    )
    store.log_audit(
        g.user["id"],
        g.user["username"],
        "chat",
        (
            f"session={session_id} mode={result.get('mode')} "
            f"retrieval={result.get('retrievalMode')} "
            f"sources={len(result.get('sources') or [])}"
        ),
        request.remote_addr or "",
    )
    result["session_id"] = session_id
    result["session"] = store.get_session_meta(session_id, g.user["id"])
    return jsonify(result)


@app.errorhandler(Exception)
def handle_error(exc):
    if isinstance(exc, HTTPException):
        return jsonify(error=exc.description), exc.code
    traceback.print_exc()
    return jsonify(error=f"{type(exc).__name__}: {exc}"), 500


def migrate_legacy_documents() -> None:
    """把 data/docs 里已有的文件迁入 Web 集合（默认归到「公共」部门）。"""
    if store.get_meta("legacy_migrated") == "1":
        return
    admin = store.get_user_by_username(os.getenv("ADMIN_USERNAME", "admin"))
    if admin is None:
        admin = next(
            (user for user in store.list_users() if user["role"] == "admin"),
            None,
        )
    if admin is None:
        return

    paths = sorted(
        path
        for path in DOCS_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS
    )
    for path in paths:
        if path.parent == DOCS_DIR:
            department = PUBLIC_DEPARTMENT
            target_dir = DOCS_DIR / department
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            if target.exists():
                path = target
            else:
                shutil.move(str(path), str(target))
                path = target
        else:
            department = sanitize_department(path.parent.name)

        if store.find_document(path.name, department):
            continue

        text = extract_text(path.name, path.read_bytes())
        doc_id = uuid.uuid4().hex
        chunks = service.ingest(path.name, text, department, doc_id)
        store.upsert_document(
            doc_id,
            path.name,
            department,
            admin["id"],
            chunks,
            path.stat().st_size,
        )
        print(f"[迁移] {department}/{path.name} -> {chunks} 个切片")

    store.set_meta("legacy_migrated", "1")
    print("[迁移] 现有资料已迁入 Web 知识库")


try:
    migrate_legacy_documents()
except Exception:  # noqa: BLE001
    traceback.print_exc()
    print("[warn] 资料迁移未完成，下次启动会重试")

# 清理上次退出时未处理完的临时上传
if UPLOAD_PENDING_DIR.exists():
    shutil.rmtree(UPLOAD_PENDING_DIR, ignore_errors=True)
UPLOAD_PENDING_DIR.mkdir(parents=True, exist_ok=True)

if not any(thread.name == "upload-worker" for thread in threading.enumerate()):
    threading.Thread(target=upload_worker, name="upload-worker", daemon=True).start()


def warmup_services() -> None:
    try:
        tokenize_text("预热检索")
        service._get_embedder()
        service._get_reranker()
        print("[warmup] 向量与重排模型已预热")
    except Exception:  # noqa: BLE001
        traceback.print_exc()


if os.getenv("WARMUP_ON_START", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}:
    threading.Thread(target=warmup_services, name="warmup", daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "8000"))
    print("=" * 56)
    print("  RAG 私有知识库已启动")
    print(f"  本机访问：http://127.0.0.1:{port}")
    print(f"  局域网访问：http://<本机IP>:{port}")
    print(f"  Milvus：{MILVUS_URI}   集合：{COLLECTION}")
    print("=" * 56)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
