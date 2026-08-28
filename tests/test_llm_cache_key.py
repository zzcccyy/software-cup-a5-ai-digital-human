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

MESSAGE = "灵山胜境门票多少钱?"


class LlmCacheKeyUnitTests(unittest.TestCase):
    """P3.1: 缓存键纳入 interest/route/draft_answer/supporting_facts."""

    def test_different_interest_different_key(self):
        k1 = ai_service._make_llm_cache_key(MESSAGE, None, None, interest="history")
        k2 = ai_service._make_llm_cache_key(MESSAGE, None, None, interest="family")
        self.assertNotEqual(k1, k2)

    def test_different_route_different_key(self):
        k1 = ai_service._make_llm_cache_key(MESSAGE, None, None, route={"id": "r1", "name": "a"})
        k2 = ai_service._make_llm_cache_key(MESSAGE, None, None, route={"id": "r2", "name": "a"})
        k3 = ai_service._make_llm_cache_key(MESSAGE, None, None, route=None)
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_different_draft_answer_different_key(self):
        k1 = ai_service._make_llm_cache_key(MESSAGE, None, None, draft_answer="草稿A")
        k2 = ai_service._make_llm_cache_key(MESSAGE, None, None, draft_answer="草稿B")
        k3 = ai_service._make_llm_cache_key(MESSAGE, None, None, draft_answer="")
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_different_supporting_facts_different_key(self):
        k1 = ai_service._make_llm_cache_key(MESSAGE, None, None, supporting_facts=["门票210元"])
        k2 = ai_service._make_llm_cache_key(MESSAGE, None, None, supporting_facts=["门票105元"])
        k3 = ai_service._make_llm_cache_key(MESSAGE, None, None, supporting_facts=None)
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_identical_params_same_key(self):
        ctx = [{"content": "灵山胜境是AAAAA景区"}]
        hist = [{"role": "user", "content": "你好"}]
        a = ai_service._make_llm_cache_key(MESSAGE, ctx, hist, interest="history", route={"id": "r1"}, draft_answer="d", supporting_facts=["f1"])
        b = ai_service._make_llm_cache_key(MESSAGE, ctx, hist, interest="history", route={"id": "r1"}, draft_answer="d", supporting_facts=["f1"])
        self.assertEqual(a, b)


class LlmCacheFunctionalTests(unittest.TestCase):
    """P3.1: chat_with_api 实际缓存行为 — 不同 interest 不串缓存."""

    def setUp(self):
        ai_service._LLM_CACHE.clear()

    def _patch_providers(self):
        """同时 mock 两个 provider (LLM_PROVIDER 随 env 变化), 返回 mock 便于计数."""
        p1 = mock.patch.object(ai_service, "_siliconflow_chat", return_value="游客您好，门票210元。")
        p2 = mock.patch.object(ai_service, "_deepseek_chat", return_value="游客您好，门票210元。")
        sf = p1.start()
        ds = p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return sf, ds

    def _count_provider_calls(self):
        return self.sf_mock.call_count + self.ds_mock.call_count

    def test_same_question_different_interest_creates_separate_entries(self):
        self.sf_mock, self.ds_mock = self._patch_providers()
        r1 = ai_service.chat_with_api(MESSAGE, avatar_config={"interest": "history"})
        r2 = ai_service.chat_with_api(MESSAGE, avatar_config={"interest": "family"})
        self.assertEqual(self._count_provider_calls(), 2)
        self.assertEqual(r1["reply"], r2["reply"])
        self.assertEqual(len(ai_service._LLM_CACHE), 2)

    def test_same_params_hits_cache(self):
        self.sf_mock, self.ds_mock = self._patch_providers()
        ai_service.chat_with_api(MESSAGE, avatar_config={"interest": "history"})
        before = self._count_provider_calls()
        r = ai_service.chat_with_api(MESSAGE, avatar_config={"interest": "history"})
        self.assertEqual(self._count_provider_calls(), before)
        self.assertEqual(r["reply"], "游客您好，门票210元。")

    def test_route_id_changes_cache_entry(self):
        self.sf_mock, self.ds_mock = self._patch_providers()
        ai_service.chat_with_api(MESSAGE, route={"id": "r1", "name": "文化线"}, avatar_config={"interest": "history"})
        r2 = ai_service.chat_with_api(MESSAGE, route={"id": "r2", "name": "亲子线"}, avatar_config={"interest": "history"})
        self.assertEqual(self._count_provider_calls(), 2)
        self.assertEqual(r2["reply"], "游客您好，门票210元。")


if __name__ == "__main__":
    unittest.main()
