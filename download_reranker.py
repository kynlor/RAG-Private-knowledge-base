"""从 ModelScope 下载 BGE 中文 reranker（量化 ONNX）到 models/ 目录。

HuggingFace 无法访问时使用本脚本；模型约 280MB。
用法：
    .venv\\Scripts\\python.exe download_reranker.py
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
TARGET_DIR = BASE_DIR / "models" / "bge-reranker-base"
URL_BASE = "https://www.modelscope.cn/models/Xenova/bge-reranker-base/resolve/master/"

# (远程路径, 本地路径, 期望字节数)
FILES = [
    ("config.json", "config.json", 782),
    ("tokenizer.json", "tokenizer.json", 17_098_079),
    ("tokenizer_config.json", "tokenizer_config.json", 443),
    ("special_tokens_map.json", "special_tokens_map.json", 279),
    ("sentencepiece.bpe.model", "sentencepiece.bpe.model", 5_069_051),
    ("onnx/model_quantized.onnx", "onnx/model.onnx", 279_301_077),
]


def fetch(remote: str, local: str, expected: int) -> None:
    destination = TARGET_DIR / local
    destination.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, 31):
        current = destination.stat().st_size if destination.exists() else 0
        if current == expected:
            print(f"OK {local} ({current} bytes)")
            return
        if current > expected:
            destination.unlink()
            current = 0

        headers = {"Range": f"bytes={current}-"} if current else {}
        print(f"[{attempt}] {local}: {current}/{expected}")
        try:
            with requests.get(
                URL_BASE + remote,
                headers=headers,
                stream=True,
                timeout=180,
            ) as response:
                response.raise_for_status()
                mode = "ab" if current and response.status_code == 206 else "wb"
                with destination.open(mode) as handle:
                    for chunk in response.iter_content(256 * 1024):
                        if chunk:
                            handle.write(chunk)
        except Exception as exc:  # noqa: BLE001
            print(f"  重试：{exc}")
        time.sleep(2)

    raise SystemExit(f"下载失败：{remote}")


def main() -> None:
    for remote, local, expected in FILES:
        fetch(remote, local, expected)
    print(f"完成：{TARGET_DIR}")


if __name__ == "__main__":
    main()
