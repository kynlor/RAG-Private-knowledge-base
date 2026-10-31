"""备份用户数据库、知识库文件和会话密钥到 backups/*.zip。

向量数据不直接打包（Milvus 数据在 ./volumes/ 下，体积大）。
恢复后可以用 data/docs 重新入库；需要完整 Milvus 备份时使用 milvus-backup 工具。

用法：
    .venv\\Scripts\\python.exe backup.py
"""

from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "app.db"
SECRET_PATH = BASE_DIR / "data" / ".secret_key"
DOCS_DIR = BASE_DIR / "data" / "docs"
BACKUP_DIR = BASE_DIR / "backups"


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"rag-backup-{stamp}.zip"

    with tempfile.TemporaryDirectory() as temp_dir:
        db_copy = Path(temp_dir) / "app.db"
        if DB_PATH.exists():
            source = sqlite3.connect(DB_PATH)
            destination = sqlite3.connect(db_copy)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            if db_copy.exists():
                archive.write(db_copy, "data/app.db")
            if SECRET_PATH.exists():
                archive.write(SECRET_PATH, "data/.secret_key")
            if DOCS_DIR.exists():
                for path in sorted(DOCS_DIR.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(BASE_DIR))

    size = target.stat().st_size
    print(f"备份完成：{target}（{size / 1024:.1f} KB）")
    print("恢复：停止服务，解压到项目根目录覆盖 data/app.db、data/.secret_key、data/docs，然后重启。")


if __name__ == "__main__":
    main()
