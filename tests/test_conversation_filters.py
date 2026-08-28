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


class ConversationFilterDatabaseTests(unittest.TestCase):
    def setUp(self):
        import database

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
        emotion="warm",
        interest="history",
        satisfaction=None,
    ):
        with self.db.get_db(write=True) as conn:
            conn.execute(
                "INSERT INTO conversations "
                "(id, session_id, user_id, message, reply, emotion, satisfaction, "
                "interest, topics, timestamp, latency_ms) "
                "VALUES (?, ?, ?, '', '', ?, ?, ?, '[]', ?, 0)",
                (
                    conversation_id,
                    conversation_id,
                    "guest",
                    emotion,
                    satisfaction,
                    interest,
                    timestamp,
                ),
            )

    def test_period_filters_include_exact_boundary_and_exclude_older_rows(self):
        self.add_conversation("day-boundary", timestamp="2026-08-13 12:00:00")
        self.add_conversation("day-before", timestamp="2026-08-13 11:59:59")
        self.add_conversation("week-boundary", timestamp="2026-08-07 12:00:00")
        self.add_conversation("week-before", timestamp="2026-08-07 11:59:59")
        self.add_conversation("month-boundary", timestamp="2026-07-15 12:00:00")
        self.add_conversation("month-before", timestamp="2026-07-15 11:59:59")

        with mock.patch.object(self.db, "datetime") as datetime_class:
            datetime_class.now.return_value = self.base_time
            self.assertEqual(
                {item["id"] for item in self.db.get_conversations(period="day")["list"]},
                {"day-boundary"},
            )
            self.assertEqual(
                {item["id"] for item in self.db.get_conversations(period="week")["list"]},
                {"day-boundary", "day-before", "week-boundary"},
            )
            self.assertEqual(
                {item["id"] for item in self.db.get_conversations(period="month")["list"]},
                {
                    "day-boundary",
                    "day-before",
                    "week-boundary",
                    "week-before",
                    "month-boundary",
                },
            )

    def test_independent_filters_match_exact_values_and_empty_values_do_not_filter(self):
        self.add_conversation("warm-history", emotion="warm", interest="history", satisfaction=5)
        self.add_conversation("warm-route", emotion="warm", interest="route", satisfaction=3)
        self.add_conversation("sad-history", emotion="sad", interest="history", satisfaction=None)
        self.add_conversation("empty-emotion", emotion="", interest="route", satisfaction=None)
        self.add_conversation("empty-interest", emotion="focused", interest="", satisfaction=None)

        self.assertEqual(
            {item["id"] for item in self.db.get_conversations(emotion="warm")["list"]},
            {"warm-history", "warm-route"},
        )
        self.assertEqual(
            {item["id"] for item in self.db.get_conversations(interest="history")["list"]},
            {"warm-history", "sad-history"},
        )
        self.assertEqual(
            {item["id"] for item in self.db.get_conversations(satisfaction=5)["list"]},
            {"warm-history"},
        )
        self.assertEqual(
            self.db.get_conversations(emotion="")["total"],
            5,
        )
        self.assertEqual(
            self.db.get_conversations()["total"],
            5,
        )

    def test_combined_filter_and_pagination_keep_total_consistent_with_list_where(self):
        self.add_conversation("match-new", timestamp="2026-08-14 11:00:00", interest="history", satisfaction=4)
        self.add_conversation("match-middle", timestamp="2026-08-14 10:00:00", interest="history", satisfaction=4)
        self.add_conversation("match-old", timestamp="2026-08-13 12:00:00", interest="history", satisfaction=4)
        self.add_conversation("wrong-interest", timestamp="2026-08-14 09:00:00", interest="route", satisfaction=4)
        self.add_conversation("wrong-emotion", timestamp="2026-08-14 08:00:00", emotion="sad", interest="history", satisfaction=4)
        self.add_conversation("wrong-satisfaction", timestamp="2026-08-14 07:00:00", interest="history", satisfaction=5)

        with mock.patch.object(self.db, "datetime") as datetime_class:
            datetime_class.now.return_value = self.base_time
            result = self.db.get_conversations(
                page=2,
                page_size=1,
                period="day",
                emotion="warm",
                interest="history",
                satisfaction=4,
            )

        self.assertEqual(result["total"], 3)
        self.assertEqual(len(result["list"]), 1)
        self.assertEqual(result["page"], 2)
        self.assertEqual(result["page_size"], 1)

    def test_exact_filter_value_is_parameter_bound(self):
        self.add_conversation("normal", emotion="warm")
        self.add_conversation("other", emotion="sad")

        result = self.db.get_conversations(emotion="warm' OR 1=1 --")

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["list"], [])


class ConversationFilterValidationTests(unittest.TestCase):
    def parse_filters(self, args):
        import database

        return database._parse_conversation_filters(args)

    def assert_invalid(self, args):
        filters, error = self.parse_filters(args)
        self.assertIsNone(filters)
        self.assertTrue(error)

    def test_valid_filter_values_are_normalized(self):
        filters, error = self.parse_filters(
            {
                "period": " week ",
                "emotion": " warm ",
                "interest": " history ",
                "satisfaction": "5",
            }
        )

        self.assertIsNone(error)
        self.assertEqual(
            filters,
            {"period": "week", "emotion": "warm", "interest": "history", "satisfaction": 5},
        )

    def test_invalid_period_and_satisfaction_return_errors(self):
        self.assert_invalid({"period": "year"})
        self.assert_invalid({"satisfaction": "0"})
        self.assert_invalid({"satisfaction": "6"})
        self.assert_invalid({"satisfaction": "five"})
        self.assert_invalid({"satisfaction": "1.0"})


if __name__ == "__main__":
    unittest.main()
