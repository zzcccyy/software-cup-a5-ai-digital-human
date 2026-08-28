"""Windows desktop launcher for the competition distribution."""

from __future__ import annotations

import os
import threading
import time
import urllib.request
import webbrowser

from waitress import serve

from runtime_paths import APP_ROOT


APP_URL = "http://127.0.0.1:8088/"
HEALTH_URL = "http://127.0.0.1:8088/api/v1/health"


def _service_is_running() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def _open_browser_when_ready() -> None:
    for _ in range(90):
        if _service_is_running():
            if os.environ.get("LINGSHAN_NO_BROWSER") != "1":
                webbrowser.open(APP_URL)
            return
        time.sleep(1)
    print("启动超时：请检查窗口中的错误信息。", flush=True)


def main() -> int:
    from main import app

    if _service_is_running():
        print("检测到程序已经运行，正在打开浏览器……", flush=True)
        if os.environ.get("LINGSHAN_NO_BROWSER") != "1":
            webbrowser.open(APP_URL)
        return 0

    print("=" * 56, flush=True)
    print("  灵山胜境 AI 数字人导览系统", flush=True)
    print("=" * 56, flush=True)
    print(f"程序目录：{APP_ROOT}", flush=True)
    print("正在启动，请稍候……", flush=True)
    print("游客端：http://127.0.0.1:8088/", flush=True)
    print("管理端：http://127.0.0.1:8088/admin", flush=True)
    print("关闭本窗口即可停止程序。", flush=True)

    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    try:
        serve(app, host="127.0.0.1", port=8088, threads=8, channel_timeout=300)
    except OSError as exc:
        print(f"启动失败：8088 端口可能被其他程序占用（{exc}）", flush=True)
        input("按回车键退出……")
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
