import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("SILICONFLOW_API_KEY", "")

import ai_service
import main


class AccuracyRoutingTests(unittest.TestCase):
    def test_ticket_inclusion_intent_wins_over_showtime(self):
        cases = [
            ("梵宫演出需不需要另购？", "需另购"),
            ("九龙灌浴表演免费吗？", "免费观看"),
            ("吉祥颂演出票多少钱？", "需另购"),
            ("儿童票包含演出吗？", "需另购"),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                context = main.build_dialog_context(question, "history")
                self.assertEqual(context["answerMode"], "local-operational-faq")
                self.assertIn(expected, context["reply"])
                self.assertFalse(main.should_use_llm(question, context, skip_llm=False))

    def test_time_fact_does_not_use_llm(self):
        question = "灵山大佛哪年建成？"
        context = main.build_dialog_context(question, "history")
        self.assertFalse(main.should_use_llm(question, context, skip_llm=False))

    def test_keyword_fallback_does_not_enable_llm(self):
        context = {
            "answerMode": "local-knowledge",
            "confidence": 0.95,
            "knowledge": [{"retriever": "keyword-fallback", "score": 0.95}],
        }
        self.assertFalse(main.should_use_llm("灵山胜境整体有什么文化特色？", context, skip_llm=False))

    def test_grounded_vector_context_can_use_llm(self):
        context = {
            "answerMode": "local-knowledge",
            "confidence": 0.8,
            "knowledge": [{"retriever": "vector", "score": 0.8}],
        }
        self.assertTrue(main.should_use_llm("灵山胜境整体有什么文化特色？", context, skip_llm=False))

    def test_empty_provider_response_is_not_marked_as_llm(self):
        with patch.object(ai_service, "_deepseek_chat", return_value=None):
            old_provider = ai_service.LLM_PROVIDER
            try:
                ai_service.LLM_PROVIDER = "deepseek"
                result = ai_service.chat_with_api(
                    "请润色这段介绍",
                    draft_answer="本地回答。",
                    knowledge_context=[],
                )
            finally:
                ai_service.LLM_PROVIDER = old_provider
        self.assertFalse(result["used_api"])
        self.assertEqual(result["reply"], "本地回答。")


if __name__ == "__main__":
    unittest.main()
