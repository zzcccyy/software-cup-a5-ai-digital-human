"""
Waitress WSGI server config for Windows deployment.
Usage: python waitress_server.py
"""
import os
import sys
from pathlib import Path

# Add backend directory to path
BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

# 关键修复: 强制 UTF-8 避免中文 mojibake
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from main import app


def create_server():
    from waitress import serve
    print("游客端 http://localhost:8088 | 管理端 http://localhost:8088/admin", flush=True)
    print("使用 Waitress 生产级服务器 (多线程)", flush=True)
    serve(
        app,
        host="0.0.0.0",
        port=8088,
        # 关键修复: 线程数 4 → 8, 适配并发 TTS
        threads=int(os.environ.get("WAITRESS_THREADS", "8")),
        # 关键修复: channel_timeout 120s → 300s, 给长 TTS/长 SSE 留余量
        channel_timeout=300,
        cleanup_interval=30,
        # 关键修复: 10MB → 12MB (略放宽语音上传)
        max_request_body_size=12 * 1024 * 1024,
        # 关键修复: 禁用内部 buffer 累积, 让 SSE 立即 flush
        send_bytes=4096,
    )


if __name__ == "__main__":
    create_server()
