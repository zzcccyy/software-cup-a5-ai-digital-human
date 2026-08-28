import importlib
import json
import os
import re
import sqlite3
import sys
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

os.environ.setdefault("APP_ENV", "test")


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True


class NonClosingConnection(sqlite3.Connection):
    def close(self):
        pass

    def real_close(self):
        super().close()


def reset_database_module(db, db_file: Path | None = None):
    db._reset_thread_conns()
    if getattr(db, "_test_conn", None) is not None:
        try:
            db._test_conn.real_close()
        except Exception:
            pass
    conn = sqlite3.connect(":memory:", factory=NonClosingConnection, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    db._test_conn = conn
    db.get_conn = lambda: conn
    db.get_read_conn = lambda: conn
    db.invalidate_caches()
    db.init_db()


def fake_answer(reply="stub reply"):
    return {
        "reply": reply,
        "emotion": "delighted",
        "emotionPayload": {"label": "delighted", "cssClass": "emotion-delighted"},
        "actions": [{"type": "gesture", "startMs": 0, "endMs": 800, "priority": 1}],
        "emotionState": {"primary": "joy", "intensity": 0.7},
        "expression": {"happy": 0.5},
        "route": {"id": "mock-route", "name": "mock route", "stops": []},
        "sources": [{"title": "mock source", "content": "offline evidence"}],
        "topics": ["mock"],
        "audioUrl": None,
        "answerMode": "mock",
        "position": {},
        "confidence": 0.99,
        "faqMatch": None,
    }


class EmotionEngineUnitTests(unittest.TestCase):
    def test_sentiment_branches_with_stub_keywords(self):
        import emotion_engine

        original = emotion_engine.SENTIMENT_KEYWORDS
        try:
            emotion_engine.SENTIMENT_KEYWORDS = {
                "strong_positive": ["excellent"],
                "positive": ["good"],
                "slight_positive": ["ok"],
                "slight_negative": ["meh"],
                "negative": ["bad"],
                "strong_negative": ["awful"],
                "concern": ["lost"],
                "urgent": ["urgent"],
                "curiosity": ["why"],
                "appreciation": ["thanks"],
            }
            self.assertEqual(emotion_engine.analyze_sentiment_score("")["label"], "neutral")
            self.assertEqual(emotion_engine.analyze_sentiment_score("excellent thanks")["label"], "strong_positive")
            self.assertEqual(emotion_engine.analyze_sentiment_score("bad lost")["label"], "negative")
            self.assertEqual(emotion_engine.analyze_sentiment_score("awful bad")["label"], "strong_negative")
            self.assertEqual(emotion_engine.analyze_sentiment_score("why good")["mood"], "joy")
        finally:
            emotion_engine.SENTIMENT_KEYWORDS = original

    def test_intent_and_action_timeline(self):
        import emotion_engine

        self.assertEqual(emotion_engine.detect_user_intent("hello guide"), "greeting")
        self.assertEqual(emotion_engine.detect_user_intent("bye"), "farewell")
        self.assertIsNone(emotion_engine.detect_user_intent("plain text"))
        timeline = emotion_engine.build_action_timeline(
            [{"type": "wave", "priority": 2}],
            ["nod"],
            "hello",
            2500,
            "greeting",
        )
        self.assertTrue(any(item["type"] == "wave" for item in timeline))
        self.assertTrue(all(item["endMs"] > item["startMs"] for item in timeline))


class EmotionStateUnitTests(unittest.TestCase):
    def test_expression_mapping_and_decay(self):
        import emotion_state

        sm = emotion_state.EmotionStateMachine(persona_bias="trust", decay_rate=0.05)
        sm.update_from_llm("joy", "trust", 0.8)
        self.assertEqual(sm.current.primary, "joy")
        self.assertIn("happy", sm.get_expression_blend())
        sm._last_update = time.time() - 80
        before = sm.current.intensity
        sm.decay_step()
        self.assertLessEqual(sm.current.intensity, before)
        self.assertGreaterEqual(sm.current.intensity, 0.2)

    def test_invalid_emotion_is_ignored(self):
        import emotion_state

        sm = emotion_state.EmotionStateMachine()
        before = sm.current.to_dict()
        sm.update_from_llm("invalid", None, 1.0)
        self.assertEqual(sm.current.to_dict(), before)


class GroundingUnitTests(unittest.TestCase):
    def test_grounding_supported_unsupported_and_missing_context(self):
        import grounding_check

        original = grounding_check._NUMERIC_PATTERN
        try:
            grounding_check._NUMERIC_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(米|元|小时)")
            knowledge = [{"content": "大佛高度 88米，成人票 210元。"}]
            supported = grounding_check.verify_grounding("大佛高 88米，门票 210元。", knowledge)
            unsupported = grounding_check.verify_grounding("大佛高 99米。", knowledge)
            missing = grounding_check.verify_grounding("大佛高 88米。", [])
            self.assertTrue(supported["consistent"])
            self.assertEqual(supported["checked_count"], 2)
            self.assertFalse(unsupported["consistent"])
            self.assertEqual(unsupported["suspicious_facts"], ["99米"])
            self.assertEqual(missing["checked_count"], 0)
        finally:
            grounding_check._NUMERIC_PATTERN = original

    def test_default_chinese_fact_extraction_handles_meter_claim(self):
        import grounding_check

        result = grounding_check.verify_grounding("大佛高 88米。", [{"content": "大佛高 88米。"}])
        self.assertEqual(result["checked_count"], 1)
        self.assertTrue(result["consistent"])


class QueryRewriteUnitTests(unittest.TestCase):
    def test_rewrite_paths_and_cache(self):
        import query_rewriter

        query_rewriter._rewrite_cache.clear()
        with mock.patch.object(query_rewriter, "_call_llm_for_rewrite", return_value="灵山 大佛 门票") as fake_llm:
            self.assertEqual(query_rewriter.rewrite_query("门票"), "门票")
            verbose = "我想问一下灵山大佛的门票价格大概是多少呢，顺便也想知道老人学生有没有优惠政策"
            self.assertEqual(query_rewriter.rewrite_query(verbose), "灵山 大佛 门票")
            self.assertEqual(query_rewriter.rewrite_query(verbose), "灵山 大佛 门票")
            self.assertEqual(fake_llm.call_count, 1)

        query_rewriter._rewrite_cache.clear()
        with mock.patch.object(query_rewriter, "_call_llm_for_rewrite", return_value=None):
            raw = "请问一下这个地方有没有适合老人慢慢逛的路线安排，最好不要太累也不要走太久"
            self.assertEqual(query_rewriter.rewrite_query(raw), raw)


class RagVectorUnitTests(unittest.TestCase):
    def test_keyword_fallback_normal_empty_and_vector_exception(self):
        import database
        import rag_vector

        sample = [
            {"id": "1", "title": "ticket", "category": "service", "tags": ["price"], "content": "adult ticket price 210 yuan", "source": "test"},
            {"id": "2", "title": "route", "category": "tour", "tags": ["family"], "content": "family route", "source": "test"},
        ]
        with mock.patch.object(database, "get_all_knowledge", return_value=sample):
            result = rag_vector._fallback_keyword_search("ticket price", top_k=2)
            self.assertEqual(result[0]["id"], "1")
            self.assertEqual(rag_vector._fallback_keyword_search("", top_k=2), [])

        old_using, old_collection = rag_vector._using_vector, rag_vector._chroma_collection
        try:
            rag_vector._using_vector = True
            rag_vector._chroma_collection = mock.Mock()
            rag_vector._chroma_collection.query.side_effect = RuntimeError("vector down")
            with mock.patch.object(rag_vector, "_fallback_keyword_search", return_value=[{"id": "fallback"}]):
                self.assertEqual(rag_vector.search_knowledge_vector("anything")[0]["id"], "fallback")
        finally:
            rag_vector._using_vector = old_using
            rag_vector._chroma_collection = old_collection


class AmapUnitTests(unittest.TestCase):
    def test_spot_coordinate_table_shape(self):
        import amap_service

        self.assertGreaterEqual(len(amap_service.SPOT_COORDS), 10)
        name, coord = next(iter(amap_service.SPOT_COORDS.items()))
        self.assertIsInstance(name, str)
        self.assertIn("lat", coord)
        self.assertIn("lng", coord)
        self.assertIsInstance(coord["lat"], float)
        self.assertIsInstance(coord["lng"], float)


class DatabaseUnitTests(unittest.TestCase):
    def setUp(self):
        import database

        self.db = database
        reset_database_module(database)

    def tearDown(self):
        if getattr(self.db, "_test_conn", None) is not None:
            self.db._test_conn.real_close()
            self.db._test_conn = None

    def test_knowledge_faq_conversation_and_operation_log_crud(self):
        item = self.db.add_knowledge("Test title", "cat", ["tag"], "Test content", "unit")
        self.assertEqual(self.db.get_knowledge("Test")["total"], 1)
        self.assertTrue(self.db.update_knowledge(item["id"], title="Updated title"))
        self.assertEqual(self.db.get_knowledge("Updated")["list"][0]["title"], "Updated title")
        self.assertTrue(self.db.delete_knowledge(item["id"]))
        self.assertEqual(self.db.get_knowledge("Updated")["total"], 0)

        faq = self.db.add_faq("Question?", "Answer.", "cat", ["kw"])
        self.assertTrue(self.db.update_faq(faq["id"], answer="Updated answer."))
        self.assertIn("Updated", self.db.get_faq(use_cache=False)[0]["answer"])
        self.assertTrue(self.db.delete_faq(faq["id"]))

        conv = self.db.add_conversation("s1", "guest", "msg", "reply", "joy", "history", ["topic"], latency_ms=12)
        self.assertTrue(self.db.update_conversation_satisfaction(conv["id"], 5))
        self.assertEqual(self.db.get_conversations()["total"], 1)

        self.db.add_operation_log("admin", "create", "knowledge", "rid", "detail", "127.0.0.1")
        logs = self.db.get_operation_logs(action="create", resource="knowledge")
        self.assertEqual(logs["total"], 1)
        self.assertEqual(logs["list"][0]["resource_id"], "rid")


class FlaskFunctionalTests(unittest.TestCase):
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

    def login(self):
        resp = self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["data"]["token"]

    def test_health_and_public_pages(self):
        self.assertEqual(self.client.get("/api/v1/health").get_json()["status"], "ok")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/data-screen").status_code, 200)

    def test_chat_text_equivalence_and_boundaries(self):
        with mock.patch.object(self.main, "generate_answer", return_value=fake_answer("offline text reply")):
            ok = self.client.post("/api/v1/chat/text", json={"message": "hello", "sessionId": "s-chat"})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.get_json()["code"], 0)
            empty = self.client.post("/api/v1/chat/text", json={"message": ""})
            self.assertEqual(empty.status_code, 400)
            long_msg = "x" * 5000
            long_resp = self.client.post("/api/v1/chat/text", json={"message": long_msg, "sessionId": "s-long"})
            self.assertEqual(long_resp.status_code, 200)

    def test_text_stream_sse_local_path(self):
        context = {
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
        with mock.patch.object(self.main, "build_dialog_context", return_value=context), \
             mock.patch.object(self.main, "should_use_llm", return_value=False), \
             mock.patch.object(self.main, "_is_tts_enabled", return_value=False):
            resp = self.client.post("/api/v1/chat/text-stream", json={"message": "stream", "sessionId": "s-stream"})
            body = resp.get_data(as_text=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("event: status", body)
            self.assertIn("event: text", body)
            self.assertIn("event: done", body)

    def test_voice_upload_boundaries_and_asr_failure(self):
        with mock.patch.object(self.main, "transcribe_audio", return_value="voice text"), \
             mock.patch.object(self.main, "generate_answer", return_value=fake_answer("voice reply")):
            transcript = self.client.post(
                "/api/v1/chat/transcribe-upload",
                data={"file": (BytesIO(b"audio"), "voice.webm")},
                content_type="multipart/form-data",
            )
            self.assertEqual(transcript.status_code, 200)
            self.assertEqual(transcript.get_json()["data"]["text"], "voice text")
            resp = self.client.post(
                "/api/v1/chat/voice-upload",
                data={"file": (BytesIO(b"audio"), "voice.wav")},
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json()["code"], 0)

        self.assertEqual(self.client.post("/api/v1/chat/voice-upload", data={}, content_type="multipart/form-data").status_code, 400)
        bad = self.client.post(
            "/api/v1/chat/voice-upload",
            data={"file": (BytesIO(b"x"), "voice.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(bad.status_code, 400)

        with mock.patch.object(self.main, "transcribe_audio", side_effect=RuntimeError("asr down")):
            fail = self.client.post(
                "/api/v1/chat/voice-upload",
                data={"file": (BytesIO(b"audio"), "voice.wav")},
                content_type="multipart/form-data",
            )
            self.assertEqual(fail.status_code, 500)

    def test_scenic_routes_navigation_and_admin_decision_table(self):
        self.assertEqual(self.client.get("/api/v1/scenic/brief").get_json()["code"], 0)
        routes = self.client.get("/api/v1/scenic/routes").get_json()["data"]
        self.assertIsInstance(routes, list)

        spot_name = next(iter(self.main.amap_service.SPOT_COORDS.keys()))
        nav = self.client.post("/api/v1/navigation/query", json={"message": spot_name})
        self.assertEqual(nav.status_code, 200)
        self.assertEqual(nav.get_json()["code"], 0)
        invalid = self.client.post("/api/v1/navigation/query", json={"message": "not-a-real-place"})
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.get_json()["code"], 400)

        self.assertEqual(self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/admin/auth/login", json={"username": "", "password": "admin123"}).status_code, 401)
        token = self.login()
        self.assertTrue(token)

    def test_feedback_bounds_and_admin_pagination_validation(self):
        conv = self.main.db.add_conversation(
            "feedback-session", "guest", "message", "reply", "warm", "history", [], latency_ms=1
        )
        invalid = self.client.post(
            "/api/v1/feedback",
            json={"conversationId": conv["id"], "sessionId": "feedback-session", "satisfaction": 6},
        )
        self.assertEqual(invalid.status_code, 400)
        valid = self.client.post(
            "/api/v1/feedback",
            json={"conversationId": conv["id"], "sessionId": "feedback-session", "satisfaction": 5},
        )
        self.assertEqual(valid.status_code, 200)
        wrong_session = self.client.post(
            "/api/v1/feedback",
            json={"conversationId": conv["id"], "sessionId": "other", "satisfaction": 4},
        )
        self.assertEqual(wrong_session.status_code, 404)

        headers = {"X-ADMIN-TOKEN": self.login()}
        malformed = self.client.get("/api/v1/admin/conversations?page=nope", headers=headers)
        self.assertEqual(malformed.status_code, 400)
        unlimited = self.client.get("/api/v1/admin/knowledge?page=1&page_size=-1", headers=headers)
        self.assertEqual(unlimited.status_code, 400)

    def test_admin_knowledge_data_screen_security_and_stability(self):
        unauthorized = self.client.get("/api/v1/admin/dashboard/overview")
        self.assertEqual(unauthorized.status_code, 401)
        injection = self.client.post("/api/v1/admin/auth/login", json={"username": "admin' OR '1'='1", "password": "x"})
        self.assertEqual(injection.status_code, 401)
        traversal = self.client.get("/static/audio/../admin_data/scenic.db")
        self.assertEqual(traversal.status_code, 404)

        token = self.login()
        headers = {"X-ADMIN-TOKEN": token}
        created = self.client.post(
            "/api/v1/admin/knowledge",
            headers=headers,
            json={"title": "Course test", "category": "test", "tags": ["qa"], "content": "course content", "source": "test"},
        )
        self.assertEqual(created.status_code, 200)
        item_id = created.get_json()["data"]["id"]
        listed = self.client.get("/api/v1/admin/knowledge?search=Course&page=1&page_size=5", headers=headers)
        self.assertEqual(listed.get_json()["data"]["total"], 1)
        updated = self.client.put(f"/api/v1/admin/knowledge/{item_id}", headers=headers, json={"title": "Course updated"})
        self.assertEqual(updated.get_json()["code"], 0)
        deleted = self.client.delete(f"/api/v1/admin/knowledge/{item_id}", headers=headers)
        self.assertEqual(deleted.get_json()["code"], 0)

        overview = self.client.get("/api/v1/data-screen/overview", headers=headers)
        self.assertEqual(overview.status_code, 200)
        with mock.patch.object(self.main.deep_report, "compute_deep_report", return_value={"hotSpots": [], "topics": []}):
            deep = self.client.get("/api/v1/data-screen/deep", headers=headers)
            self.assertEqual(deep.status_code, 200)

        for idx in range(20):
            self.main.db.add_operation_log("admin", "loop", "stability", str(idx), "ok", "127.0.0.1")
        logs = self.main.db.get_operation_logs(action="loop")
        self.assertGreaterEqual(logs["total"], 20)

    def test_basic_performance_mocked_external_services(self):
        with mock.patch.object(self.main, "generate_answer", return_value=fake_answer("perf reply")):
            targets = [
                ("GET", "/api/v1/health", None, {}),
                ("POST", "/api/v1/chat/text", {"message": "perf", "sessionId": "s-perf"}, {}),
            ]
            token = self.login()
            targets.append(("GET", "/api/v1/admin/dashboard/overview", None, {"X-ADMIN-TOKEN": token}))
            for method, url, payload, headers in targets:
                durations = []
                ok_count = 0
                for _ in range(10):
                    start = time.perf_counter()
                    if method == "GET":
                        resp = self.client.get(url, headers=headers)
                    else:
                        resp = self.client.post(url, json=payload, headers=headers)
                    durations.append((time.perf_counter() - start) * 1000)
                    if resp.status_code < 500:
                        ok_count += 1
                self.assertEqual(ok_count, 10)
                self.assertLess(max(durations), 2000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
