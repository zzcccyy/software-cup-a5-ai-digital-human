import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("SILICONFLOW_API_KEY", "")

import main
import ai_service


class AnswerRoutingTests(unittest.TestCase):
    def test_operational_questions_use_curated_faq_answers(self):
        cases = [
            ("小孩多高免票？", "1.4米"),
            ("景区几点开门，几点停止入园？", "17:00停止入园"),
            ("梵宫演出有几场，分别几点？", "10:00、11:30、14:00、16:00"),
            ("九龙灌浴什么时候演出？", "当日公告"),
            ("门票包含梵宫吉祥颂演出吗？", "需另购"),
            ("景区停车场在哪里，收费多少？", "东门和北门"),
            ("哪里可以找到卫生间？", "大佛广场、梵宫、五印坛城"),
            ("有轮椅租赁和无障碍服务吗？", "免费租赁服务"),
            ("可以带宠物进入景区吗？", "禁止携带宠物入园"),
            ("游玩一圈大约需要多久？", "3-4小时"),
            ("景区里有什么特色活动？", "《吉祥颂》演出"),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                context = main.build_dialog_context(question, "history")
                self.assertEqual(context["answerMode"], "local-operational-faq")
                self.assertIn(expected, context["reply"])
                self.assertFalse(main.should_use_llm(question, context, skip_llm=False))

    def test_spot_intro_stays_local(self):
        context = main.build_dialog_context("介绍一下梵宫的建筑艺术特色", "history")
        self.assertEqual(context["answerMode"], "local-spot-intro")
        self.assertFalse(main.should_use_llm("介绍一下梵宫的建筑艺术特色", context, skip_llm=False))

    def test_grounded_open_ended_question_can_use_llm(self):
        question = "灵山胜境整体有什么文化特色？"
        context = {"answerMode": "local-knowledge", "confidence": 0.8}
        self.assertTrue(main.should_use_llm(question, context, skip_llm=False))

    def test_unknown_fact_does_not_use_llm(self):
        context = main.build_dialog_context("景区今天瞬时客流量是多少？", "history")
        self.assertFalse(main.should_use_llm("景区今天瞬时客流量是多少？", context, skip_llm=False))

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
