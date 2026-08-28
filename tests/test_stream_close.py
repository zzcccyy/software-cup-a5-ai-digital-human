import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True

import ai_service  # noqa: E402


class FakeStreamResponse:
    """模拟流式 HTTP 响应, 记录 close() 调用."""

    def __init__(self, lines, status_code=200):
        self.lines = list(lines)
        self.status_code = status_code
        self.text = "mock error body"
        self.closed = False
        self.close_calls = 0

    def iter_lines(self, decode_unicode=False):
        for line in self.lines:
            yield line

    def close(self):
        self.closed = True
        self.close_calls += 1


SSE_OK_LINES = [
    'data: {"choices": [{"delta": {"content": "灵山"}}]}'.encode("utf-8"),
    'data: {"choices": [{"delta": {"content": "胜境"}}]}'.encode("utf-8"),
    b"data: [DONE]",
]


def _patch_http_post(resp):
    return mock.patch.object(ai_service._http, "post", return_value=resp)


class SiliconflowStreamCloseTests(unittest.TestCase):
    """P3.5: _siliconflow_chat_stream 断连/正常/异常三路都关闭响应连接."""

    def test_generator_exit_closes_connection(self):
        """客户端提前 close() 生成器 (GeneratorExit) → finally 关闭连接."""
        resp = FakeStreamResponse(SSE_OK_LINES)
        with _patch_http_post(resp):
            gen = ai_service._siliconflow_chat_stream([{"role": "user", "content": "hi"}])
            first = next(gen)  # 停在第一个 yield
            self.assertEqual(first, "灵山")
            gen.close()  # 断连
        self.assertTrue(resp.closed)
        self.assertEqual(resp.close_calls, 1)

    def test_normal_consumption_closes_connection(self):
        """消费到 [DONE] 正常结束 → finally 关闭连接."""
        resp = FakeStreamResponse(SSE_OK_LINES)
        with _patch_http_post(resp):
            chunks = list(ai_service._siliconflow_chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual("".join(chunks), "灵山胜境")
        self.assertTrue(resp.closed)

    def test_non_200_closes_connection(self):
        """上游返回错误状态码 → 连接同样被关闭."""
        resp = FakeStreamResponse(SSE_OK_LINES, status_code=500)
        with _patch_http_post(resp):
            chunks = list(ai_service._siliconflow_chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(chunks, [])
        self.assertTrue(resp.closed)

    def test_post_exception_no_crash(self):
        """post 本身抛异常 (r 为 None) → 不崩溃, 不调 close."""
        with mock.patch.object(ai_service._http, "post", side_effect=RuntimeError("boom")):
            chunks = list(ai_service._siliconflow_chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(chunks, [])


class DeepseekStreamCloseTests(unittest.TestCase):
    """P3.5: _deepseek_chat_stream 三路关闭连接 (与 siliconflow 对齐)."""

    def test_generator_exit_closes_connection(self):
        resp = FakeStreamResponse(SSE_OK_LINES)
        with _patch_http_post(resp):
            gen = ai_service._deepseek_chat_stream([{"role": "user", "content": "hi"}])
            first = next(gen)
            self.assertEqual(first, "灵山")
            gen.close()
        self.assertTrue(resp.closed)

    def test_normal_consumption_closes_connection(self):
        resp = FakeStreamResponse(SSE_OK_LINES)
        with _patch_http_post(resp):
            chunks = list(ai_service._deepseek_chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual("".join(chunks), "灵山胜境")
        self.assertTrue(resp.closed)

    def test_post_exception_no_crash(self):
        with mock.patch.object(ai_service._http, "post", side_effect=RuntimeError("boom")):
            chunks = list(ai_service._deepseek_chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(chunks, [])


if __name__ == "__main__":
    unittest.main()
