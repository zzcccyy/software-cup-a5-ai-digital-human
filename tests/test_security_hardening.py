import importlib
import os
import sqlite3
import sys
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock
from urllib.parse import quote


os.environ.setdefault("APP_ENV", "test")


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TESTS = ROOT / "tests"
for _p in (BACKEND, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

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


class SecurityHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import database
        import ai_service

        reset_database_module(database)
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

    def setUp(self):
        self.main.LOGIN_ATTEMPTS.clear()

    def login(self, username="admin", password="admin123"):
        resp = self.client.post("/api/v1/admin/auth/login", json={"username": username, "password": password})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["data"]["token"]

    def _restore_admin123(self, token):
        resp = self.client.put(
            "/api/v1/admin/settings",
            json={"admin_password": "admin123"},
            headers={"X-ADMIN-TOKEN": token},
        )
        self.assertEqual(resp.status_code, 200)

    # ---------- P2.1 密码哈希 ----------

    def test_default_password_hashed_and_login_works(self):
        stored = self.main.db.get_settings().get("admin_password", "")
        self.assertTrue(stored.startswith(("scrypt:", "pbkdf2:")), f"not a hash: {stored!r}")
        self.assertNotIn("admin123", stored)
        self.assertEqual(self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "admin123"}).status_code, 200)

    def test_settings_put_hashes_password_and_revokes_sessions(self):
        token = self.login()
        resp = self.client.put("/api/v1/admin/settings", json={"admin_password": "new-secret-9"}, headers={"X-ADMIN-TOKEN": token})
        self.assertEqual(resp.status_code, 200)
        stored = self.main.db.get_settings().get("admin_password", "")
        self.assertTrue(stored.startswith(("scrypt:", "pbkdf2:")), f"not a hash: {stored!r}")
        self.assertNotIn("new-secret-9", stored)
        # 改密后旧 token 立即失效
        resp = self.client.get("/api/v1/admin/settings", headers={"X-ADMIN-TOKEN": token})
        self.assertEqual(resp.status_code, 401)
        # 新密码可登录
        new_token = self.login("admin", "new-secret-9")
        self._restore_admin123(new_token)

    def test_legacy_plaintext_migrated_on_first_login(self):
        self.main.db.update_settings({"admin_password": "legacy-plain"})
        resp = self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "legacy-plain"})
        self.assertEqual(resp.status_code, 200)
        stored = self.main.db.get_settings().get("admin_password", "")
        self.assertTrue(stored.startswith(("scrypt:", "pbkdf2:")), f"not migrated: {stored!r}")
        self.assertNotEqual(stored, "legacy-plain")
        token = resp.get_json()["data"]["token"]
        self._restore_admin123(token)

    def test_production_startup_requires_env_credentials(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ADMIN_USERNAME", None)
            os.environ.pop("ADMIN_PASSWORD", None)
            os.environ["APP_ENV"] = "production"
            with self.assertRaises(RuntimeError):
                self.main.get_admin_credentials()

    def test_undeclared_environment_does_not_backfill_default_password(self):
        self.main.db.update_settings({"admin_password": ""})
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    self.main.get_admin_credentials()
            with mock.patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
                username, credential = self.main.get_admin_credentials()
            self.assertEqual(username, "admin")
            self.assertTrue(credential.startswith(("scrypt:", "pbkdf2:")))
        finally:
            from werkzeug.security import generate_password_hash
            self.main.db.update_settings({"admin_password": generate_password_hash("admin123")})

    def test_existing_default_password_requires_explicit_dev_environment(self):
        from werkzeug.security import generate_password_hash
        self.main.db.update_settings({"admin_password": generate_password_hash("admin123")})
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    self.main.get_admin_credentials()
            with mock.patch.dict(os.environ, {"APP_ENV": "test"}, clear=True):
                self.assertEqual(self.main.get_admin_credentials()[0], "admin")
        finally:
            self.main.db.update_settings({"admin_password": generate_password_hash("admin123")})

    def test_environment_credentials_cannot_be_changed_in_settings(self):
        with mock.patch.dict(
            os.environ,
            {"APP_ENV": "production", "ADMIN_USERNAME": "env-admin", "ADMIN_PASSWORD": "env-pass"},
            clear=True,
        ):
            resp = self.client.post(
                "/api/v1/admin/auth/login",
                json={"username": "env-admin", "password": "env-pass"},
            )
            self.assertEqual(resp.status_code, 200)
            token = resp.get_json()["data"]["token"]
            resp = self.client.put(
                "/api/v1/admin/settings",
                json={"admin_password": "new-env-pass", "aiModel": "siliconflow"},
                headers={"X-ADMIN-TOKEN": token},
            )
            self.assertEqual(resp.status_code, 400)
            username_resp = self.client.put(
                "/api/v1/admin/settings",
                json={"adminUser": "db-admin"},
                headers={"X-ADMIN-TOKEN": token},
            )
            self.assertEqual(username_resp.status_code, 400)
            settings_resp = self.client.put(
                "/api/v1/admin/settings",
                json={"aiModel": "deepseek"},
                headers={"X-ADMIN-TOKEN": token},
            )
            self.assertEqual(settings_resp.status_code, 400)
            self.assertEqual(
                self.client.post(
                    "/api/v1/admin/auth/login",
                    json={"username": "env-admin", "password": "env-pass"},
                ).status_code,
                200,
            )

    def test_bearer_logout_revokes_session(self):
        token = self.login()
        resp = self.client.post("/api/v1/admin/auth/logout", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/v1/admin/settings", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 401)

    # ---------- P2.2 /settings 脱敏 ----------

    def test_settings_get_strips_password(self):
        token = self.login()
        resp = self.client.get("/api/v1/admin/settings", headers={"X-ADMIN-TOKEN": token})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertNotIn("admin_password", data)
        self.assertNotIn("admin_password", resp.get_data(as_text=True))

    def test_settings_get_reports_runtime_status_for_fixed_fields(self):
        import ai_service

        original = self.main.db.get_settings()
        self.main.db.update_settings(
            {
                "aiModel": "stale-db-provider",
                "knowledgeMode": "stale-db-knowledge",
                "responseTargetMs": "999",
                "emotionEngine": "stale-db-emotion",
                "asrMode": "stale-db-asr",
                "ttsEnabled": "False",
            }
        )
        try:
            with mock.patch.object(ai_service, "LLM_PROVIDER", "xunfei"), mock.patch.object(
                ai_service, "ASR_MODEL", "runtime-asr-model"
            ):
                token = self.login()
                resp = self.client.get("/api/v1/admin/settings", headers={"X-ADMIN-TOKEN": token})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()["data"]
            self.assertEqual(data["aiModel"], "xunfei")
            self.assertEqual(data["asrMode"], "SiliconFlow /audio/transcriptions（runtime-asr-model）")
            self.assertEqual(data["knowledgeMode"], "本地景区知识库 + FTS5 + Chroma向量RAG + 资料包自动导入")
            self.assertEqual(data["emotionEngine"], "规则情绪分析 + Plutchik 情绪状态机 + LLM 情绪标签")
            self.assertIsNone(data["responseTargetMs"])
            self.assertEqual(data["ttsEnabled"], "False")
        finally:
            self.main.db.update_settings(original)

    def test_settings_put_rejects_fixed_fields_without_changing_runtime(self):
        import ai_service

        token = self.login()
        original_provider = ai_service.LLM_PROVIDER
        original_settings = self.main.db.get_settings()
        payload = {
            "aiModel": "siliconflow",
            "knowledgeMode": "custom",
            "responseTargetMs": 1,
            "emotionEngine": "custom",
            "asrMode": "custom",
            "ttsEnabled": False,
        }
        resp = self.client.put(
            "/api/v1/admin/settings",
            json=payload,
            headers={"X-ADMIN-TOKEN": token},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("只读", resp.get_json().get("message", ""))
        self.assertEqual(ai_service.LLM_PROVIDER, original_provider)
        self.assertEqual(self.main.db.get_settings(), original_settings)
        self.main.db.update_settings({"aiModel": "xunfei"})
        self.main._restore_runtime_settings(self.main.db.get_settings())
        self.assertEqual(ai_service.LLM_PROVIDER, original_provider)
        self.main.db.update_settings(original_settings)

    def test_settings_put_keeps_tts_voice_and_admin_username_editable(self):
        import ai_service

        token = self.login()
        original_voice = ai_service.TTS_VOICE
        original_username = self.main.db.get_settings().get("adminUser")
        try:
            resp = self.client.put(
                "/api/v1/admin/settings",
                json={"ttsVoice": "活泼少女", "adminUser": "operator"},
                headers={"X-ADMIN-TOKEN": token},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(ai_service.TTS_VOICE, ai_service.VOICE_MAP["活泼少女"])
            self.assertEqual(self.main.db.get_settings().get("adminUser"), "operator")
        finally:
            ai_service.TTS_VOICE = original_voice
            self.main.db.update_settings({"adminUser": original_username or "admin"})

    def test_settings_put_rejects_unknown_tts_voice_without_persisting(self):
        token = self.login()
        original = self.main.db.get_settings().get("ttsVoice")
        resp = self.client.put(
            "/api/v1/admin/settings",
            json={"ttsVoice": "not-a-real-voice"},
            headers={"X-ADMIN-TOKEN": token},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.main.db.get_settings().get("ttsVoice"), original)

    def test_startup_restores_persisted_tts_voice(self):
        import ai_service

        original = ai_service.TTS_VOICE
        original_setting = self.main.db.get_settings().get("ttsVoice")
        try:
            self.main.db.update_settings({"ttsVoice": "活泼少女"})
            ai_service.TTS_VOICE = "stale-runtime-voice"
            self.main._restore_runtime_settings(self.main.db.get_settings())
            self.assertEqual(ai_service.TTS_VOICE, ai_service.VOICE_MAP["活泼少女"])
        finally:
            ai_service.TTS_VOICE = original
            if original_setting is None:
                self.main.db.update_settings({"ttsVoice": ""})
            else:
                self.main.db.update_settings({"ttsVoice": original_setting})

    def test_settings_put_whitelist_rejects_unknown_keys(self):
        token = self.login()
        resp = self.client.put(
            "/api/v1/admin/settings",
            json={"someEvilKey": "pwned"},
            headers={"X-ADMIN-TOKEN": token},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("someEvilKey", self.main.db.get_settings())

    def test_public_config_does_not_expose_amap_security_code(self):
        with mock.patch.object(self.main, "_AMAP_KEY", "browser-key"), mock.patch.object(
            self.main, "_AMAP_SECURITY_CODE", "server-only-security-code"
        ):
            resp = self.client.get("/api/v1/config")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertEqual(data, {"amapKey": "browser-key"})
        self.assertNotIn("server-only-security-code", resp.get_data(as_text=True))
        self.assertNotIn("amapSecurityCode", data)

    # ---------- P2.3 serve_vrm 穿越防护 ----------

    def test_serve_vrm_path_traversal_rejected(self):
        bad_paths = [
            "/...vrm",                      # name == ".."
            "/....vrm",                     # name == "..."
            "/%2e%2e.vrm",                  # 编码点号
            "/..%5C..%5C..%5C..%5Cwindows%5Cwin.ini.vrm",  # 编码反斜杠
            "/.env",                        # 点开头隐藏文件
        ]
        for path in bad_paths:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 404, f"expected 404 for {path}")

    def test_serve_vrm_valid_chinese_filename(self):
        vrm_files = list(self.main.MODEL_DIR.glob("*.vrm"))
        if not vrm_files:
            self.skipTest("no .vrm files under MODEL_DIR")
        name = vrm_files[0].stem
        resp = self.client.get(f"/{quote(name)}.vrm")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "model/vrm")

    # ---------- P2.4 错误脱敏与状态码对齐 ----------

    def test_data_screen_errors_sanitized(self):
        import database
        from blueprints import data_screen
        token = self.login()
        with mock.patch.object(database, "compute_dashboard", side_effect=RuntimeError("db-boom-secret")):
            resp = self.client.get("/api/v1/data-screen/overview", headers={"X-ADMIN-TOKEN": token})
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertEqual(body["code"], 500)
        self.assertNotIn("db-boom-secret", resp.get_data(as_text=True))

        with mock.patch.object(data_screen.deep_report, "compute_deep_report", side_effect=RuntimeError("deep-boom-secret")):
            resp = self.client.get("/api/v1/data-screen/deep", headers={"X-ADMIN-TOKEN": token})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["code"], 500)
        self.assertNotIn("deep-boom-secret", resp.get_data(as_text=True))

        with mock.patch.object(database, "get_latest_feedback", side_effect=RuntimeError("fb-boom-secret")):
            resp = self.client.get("/api/v1/data-screen/feedback", headers={"X-ADMIN-TOKEN": token})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["code"], 500)
        self.assertNotIn("fb-boom-secret", resp.get_data(as_text=True))

    def test_weather_error_sanitized(self):
        with mock.patch.object(self.main.requests, "get", side_effect=RuntimeError("amap-key-secret")):
            resp = self.client.get("/api/v1/weather")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["code"], 1)
        self.assertNotIn("amap-key-secret", resp.get_data(as_text=True))

    def test_voice_upload_error_sanitized(self):
        with mock.patch.object(self.main, "transcribe_audio", side_effect=RuntimeError("asr-internal-secret")):
            resp = self.client.post(
                "/api/v1/chat/voice-upload",
                data={"file": (BytesIO(b"audio"), "voice.wav")},
                content_type="multipart/form-data",
            )
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertEqual(body["code"], 500)
        self.assertNotIn("asr-internal-secret", resp.get_data(as_text=True))

    def test_tts_health_error_sanitized(self):
        self.main._startup_error[:] = [RuntimeError("tts-provider-secret")]
        try:
            with mock.patch.object(self.main.ai_service, "_get_tts_loop", return_value=mock.Mock(is_running=lambda: False)):
                resp = self.client.get("/api/v1/health/tts")
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("tts-provider-secret", resp.get_data(as_text=True))
            self.assertEqual(resp.get_json()["data"]["startup_error"], "initialization_failed")
        finally:
            self.main._startup_error.clear()

    # ---------- P2.5 登录限流加固 ----------

    def test_login_rate_limit_compound_key_isolation(self):
        m = self.main
        for _ in range(m.LOGIN_ATTEMPT_LIMIT):
            resp = self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "wrong"})
            self.assertEqual(resp.status_code, 401)
        resp = self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(resp.status_code, 429)
        # 同 IP 另一个用户不受牵连: 仍走 401 而不是 429
        resp = self.client.post("/api/v1/admin/auth/login", json={"username": "other-user", "password": "x"})
        self.assertEqual(resp.status_code, 401)
        # 被锁用户即使密码正确也 429
        resp = self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(resp.status_code, 429)

    def test_login_rate_limit_window_expiry(self):
        m = self.main
        stale = time.time() - m.LOGIN_ATTEMPT_WINDOW_SECONDS - 10
        m.LOGIN_ATTEMPTS["127.0.0.1|admin"] = [stale] * m.LOGIN_ATTEMPT_LIMIT
        resp = self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(resp.status_code, 401)  # 过期桶被 prune, 不触发 429
        bucket = m.LOGIN_ATTEMPTS.get("127.0.0.1|admin", [])
        self.assertEqual(len(bucket), 1)  # 只剩本次失败的记录
        self.assertTrue(time.time() - bucket[0] < m.LOGIN_ATTEMPT_WINDOW_SECONDS)

    def test_successful_login_resets_bucket(self):
        m = self.main
        for _ in range(3):
            self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "wrong"})
        self.assertIn("127.0.0.1|admin", m.LOGIN_ATTEMPTS)
        resp = self.client.post("/api/v1/admin/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("127.0.0.1|admin", m.LOGIN_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
