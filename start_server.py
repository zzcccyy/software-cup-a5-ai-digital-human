import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent / "backend"

proc = subprocess.Popen(
    [sys.executable, str(BACKEND_DIR / "main.py")],
    creationflags=subprocess.CREATE_NEW_CONSOLE
)
print("后端已启动, PID:", proc.pid)
input("按回车停止...")