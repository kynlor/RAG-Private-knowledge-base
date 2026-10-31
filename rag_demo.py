"""
最小可运行的 RAG 示例（Python + Milvus）

把 RAG 拆成 4 步，每一步对应下面一个函数/类：

    1. 文档切片          chunk_text()
    2. 文本向量化        Embedder
    3. 写入 / 检索向量库  recreate_collection() / client.insert() / client.search()
    4. 拼 Prompt + 调 LLM build_prompt() / ask_llm()

用法：
    python rag_demo.py ingest                 # 建库：切片 -> 向量化 -> 写入 Milvus
    python rag_demo.py ask "你的问题"          # 检索 -> 拼 Prompt -> 调用大模型
    python rag_demo.py ask "你的问题" --raw    # 只检索并打印 Prompt，不调大模型

依赖见 requirements.txt，配置见 .env（从 .env.example 复制）。
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymilvus import DataType, MilvusClient

BASE_DIR = Path(__file__).resolve().parent
# override=True：即使系统/终端里已有同名变量（例如 ANTHROPIC_*），也以项目 .env 为准
load_dotenv(BASE_DIR / ".env", override=True)

DOCS_DIR = BASE_DIR / "data" / "docs"

# ---------- 可配置项（全部可以用 .env 覆盖） ----------
MILVUS_URI = os.getenv("MILVUS_URI", "http://localhost:19530")
MILVUS_TOKEN = os.getenv("MILVUS_TOKEN", "").strip()  # 自建 Milvus 一般留空；Zilliz Cloud 填 API Key
COLLECTION = os.getenv("COLLECTION_NAME", "rag_demo")
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "300"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))
TOP_K = int(os.getenv("TOP_K", "3"))

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
# BGE 中文模型检索时的推荐指令前缀；文档侧不加
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


# ============================================================
# 第 1 步：文档切片
# ============================================================
def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把长文本按字符切成小块，相邻块之间保留 overlap 个字符的重叠。

    简单版：固定窗口滑动。真实项目还会优先按段落/标题/句子边界切，
    避免把一句话从中间截断。overlap 是为了防止关键信息正好落在切口上。
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    step = max(1, chunk_size - overlap)
    chunks: list[str] = []
    for start in range(0, len(text), step):
        chunk = text[start:start + chunk_size].strip()
        if len(chunk) >= 20:  # 丢掉过短的尾巴
            chunks.append(chunk)
        if start + chunk_size >= len(text):
            break
    return chunks


# ============================================================
# 第 2 步：文本 -> 向量
# ============================================================
class Embedder:
    """把文本转成向量。支持三种后端，用 EMBEDDING_BACKEND 切换：

    - fastembed（默认）：本地 ONNX 模型，不需要 API Key，第一次运行会下载模型
    - openai：任何 OpenAI 兼容的 embedding 接口（DashScope、SiliconFlow、OpenAI 等）
    - hash：零依赖后备方案，用字符 n-gram 哈希成向量，只用来跑通流程
    """

    def __init__(self) -> None:
        self.backend = os.getenv("EMBEDDING_BACKEND", "fastembed").strip().lower()
        self.model_name = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
        self._model = None       # fastembed 模型，延迟加载
        self._client = None      # OpenAI 客户端

        if self.backend == "openai":
            from openai import OpenAI

            self._client = OpenAI(
                base_url=os.getenv("EMBEDDING_BASE_URL"),
                api_key=os.getenv("EMBEDDING_API_KEY"),
            )
        elif self.backend not in ("fastembed", "hash"):
            raise SystemExit(
                f"未知的 EMBEDDING_BACKEND：{self.backend}（可选 fastembed / openai / hash）"
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量把文本转成向量。"""
        if self.backend == "hash":
            return [self._hash_vector(t) for t in texts]

        if self.backend == "openai":
            resp = self._client.embeddings.create(model=self.model_name, input=texts)
            return [item.embedding for item in resp.data]

        # fastembed：本地模型
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return [vec.tolist() for vec in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        """问题向量。BGE 模型建议给查询加指令前缀，文档侧不加。"""
        if self.backend == "fastembed" and "bge" in self.model_name.lower():
            text = BGE_QUERY_PREFIX + text
        return self.embed([text])[0]

    @staticmethod
    def _hash_vector(text: str, dim: int = 256) -> list[float]:
        """把字符 1~3-gram 哈希到固定维度。语义能力很弱，但能演示完整链路。"""
        vec = [0.0] * dim
        text = re.sub(r"\s+", "", text.lower())
        for n in (1, 2, 3):
            for i in range(len(text) - n + 1):
                gram = text[i:i + n]
                digest = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16)
                index = digest % dim
                sign = 1.0 if (digest >> 64) & 1 else -1.0
                vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


