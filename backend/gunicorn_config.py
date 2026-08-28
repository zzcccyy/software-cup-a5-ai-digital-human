import multiprocessing
import os

# Server socket
bind = "0.0.0.0:8088"
backlog = 2048

# Worker processes - 关键修复: 不再随 CPU 数爆量, 默认 4 个 worker
# (边缘机器 16+ 核会导致 33 worker 抢端口, 17 worker 状态分裂)
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
worker_timeout = 300  # 关键修复: 120s → 300s, 给长 TTS 留余量

# 关键修复: 禁用频繁 worker 回收 (原 max_requests=1000 致状态丢失)
# 配 preload_app=True 时, 内存状态在 fork 后共享 read-only, 问题不大;
# 但最大 0 (禁用) 才是长跑稳定选择
max_requests = 0
max_requests_jitter = 0

# Logging - 关键修复: 强制 UTF-8 避免中文 mojibake
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Process naming
proc_name = "digital-human"

# Preload app for faster worker startup + 共享只读状态
preload_app = True

# Server mechanics - 关键修复: 延长 timeout + keepalive, 适配长 SSE
timeout = 300          # 120s → 300s, 给长 TTS 留余量
graceful_timeout = 60  # 优雅关闭超时
keepalive = 30         # 5s → 30s, 减少中间代理误关
