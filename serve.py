"""生产模式启动：使用 Waitress WSGI 服务器（Windows 推荐）。

开发调试用 app.py；正式使用建议用本文件，前面再挂 Nginx / Caddy 做 HTTPS。
"""

import os

from waitress import serve

from app import app

if __name__ == "__main__":
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_PORT", "8000"))
    threads = int(os.getenv("WEB_THREADS", "8"))
    print("=" * 56)
    print("  RAG 私有知识库（Waitress 生产模式）")
    print(f"  监听：http://{host}:{port}  threads={threads}")
    print("=" * 56)
    serve(app, host=host, port=port, threads=threads)