# ============================================================
# 第 3 步：Milvus 建集合 / 写入 / 检索
# ============================================================
def get_client() -> MilvusClient:
    try:
        return MilvusClient(uri=MILVUS_URI, token=MILVUS_TOKEN)
    except Exception as exc:
        raise SystemExit(
            f"无法连接 Milvus（{MILVUS_URI}）：{exc}\n"
            "请确认 etcd、MinIO、Milvus 容器已启动，且 19530 端口可访问。"
        ) from exc


def recreate_collection(client: MilvusClient, dim: int) -> None:
    """演示用：每次 ingest 都重建集合，避免重复插入。"""
    if client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)
        print(f"[重建] 已删除旧集合 {COLLECTION}")

    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field(field_name="id", datatype=DataType.INT64, is_primary=True)
    schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=4096)
    schema.add_field(field_name="source", datatype=DataType.VARCHAR, max_length=256)

    # 向量字段必须建索引。HNSW 是常用的近似最近邻索引：
    # M 控制图的连接数，efConstruction 控制建索引时的搜索宽度。
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
    print(f"[建集合] {COLLECTION}（dim={dim}, metric=COSINE, index=HNSW）")


def load_documents() -> list[tuple[str, str]]:
    """读取 data/docs 下的 .md / .txt，返回 [(文件名, 全文), ...]。"""
    if not DOCS_DIR.exists():
        return []
    docs: list[tuple[str, str]] = []
    for path in sorted(DOCS_DIR.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
            docs.append((path.name, path.read_text(encoding="utf-8")))
    return docs


def cmd_ingest(_args: argparse.Namespace) -> None:
    # 先连 Milvus，避免白跑一遍向量化（尤其是第一次还要下载模型）
    client = get_client()

    docs = load_documents()
    if not docs:
        raise SystemExit(f"没有找到文档：请把 .md / .txt 文件放进 {DOCS_DIR}")

    embedder = Embedder()
    rows: list[dict] = []

    for source, text in docs:
        chunks = chunk_text(text)
        vectors = embedder.embed(chunks)  # 批量向量化
        rows.extend(
            {"vector": vec, "text": chunk, "source": source}
            for chunk, vec in zip(chunks, vectors)
        )
        print(f"[切片] {source}: {len(text)} 字 -> {len(chunks)} 块")

    if not rows:
        raise SystemExit("所有文档切片后都为空。")

    recreate_collection(client, dim=len(rows[0]["vector"]))
    # 演示数据量小，一次 insert 即可；生产环境要分批，避免超过 gRPC 消息大小限制
    client.insert(collection_name=COLLECTION, data=rows)
    client.flush(COLLECTION)
    print(f"[入库] {len(rows)} 个切片已写入 Milvus")


def cmd_ask(args: argparse.Namespace) -> None:
    client = get_client()
    if not client.has_collection(COLLECTION):
        raise SystemExit("集合不存在。请先运行：python rag_demo.py ingest")

    # 查询前要确保集合已加载到 querynode 内存；重复调用是幂等的
    client.load_collection(COLLECTION)

    embedder = Embedder()
    query_vector = embedder.embed_query(args.question)

    # 向量检索：返回与问题向量最相似的 Top-K 个切片
    hits = client.search(
        collection_name=COLLECTION,
        data=[query_vector],
        limit=TOP_K,
        output_fields=["text", "source"],
        search_params={"metric_type": "COSINE", "params": {"ef": 64}},
    )[0]

    print(f"\n=== 检索结果 Top-{TOP_K}（COSINE，分数越大越相似）===")
    if not hits:
        print("没有检索到内容。")
        return
    for rank, hit in enumerate(hits, 1):
        entity = hit["entity"]
        preview = entity["text"].replace("\n", " ")[:80]
        print(f"[{rank}] score={hit['distance']:.4f}  source={entity['source']}")
        print(f"    {preview}...")

    prompt = build_prompt(args.question, hits)

    if args.raw:
        print("\n=== 拼好的 Prompt（RAG 的“增强”就发生在这里）===")
        print(prompt)
        return

    answer = ask_llm(prompt)
    if answer is None:
        print("\n[提示] 未配置大模型凭证（ANTHROPIC_AUTH_TOKEN 或 LLM_API_KEY），跳过调用大模型。下面是实际会发给大模型的 Prompt：")
        print(prompt)
    else:
        print("\n=== 大模型回答 ===")
        print(answer)


# ============================================================
# 第 4 步：拼 Prompt + 调用大模型
# ============================================================
def build_prompt(question: str, hits) -> str:
    """把检索到的切片拼进提示词，这就是“检索增强”的核心动作。"""
    blocks = []
    for i, hit in enumerate(hits, 1):
        entity = hit["entity"]
        blocks.append(f"[资料{i}] 来源：{entity['source']}\n{entity['text']}")
    context = "\n\n".join(blocks)

    return (
        "请只根据下面的资料回答问题。如果资料中没有答案，"
        "请直接回答“资料中没有提到”，不要编造。\n\n"
        f"资料：\n{context}\n\n"
        f"问题：{question}\n\n"
        "回答："
    )


def ask_llm(prompt: str) -> str | None:
    """调用大模型；没有配置任何凭证时返回 None。

    优先走 Anthropic 兼容协议（阿里云百炼 Token Plan / 千问），
    也保留 OpenAI 兼容接口（DeepSeek、DashScope 等）作为备选。
    """
    anthropic_token = os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
    if anthropic_token:
        from anthropic import Anthropic

        client_kwargs: dict = {"auth_token": anthropic_token}
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
        if base_url:
            client_kwargs["base_url"] = base_url

        response = Anthropic(**client_kwargs).messages.create(
            model=os.getenv("LLM_MODEL", "qwen3.8-max"),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
            messages=[{"role": "user", "content": prompt}],
        )
        # 千问可能返回 thinking 块，这里只取最终文本
        return "".join(block.text for block in response.content if block.type == "text")

    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return None

    from openai import OpenAI

    client = OpenAI(
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=api_key,
    )
    response = client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "deepseek-chat"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return response.choices[0].message.content


# ============================================================
# 命令行入口
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="最小 RAG 示例（Milvus + 本地向量模型）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_ingest = subparsers.add_parser("ingest", help="文档切片 -> 向量化 -> 写入 Milvus")
    p_ingest.set_defaults(func=cmd_ingest)

    p_ask = subparsers.add_parser("ask", help="检索 -> 拼 Prompt -> 调用大模型")
    p_ask.add_argument("question", help="你的问题")
    p_ask.add_argument("--raw", action="store_true", help="只打印检索结果和 Prompt，不调大模型")
    p_ask.set_defaults(func=cmd_ask)

    args = parser.parse_args()
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n[出错] {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"排查提示：Milvus 是否可访问（{MILVUS_URI}）？依赖是否装全？", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
