import json
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True


class ConversationAnalysisServiceTests(unittest.TestCase):
    def setUp(self):
        import database
        import conversation_analysis

        self.db = database
        self.service = conversation_analysis
        self.db._reset_thread_conns()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.original_get_conn = self.db.get_conn
        self.original_get_read_conn = self.db.get_read_conn
        self.db.get_conn = lambda: self.connection
        self.db.get_read_conn = lambda: self.connection
        self.db._thread_local.tx_depth = 0
        self.db.init_db()
        self.base_time = datetime(2026, 8, 14, 12, 0, 0)

    def tearDown(self):
        self.db.get_conn = self.original_get_conn
        self.db.get_read_conn = self.original_get_read_conn
        self.db._thread_local.tx_depth = 0
        self.connection.close()

    def add_conversation(
        self,
        conversation_id,
        *,
        timestamp="2026-08-14 12:00:00",
        message="游客问题",
        reply="数字人回复",
        emotion="warm",
        interest="history",
        satisfaction=None,
        topics=None,
    ):
        with self.db.get_db(write=True) as conn:
            conn.execute(
                "INSERT INTO conversations "
                "(id, session_id, user_id, message, reply, emotion, satisfaction, "
                "interest, topics, timestamp, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    conversation_id,
                    f"session-{conversation_id}",
                    f"user-{conversation_id}",
                    message,
                    reply,
                    emotion,
                    satisfaction,
                    interest,
                    json.dumps(topics or [], ensure_ascii=False),
                    timestamp,
                ),
            )

    def test_report_uses_filtered_rows_and_computes_rating_coverage(self):
        self.add_conversation("match-1", message="请问演出时间", reply="reply-only-keyword", satisfaction=5, topics=["后台标签"])
        self.add_conversation("match-2", message="演出时间和门票价格", reply="reply-only-keyword", satisfaction=2, emotion="neutral", topics=["后台标签"])
        self.add_conversation("unrated", message="请问门票价格", reply="reply-only-keyword", satisfaction=None, topics=["后台标签"])
        self.add_conversation("outside", timestamp="2026-08-07 11:59:59", satisfaction=5)

        with mock.patch.object(self.service, "_call_analysis_llm", return_value=None), \
                mock.patch.object(self.db, "datetime") as datetime_class:
            datetime_class.now.return_value = self.base_time
            report = self.service.analyze_conversations({"period": "week"}, sample_limit=10)

        self.assertEqual(report["scope"]["totalConversations"], 3)
        self.assertEqual(report["metrics"]["ratedConversations"], 2)
        self.assertEqual(report["metrics"]["avgSatisfaction"], 3.5)
        self.assertEqual(report["metrics"]["ratingCoverage"], 66.7)
        self.assertEqual(report["metrics"]["dailyTrend"], [{"name": "2026-08-14", "value": 3}])
        keywords = {item["name"]: item["value"] for item in report["metrics"]["keywordDistribution"]}
        self.assertEqual(keywords["演出时间"], 2)
        self.assertNotIn("后台标签", keywords)
        self.assertNotIn("reply-only-keyword", keywords)
        self.assertEqual(report["meta"]["mode"], "deterministic")
        self.assertNotIn("outside", json.dumps(report, ensure_ascii=False))

    def test_keyword_distribution_keeps_whole_terms_instead_of_character_ngrams(self):
        rows = [
            {"message": "请问演出时间和门票价格", "topics": []},
            {"message": "演出时间和门票价格", "topics": []},
            {"message": "梵宫是什么，4m，景区在哪，演出几点", "topics": []},
        ]

        keywords = {item["name"]: item["value"] for item in self.service._keyword_distribution(rows)}

        self.assertEqual(keywords["演出时间"], 2)
        self.assertEqual(keywords["门票价格"], 2)
        self.assertNotIn("出时", keywords)
        self.assertNotIn("演出时", keywords)
        self.assertNotIn("出时间", keywords)
        self.assertNotIn("门票价", keywords)
        self.assertNotIn("梵宫是什么", keywords)
        self.assertNotIn("有什么好玩", keywords)
        self.assertNotIn("4m", keywords)
        self.assertNotIn("景区在哪", keywords)
        self.assertNotIn("演出几点", keywords)

    def test_llm_prompt_contains_content_but_not_identity_fields(self):
        self.add_conversation(
            "private-row",
            message="请帮我找演出时间",
            reply="可以查看今日演出安排",
            satisfaction=5,
        )
        captured = {}

        def fake_llm(messages):
            captured["messages"] = messages
            return json.dumps(
                {
                    "executiveSummary": "游客主要关注演出安排。",
                    "findings": [],
                    "knowledgeGaps": [],
                    "suggestions": [],
                    "cases": [],
                    "limitations": [],
                },
                ensure_ascii=False,
            )

        with mock.patch.object(self.service, "_call_analysis_llm", side_effect=fake_llm):
            report = self.service.analyze_conversations({}, sample_limit=10)

        prompt = json.dumps(captured["messages"], ensure_ascii=False)
        self.assertIn("请帮我找演出时间", prompt)
        self.assertNotIn("session-private-row", prompt)
        self.assertNotIn("user-private-row", prompt)
        self.assertNotIn('"id"', prompt)
        self.assertEqual(report["meta"]["mode"], "ai")

    def test_invalid_llm_json_falls_back_without_failing_request(self):
        self.add_conversation("fallback-row", satisfaction=4)

        with mock.patch.object(self.service, "_call_analysis_llm", return_value="{}"):
            report = self.service.analyze_conversations({}, sample_limit=10)

        self.assertEqual(report["meta"]["mode"], "deterministic")
        self.assertTrue(report["meta"]["warnings"])
        self.assertIsInstance(report["findings"], list)

    def test_empty_filter_result_returns_report_without_calling_ai(self):
        with mock.patch.object(self.service, "_call_analysis_llm") as call_llm:
            report = self.service.analyze_conversations({"emotion": "focused"}, sample_limit=20)

        self.assertEqual(report["scope"]["totalConversations"], 0)
        self.assertEqual(report["meta"]["mode"], "deterministic")
        call_llm.assert_not_called()

    def test_request_parser_normalizes_filters_and_sample_limit(self):
        filters, sample_limit, error = self.service.parse_analysis_request(
            {
                "filters": {
                    "period": " week ",
                    "emotion": " warm ",
                    "interest": " history ",
                    "satisfaction": "5",
                },
                "sampleLimit": 40,
            }
        )

        self.assertIsNone(error)
        self.assertEqual(
            filters,
            {"period": "week", "emotion": "warm", "interest": "history", "satisfaction": 5},
        )
        self.assertEqual(sample_limit, 40)

    def test_request_parser_rejects_out_of_range_sample_limit(self):
        filters, sample_limit, error = self.service.parse_analysis_request(
            {"filters": {}, "sampleLimit": 1000}
        )

        self.assertIsNone(filters)
        self.assertIsNone(sample_limit)
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
