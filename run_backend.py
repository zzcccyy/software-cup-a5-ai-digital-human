import subprocess
import os

BASE = os.path.dirname(os.path.abspath(__file__))
backend = os.path.join(BASE, "backend")
subprocess.run(["python", "main.py"], cwd=backend)
input("按回车退出...")