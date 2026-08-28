import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True

import ai_service  # noqa: E402


def xunfei_frame(content: str, status: int = 0, code: int = 0) -> str:
    return json.dumps({
        "header": {"code": code, "status": status, "message": "ok"},
        "payload": {"choices": {"text": [{"content": content}]}},
    })


class FakeWS:
    def __init__(self, responses=None, exc=None):
        self.responses = list(responses or [])
        self.exc = exc
        self.sent = None
        self.closed = False

    def send(self, data):
        self.sent = data

    def recv(self):
        if self.exc:
            raise self.exc
        if not self.responses:
            raise RuntimeError("no more responses")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class XunfeiTimeoutUnitTests(unittest.TestCase):
    def setUp(self):
        self.env_patches = [
            mock.patch.object(ai_service, "XUNFEI_APP_ID", "test-app"),
            mock.patch.object(ai_service, "XUNFEI_API_KEY", "test-key"),
            mock.patch.object(ai_service, "XUNFEI_API_SECRET", "test-secret"),
            mock.patch.object(ai_service, "XUNFEI_API_BASE", "wss://spark-api.xf-yun.com/v4.0/chat"),
        ]
        for p in self.env_patches:
            p.start()
        self.addCleanup(self._stop_env_patches)

    def _stop_env_patches(self):
        for p in self.env_patches:
            p.stop()

    def _patch_ws(self, fake_ws):
        return mock.patch.object(
            ai_service.websocket, "create_connection", return_value=fake_ws
        )

    def _fast_forward_monotonic_after_first(self):
        real_monotonic = time.monotonic
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            # 首次调用用于计算 deadline, 之后瞬间跳到 100s 模拟慢速服务端
            return real_monotonic() if calls["n"] == 1 else 100.0

        return mock.patch.object(ai_service.time, "monotonic", side_effect=fake)

    def test_slow_drip_triggers_overall_timeout_and_closes(self):
        """服务端每 29s 吐一个分片: 25s 总时限必须触发, 返回 None 且连接被关闭."""
        fake = FakeWS(responses=[xunfei_frame("你好"), xunfei_frame("世界")])
        with self._patch_ws(fake), self._fast_forward_monotonic_after_first():
            result = ai_service._xunfei_chat([{"role": "user", "content": "hi"}])
        self.assertIsNone(result)
        self.assertTrue(fake.closed)

    def test_normal_stream_concatenates_and_closes(self):
        """正常流: code=0 分片拼接, status=2 收尾, close 被调用."""
        fake = FakeWS(responses=[xunfei_frame("你好"), xunfei_frame("世界", status=2)])
        with self._patch_ws(fake):
            result = ai_service._xunfei_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result, "你好世界")
        self.assertTrue(fake.closed)

    def test_exception_closes_and_returns_none(self):
        """异常流: recv 抛异常 → 返回 None 且连接被关闭."""
        import websocket as ws_lib

        fake = FakeWS(exc=ws_lib.WebSocketException("boom"))
        with self._patch_ws(fake):
            result = ai_service._xunfei_chat([{"role": "user", "content": "hi"}])
        self.assertIsNone(result)
        self.assertTrue(fake.closed)

    def test_upstream_error_code_breaks_and_closes(self):
        """服务端返回 code!=0: 停止收流, 关闭连接."""
        fake = FakeWS(responses=[xunfei_frame("", code=40001)])
        with self._patch_ws(fake):
            result = ai_service._xunfei_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result, "")
        self.assertTrue(fake.closed)

    def test_connect_failure_returns_none_without_ws(self):
        """连接失败: 直接返回 None, 不产生 close 调用."""
        with mock.patch.object(
            ai_service.websocket, "create_connection",
            side_effect=RuntimeError("connect refused"),
        ):
            result = ai_service._xunfei_chat([{"role": "user", "content": "hi"}])
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
