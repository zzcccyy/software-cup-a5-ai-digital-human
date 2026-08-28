import importlib
import sys
from pathlib import Path
from unittest import mock

import unittest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True

try:
    from test_quality_regression import reset_database_module  # discover 模式: tests/ 在 sys.path
except ModuleNotFoundError:
    from tests.test_quality_regression import reset_database_module  # 包模式: python -m unittest tests.test_sse_cleanup


LOCAL_CONTEXT = {
    "reply": "stream reply.",
    "emotion": "delighted",
    "emotionPayload": {"label": "delighted"},
    "route": {"id": "r", "stops": []},
    "sources": [],
    "topics": [],
    "answerMode": "mock-local",
    "faqMatch": None,
    "supportingFacts": [],
    "knowledge": [],
}


class SseCleanupFunctionalTests(unittest.TestCase):
    """P0.2: generate() 断连/异常/正常完成三路都必须清理 _ACTIVE_GENS 与 TTS 任务."""

    @classmethod
    def setUpClass(cls):
        import database
        import ai_service

        reset_database_module(database)
        # 关键修复: 用 mock.patch 托管替代直接赋值, 类销毁时自动恢复原函数,
        # 避免污染 ai_service 模块导致 discover 全量跑时顺序耦合失败 (如 test_tts_fallback)
        cls._ai_patches = [
            mock.patch.object(ai_service, "warmup_tts", lambda: None),
            mock.patch.object(ai_service, "synthesize_tts", lambda *args, **kwargs: None),
            mock.patch.object(ai_service, "synthesize_tts_bytes", lambda *args, **kwargs: (None, None)),
        ]
        for p in cls._ai_patches:
            p.start()
        sys.modules.pop("main", None)
        # 关键修复: 与 FlaskFunctionalTests 保持一致, 屏蔽 main 模块启动时的后台线程
        # (startup-init 会跑 import_bundle_knowledge + TTS 预热, 与测试并发访问共享测试连接,
        # 类 teardown 关闭连接后该线程仍在跑 → 竞态 "Cannot operate on a closed database")
        with mock.patch("threading.Thread.start", lambda self: None):
            cls.main = importlib.import_module("main")
        cls.main.app.config.update(TESTING=True)
        cls.client = cls.main.app.test_client()

    @classmethod
    def tearDownClass(cls):
        for p in getattr(cls, "_ai_patches", []):
            p.stop()
        if getattr(cls.main, "db", None) is not None and getattr(cls.main.db, "_test_conn", None) is not None:
            cls.main.db._test_conn.real_close()
            cls.main.db._test_conn = None

    def tearDown(self):
        self.main._ACTIVE_GENS.clear()

    def _post_stream(self, session_id="s-sse", message="stream"):
        return self.client.post(
            "/api/v1/chat/text-stream",
            json={"message": message, "sessionId": session_id},
        )

    def test_disconnect_cleans_active_gens_and_cancels_tts(self):
        """客户端断连: 消费几帧后 close 流 → GeneratorExit → finally 清理."""
        with mock.patch.object(self.main, "build_dialog_context", return_value=LOCAL_CONTEXT), \
             mock.patch.object(self.main, "should_use_llm", return_value=False), \
             mock.patch.object(self.main, "_is_tts_enabled", return_value=False), \
             mock.patch.object(self.main.ai_service, "cancel_tts_for_tag") as cancel_mock:
            resp = self._post_stream("s-disconnect")
            self.assertEqual(resp.status_code, 200)
            stream = resp.response
            # 消费前几帧后模拟断连
            next(stream)
            next(stream)
            stream.close()
        self.assertNotIn("s-disconnect", self.main._ACTIVE_GENS)
        cancel_mock.assert_called_once()

    def test_phase1_exception_sends_error_and_cleans(self):
        """Phase1 异常: 前端收到 error 事件, _ACTIVE_GENS 已清."""
        def _boom(*args, **kwargs):
            raise RuntimeError("db locked")

        with mock.patch.object(self.main, "build_dialog_context", side_effect=_boom):
            resp = self._post_stream("s-phase1-err")
            body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("event: error", body)
        self.assertIn("LOCAL_FAILED", body)
        self.assertNotIn("s-phase1-err", self.main._ACTIVE_GENS)

    def test_normal_completion_cleans_and_sends_done(self):
        """正常路径: 收到 done 事件, 清理函数各调用一次."""
        with mock.patch.object(self.main, "build_dialog_context", return_value=LOCAL_CONTEXT), \
             mock.patch.object(self.main, "should_use_llm", return_value=False), \
             mock.patch.object(self.main, "_is_tts_enabled", return_value=False), \
             mock.patch.object(self.main.ai_service, "cancel_tts_for_tag") as cancel_mock:
            resp = self._post_stream("s-normal")
            body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("event: done", body)
        self.assertNotIn("s-normal", self.main._ACTIVE_GENS)
        cancel_mock.assert_called_once()

    def test_same_session_second_request_replaces_first(self):
        """同 session 连发两请求: 第二个取消第一个后, 旧生成残留被清理."""
        with mock.patch.object(self.main, "build_dialog_context", return_value=LOCAL_CONTEXT), \
             mock.patch.object(self.main, "should_use_llm", return_value=False), \
             mock.patch.object(self.main, "_is_tts_enabled", return_value=False):
            # 请求1: 只消费一帧然后挂起
            resp1 = self._post_stream("s-race")
            stream1 = resp1.response
            next(stream1)
            # 请求2: 完整消费 (触发旧生成取消)
            resp2 = self._post_stream("s-race")
            resp2.get_data(as_text=True)
            # 旧生成断连清理
            stream1.close()
        self.assertNotIn("s-race", self.main._ACTIVE_GENS)

    def test_llm_stream_error_still_cleans(self):
        """LLM 段异常: 收到 error 事件, 生成器完成, _ACTIVE_GENS 已清."""
        def _boom(*args, **kwargs):
            raise RuntimeError("provider down")
            yield  # pragma: no cover

        with mock.patch.object(self.main, "build_dialog_context", return_value=LOCAL_CONTEXT), \
             mock.patch.object(self.main, "should_use_llm", return_value=True), \
             mock.patch.object(self.main, "chat_with_api_stream", side_effect=_boom):
            resp = self._post_stream("s-llm-err")
            body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("event: error", body)
        self.assertNotIn("s-llm-err", self.main._ACTIVE_GENS)


if __name__ == "__main__":
    unittest.main()
