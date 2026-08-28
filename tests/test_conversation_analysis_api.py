import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("APP_ENV", "test")
sys.dont_write_bytecode = True


class ConversationAnalysisApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import database
        import ai_service

        cls.db = database
        cls.db._reset_thread_conns()
        cls._ai_patches = [
            mock.patch.object(ai_service, "warmup_tts", lambda: None),
            mock.patch.object(ai_service, "synthesize_tts", lambda *args, **kwargs: None),
            mock.patch.object(ai_service, "synthesize_tts_bytes", lambda *args, **kwargs: (None, None)),
        ]
        for patcher in cls._ai_patches:
            patcher.start()
        sys.modules.pop("main", None)
        with mock.patch("threading.Thread.start", lambda self: None):
            cls.main = importlib.import_module("main")
        cls.main.app.config.update(TESTING=True)
        cls.client = cls.main.app.test_client()

    @classmethod
    def tearDownClass(cls):
        for patcher in getattr(cls, "_ai_patches", []):
            patcher.stop()
        if getattr(cls.db, "_test_conn", None) is not None:
            cls.db._test_conn.real_close()
            cls.db._test_conn = None

    def login(self):
        response = self.client.post(
            "/api/v1/admin/auth/login",
            json={"username": "admin", "password": "admin123"},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["data"]["token"]

    def test_analysis_requires_admin_authentication(self):
        response = self.client.post(
            "/api/v1/admin/conversations/analyze",
            json={"filters": {}},
        )

        self.assertEqual(response.status_code, 401)

    def test_analysis_endpoint_validates_request_and_returns_report(self):
        token = self.login()
        expected = {
            "scope": {"totalConversations": 2},
            "metrics": {"totalConversations": 2},
            "meta": {"mode": "deterministic", "warnings": []},
        }
        with mock.patch.object(
            self.main.admin_core.conversation_analysis,
            "analyze_conversations",
            return_value=expected,
        ) as analyze:
            response = self.client.post(
                "/api/v1/admin/conversations/analyze",
                headers={"X-ADMIN-TOKEN": token},
                json={"filters": {"period": "week"}, "sampleLimit": 40},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["code"], 0)
        self.assertEqual(response.get_json()["data"], expected)
        analyze.assert_called_once_with(
            {"period": "week", "emotion": "", "interest": "", "satisfaction": None},
            sample_limit=40,
        )

    def test_analysis_endpoint_rejects_invalid_sample_limit(self):
        token = self.login()
        response = self.client.post(
            "/api/v1/admin/conversations/analyze",
            headers={"X-ADMIN-TOKEN": token},
            json={"filters": {}, "sampleLimit": 1000},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("样本数量", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
