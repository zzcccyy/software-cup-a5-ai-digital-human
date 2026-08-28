import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True


class InMemoryMetricsTestCase(unittest.TestCase):
    def setUp(self):
        import analyzer
        import database

        self.analyzer = analyzer
        self.db = database
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

    def tearDown(self):
        self.db.get_conn = self.original_get_conn
        self.db.get_read_conn = self.original_get_read_conn
        self.db._thread_local.tx_depth = 0
        self.connection.close()

    def add_conversation(
        self,
        conversation_id,
        session_id,
        user_id,
        topics,
        *,
        timestamp=None,
        satisfaction=None,
        interest="history",
        message="",
    ):
        with self.db.get_db(write=True) as conn:
            conn.execute(
                "INSERT INTO conversations "
                "(id, session_id, user_id, message, reply, emotion, satisfaction, "
                "interest, topics, timestamp, latency_ms) "
                "VALUES (?, ?, ?, ?, 'reply', 'warm', ?, ?, ?, ?, 10)",
                (
                    conversation_id,
                    session_id,
                    user_id,
                    message,
                    satisfaction,
                    interest,
                    json.dumps(topics, ensure_ascii=False),
                    timestamp or self.db.now_str(),
                ),
            )


class DashboardMetricsUnitTests(InMemoryMetricsTestCase):
    def test_dashboard_separates_deduplicated_visitors_from_conversations(self):
        today = datetime.now().strftime("%Y-%m-%d 10:00:00")
        week_day = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d 10:00:00")
        self.add_conversation("c1", "session-a", "user-1", ["门票价格"], timestamp=today, satisfaction=4)
        self.add_conversation("c2", "session-b", "user-1", ["路线推荐"], timestamp=today, satisfaction=2)
        self.add_conversation("c3", "guest-session", "guest", ["景点讲解"], timestamp=today, satisfaction=5)
        self.add_conversation("c4", "guest-session", "", ["闲聊"], timestamp=today)
        self.add_conversation("c5", "session-c", "user-2", ["门票价格", "路线推荐"], timestamp=week_day, satisfaction=3)

        dashboard = self.db.compute_dashboard()

        self.assertEqual(dashboard["todayVisitors"], 2)
        self.assertEqual(dashboard["weekVisitors"], 3)
        self.assertEqual(dashboard["todayConversations"], 4)
        self.assertEqual(dashboard["weekConversations"], 5)
        self.assertEqual(dashboard["totalChats"], 5)

    def test_service_ratio_classifies_topics_with_ticket_priority_and_sums_to_100(self):
        today = datetime.now().strftime("%Y-%m-%d 10:00:00")
        self.add_conversation("c1", "s1", "u1", ["门票价格"], timestamp=today)
        self.add_conversation("c2", "s2", "u2", ["路线推荐"], timestamp=today)
        self.add_conversation("c3", "s3", "u3", ["景点讲解"], timestamp=today)
        self.add_conversation("c4", "s4", "u4", ["闲聊"], timestamp=today)
        self.add_conversation("c5", "s5", "u5", ["门票价格", "路线推荐"], timestamp=today)

        service_ratio = self.db.compute_dashboard()["serviceRatio"]

        self.assertEqual(service_ratio, {"consult": 20, "ticket": 40, "guide": 40})
        self.assertEqual(sum(service_ratio.values()), 100)

    def test_service_ratio_is_zero_for_empty_data(self):
        self.assertEqual(
            self.db.compute_dashboard()["serviceRatio"],
            {"consult": 0, "ticket": 0, "guide": 0},
        )


class AnalyzerMetricsUnitTests(InMemoryMetricsTestCase):
    def test_visitor_profiling_uses_average_satisfaction_and_keeps_count(self):
        self.add_conversation("c1", "s1", "u1", [], interest="history", satisfaction=2)
        self.add_conversation("c2", "s2", "u2", [], interest="history", satisfaction=4)
        self.add_conversation("c3", "s3", "u3", [], interest="history")
        self.add_conversation("c4", "s4", "u4", [], interest="route")

        profiles = self.analyzer.SentimentAnalyzer().visitor_profiling()

        self.assertEqual(profiles["history"], {"count": 3, "avgSatisfaction": 3.0})
        self.assertEqual(profiles["route"], {"count": 1, "avgSatisfaction": None})

    def test_spot_heatmap_counts_all_historical_mentions(self):
        old_timestamp = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 10:00:00")
        self.add_conversation(
            "old-spot",
            "old-session",
            "old-user",
            [],
            timestamp=old_timestamp,
            satisfaction=4,
            message="请介绍灵山大佛",
        )
        for index in range(501):
            self.add_conversation(
                f"recent-{index}",
                f"recent-session-{index}",
                f"recent-user-{index}",
                [],
                timestamp=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                message=f"普通问题 {index}",
            )

        heatmap = self.analyzer.SentimentAnalyzer().spot_heatmap()
        spot = next(item for item in heatmap if item["spot"] == "灵山大佛")

        self.assertEqual(spot["mentions"], 1)
        self.assertEqual(spot["avgSatisfaction"], 4.0)
        self.assertNotIn("visits", spot)

    def test_latest_feedback_filters_unrated_rows_before_limit(self):
        base = datetime.now()
        for index in range(3):
            self.add_conversation(
                f"unrated-{index}",
                f"unrated-session-{index}",
                f"unrated-user-{index}",
                [],
                timestamp=(base - timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S"),
            )
        self.add_conversation(
            "rated-old",
            "rated-session",
            "rated-user",
            [],
            timestamp=(base - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
            satisfaction=5,
        )

        feedback = self.db.get_latest_feedback(1)

        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]["id"], "rated-old")
        self.assertEqual(feedback[0]["satisfaction"], 5)


class DataScreenFrontendContractTests(unittest.TestCase):
    def test_data_screen_uses_real_ratio_and_mention_fields(self):
        html = (ROOT / "admin" / "data-screen.html").read_text(encoding="utf-8")
        admin_js = (ROOT / "admin" / "app.js").read_text(encoding="utf-8")

        self.assertIn("d.serviceRatio", html)
        self.assertNotIn("|| 45", html)
        self.assertIn("d.mentions", html)
        self.assertNotIn("d.visits", html)
        self.assertIn("i.mentions || 0", admin_js)
        header = html.split('<div class="screen-header">', 1)[1].split('</div>\n\n<!-- Dashboard -->', 1)[0]
        self.assertIn('id="screen-time"', header)
        self.assertNotIn("href=", header)
        self.assertNotIn("data-update", header)
        self.assertIn("/admin/assets/data-screen-bg-v2.png", html)
        self.assertTrue((ROOT / "admin" / "assets" / "data-screen-bg-v2.png").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
