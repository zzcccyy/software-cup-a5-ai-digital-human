import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("APP_ENV", "test")

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class NonClosingConnection(sqlite3.Connection):
    def close(self):
        pass

    def real_close(self):
        super().close()


def reset_database_module(db):
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


class AvatarModelDatabaseTests(unittest.TestCase):
    def setUp(self):
        import database

        reset_database_module(database)
        self.db = database
        self.models_dir = Path(tempfile.mkdtemp())
        for name in ("model-a.vrm", "model-b.vrm"):
            (self.models_dir / name).write_bytes(b"vrm")
        self.db.upsert_guide_preset("model-a.vrm", "voice-a", "outfit-a", "style-a", "warm")
        self.db.upsert_guide_preset("model-b.vrm", "voice-b", "outfit-b", "style-b", "calm")

    def test_model_enabled_state_persists_and_cannot_disable_last_enabled_model(self):
        self.assertTrue(all(item["enabled"] for item in self.db.get_vrm_models(self.models_dir)))

        updated = self.db.set_vrm_model_enabled("model-a.vrm", False, self.models_dir)
        self.assertFalse(updated["enabled"])
        self.assertFalse(next(item for item in self.db.get_vrm_models(self.models_dir) if item["name"] == "model-a.vrm")["enabled"])

        with self.assertRaises(ValueError):
            self.db.set_vrm_model_enabled("model-b.vrm", False, self.models_dir)
        self.assertTrue(next(item for item in self.db.get_vrm_models(self.models_dir) if item["name"] == "model-b.vrm")["enabled"])

    def test_legacy_guide_presets_migrate_to_enabled_by_default(self):
        conn = self.db.get_conn()
        conn.execute("DROP TABLE guide_presets")
        conn.execute(
            "CREATE TABLE guide_presets (id TEXT PRIMARY KEY, model_name TEXT NOT NULL UNIQUE, voice TEXT NOT NULL DEFAULT '', outfit TEXT NOT NULL DEFAULT '', style TEXT NOT NULL DEFAULT '', expression_bias TEXT NOT NULL DEFAULT 'warm', updated_at TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO guide_presets (id, model_name, voice) VALUES ('legacy', 'model-a.vrm', 'legacy-voice')")
        conn.commit()
        self.db.init_db()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(guide_presets)").fetchall()}
        self.assertIn("enabled", columns)
        self.assertEqual(self.db.get_guide_preset("model-a.vrm")["enabled"], 1)

    def test_seeded_default_models_all_use_gentle_female_voice(self):
        conn = self.db.get_conn()
        conn.execute("DELETE FROM guide_presets")
        conn.commit()

        self.db.seed_guide_presets()

        presets = {item["model_name"]: item for item in self.db.get_guide_presets()}
        self.assertEqual(
            {"景.vrm", "区.vrm", "灵.vrm", "山.vrm"},
            set(presets),
        )
        self.assertEqual({"温柔女声"}, {item["voice"] for item in presets.values()})

    def test_seeding_normalizes_existing_default_model_voices(self):
        conn = self.db.get_conn()
        conn.execute("DELETE FROM guide_presets")
        for model_name, voice in (
            ("景.vrm", "温柔女声"),
            ("区.vrm", "活泼少女"),
            ("灵.vrm", "稳重男声"),
            ("山.vrm", "阳光男声"),
        ):
            self.db.upsert_guide_preset(model_name, voice, "outfit", "style", "warm")
        self.db.upsert_guide_preset("custom.vrm", "自定义声音", "custom-outfit", "custom-style", "calm")
        conn.execute("UPDATE guide_presets SET enabled=0 WHERE model_name='灵.vrm'")
        conn.commit()

        before = {
            item["model_name"]: {
                key: item[key]
                for key in ("id", "model_name", "outfit", "style", "expression_bias", "enabled", "updated_at")
            }
            for item in self.db.get_guide_presets()
        }

        self.db.seed_guide_presets()

        after = {
            item["model_name"]: {
                key: item[key]
                for key in ("id", "model_name", "outfit", "style", "expression_bias", "enabled", "updated_at")
            }
            for item in self.db.get_guide_presets()
        }
        for model_name in ("景.vrm", "区.vrm", "灵.vrm", "山.vrm"):
            self.assertEqual(before[model_name], after[model_name])
        self.assertEqual(before["custom.vrm"], after["custom.vrm"])
        self.assertEqual(
            {"温柔女声"},
            {
                item["voice"]
                for item in self.db.get_guide_presets()
                if item["model_name"] in {"景.vrm", "区.vrm", "灵.vrm", "山.vrm"}
            },
        )

    def test_model_list_exposes_fixed_guide_attributes(self):
        model = next(item for item in self.db.get_vrm_models(self.models_dir) if item["name"] == "model-a.vrm")
        self.assertEqual(
            {key: model[key] for key in ("enabled", "voice", "outfit", "style", "expressionBias")},
            {"enabled": True, "voice": "voice-a", "outfit": "outfit-a", "style": "style-a", "expressionBias": "warm"},
        )

    def test_enabling_all_models_is_persisted_in_one_operation(self):
        self.db.set_vrm_model_enabled("model-a.vrm", False, self.models_dir)
        self.db.set_vrm_models_enabled(True, self.models_dir)
        self.assertTrue(all(item["enabled"] for item in self.db.get_vrm_models(self.models_dir)))

    def test_enabling_all_models_migrates_legacy_database_on_demand(self):
        conn = self.db.get_conn()
        conn.execute("DROP TABLE guide_presets")
        conn.execute(
            "CREATE TABLE guide_presets (id TEXT PRIMARY KEY, model_name TEXT NOT NULL UNIQUE, voice TEXT NOT NULL DEFAULT '', outfit TEXT NOT NULL DEFAULT '', style TEXT NOT NULL DEFAULT '', expression_bias TEXT NOT NULL DEFAULT 'warm', updated_at TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO guide_presets (id, model_name) VALUES ('legacy-a', 'model-a.vrm')")
        conn.execute("INSERT INTO guide_presets (id, model_name) VALUES ('legacy-b', 'model-b.vrm')")
        conn.commit()

        self.db.set_vrm_models_enabled(True, self.models_dir)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(guide_presets)").fetchall()}
        self.assertIn("enabled", columns)
        self.assertTrue(all(item["enabled"] for item in self.db.get_vrm_models(self.models_dir)))

    def test_public_config_uses_each_requested_models_fixed_voice(self):
        config_a = self.db.get_avatar_public_config(self.models_dir, model_id="model-a.vrm")
        config_b = self.db.get_avatar_public_config(self.models_dir, model_id="model-b.vrm")
        self.assertEqual((config_a["modelId"], config_a["voice"]), ("model-a.vrm", "voice-a"))
        self.assertEqual((config_b["modelId"], config_b["voice"]), ("model-b.vrm", "voice-b"))


class AvatarModelApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import database

        reset_database_module(database)
        cls.models_dir = Path(tempfile.mkdtemp())
        for name in ("api-a.vrm", "api-b.vrm"):
            (cls.models_dir / name).write_bytes(b"vrm")
        database.upsert_guide_preset("api-a.vrm", "voice-a", "outfit-a", "style-a", "warm")
        database.upsert_guide_preset("api-b.vrm", "voice-b", "outfit-b", "style-b", "calm")
        sys.modules.pop("main", None)
        cls.main = importlib.import_module("main")
        cls.main.app.config.update(TESTING=True)
        cls.client = cls.main.app.test_client()
        cls.original_model_dir = cls.main.MODEL_DIR
        cls.main.MODEL_DIR = cls.models_dir

    @classmethod
    def tearDownClass(cls):
        cls.main.MODEL_DIR = cls.original_model_dir
        if getattr(cls.main.db, "_test_conn", None) is not None:
            cls.main.db._test_conn.real_close()
            cls.main.db._test_conn = None

    def test_public_model_list_returns_only_enabled_models(self):
        self.main.db.set_vrm_model_enabled("api-b.vrm", False, self.models_dir)
        response = self.client.get("/api/v1/avatar/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["modelId"] for item in response.get_json()["data"]], ["api-a.vrm"])

    def test_invalid_and_disabled_model_ids_are_rejected_without_fallback(self):
        for model_id in ("missing.vrm", "api-b.vrm"):
            if model_id == "api-b.vrm":
                self.main.db.set_vrm_model_enabled(model_id, False, self.models_dir)
            response = self.client.post("/api/v1/chat/text", json={"message": "你好", "modelId": model_id})
            self.assertEqual(response.status_code, 400)
            self.assertIn("modelId", response.get_json()["message"])

    def test_two_chat_requests_keep_their_requested_model_ids(self):
        calls = []

        def fake_generate(message, interest, session_id, gps_coords=None, model_id=None, **kwargs):
            calls.append((session_id, model_id))
            return {
                "reply": "ok", "emotion": "warm", "emotionPayload": {}, "actions": [],
                "emotionState": {}, "expression": {}, "topics": [], "route": {},
                "sources": [], "answerMode": "test", "audioUrl": None, "position": {},
                "confidence": 1, "faqMatch": None,
            }

        with mock.patch.object(self.main, "generate_answer", side_effect=fake_generate):
            for sid, model_id in (("s1", "api-a.vrm"), ("s2", "api-b.vrm")):
                self.main.db.set_vrm_model_enabled(model_id, True, self.models_dir)
                response = self.client.post("/api/v1/chat/text", json={"message": "你好", "sessionId": sid, "modelId": model_id})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [("s1", "api-a.vrm"), ("s2", "api-b.vrm")])


class AvatarModelAdminApiTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        import blueprints.admin_content as admin_content
        import database

        reset_database_module(database)
        self.db = database
        self.models_dir = Path(tempfile.mkdtemp())
        (self.models_dir / "admin-model.vrm").write_bytes(b"vrm")
        self.db.upsert_guide_preset("admin-model.vrm", "voice", "outfit", "style", "warm")
        self.admin_content = admin_content
        self.app = Flask(__name__)
        self.app.register_blueprint(admin_content.bp)
        self.client = self.app.test_client()

    def test_admin_changes_only_enabled_and_rejects_fixed_property_writes(self):
        import blueprints.admin_core as admin_core

        with mock.patch.object(admin_core, "get_admin_session", return_value={"username": "admin"}), mock.patch.object(
            self.admin_content, "MODEL_DIR", self.models_dir
        ):
            response = self.client.put(
                "/api/v1/admin/avatar/models/status",
                json={"modelId": "admin-model.vrm", "enabled": True},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(self.db.get_vrm_model("admin-model.vrm", self.models_dir)["enabled"])

            for url, method, payload in (
                ("/api/v1/admin/avatar", "put", {"profiles": []}),
                ("/api/v1/admin/guide-presets", "post", {"model_name": "admin-model.vrm", "voice": "changed"}),
                ("/api/v1/admin/guide-presets/batch", "put", {"presets": []}),
            ):
                response = getattr(self.client, method)(url, json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.db.get_guide_preset("admin-model.vrm")["voice"], "voice")

    def test_admin_can_enable_all_models_but_cannot_bulk_disable(self):
        import blueprints.admin_core as admin_core

        with mock.patch.object(admin_core, "get_admin_session", return_value={"username": "admin"}), mock.patch.object(
            self.admin_content, "MODEL_DIR", self.models_dir
        ):
            response = self.client.put(
                "/api/v1/admin/avatar/models/status/batch",
                json={"enabled": True},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["data"][0]["enabled"])

            response = self.client.put(
                "/api/v1/admin/avatar/models/status/batch",
                json={"enabled": False},
            )
            self.assertEqual(response.status_code, 400)

class AvatarModelContractTests(unittest.TestCase):
    def test_admin_and_tourist_contracts_are_model_selection_only(self):
        admin_source = (ROOT / "backend/blueprints/admin_content.py").read_text(encoding="utf-8")
        admin_js = (ROOT / "admin/app.js").read_text(encoding="utf-8")
        admin_html = (ROOT / "admin/index.html").read_text(encoding="utf-8")
        tourist_js = (ROOT / "tourist-client/app.js").read_text(encoding="utf-8")
        tourist_html = (ROOT / "tourist-client/index.html").read_text(encoding="utf-8")

        self.assertIn("set_vrm_model_enabled", admin_source)
        self.assertIn("数字人固定属性只读", admin_source)
        self.assertIn("声音、服装、语气和表情固定属性不可修改", admin_source)
        self.assertIn("模型固定属性不可删除", admin_source)
        self.assertIn("模型固定属性不可批量修改", admin_source)
        self.assertIn("/models/status/batch", admin_source)
        self.assertIn("guide-preset", admin_source)
        self.assertIn("modelId", admin_js)
        self.assertNotIn('id="btn-save-avatar"', admin_html)
        self.assertNotIn('id="btn-upload-vrm"', admin_html)
        self.assertNotIn("数字人预览", admin_html)
        self.assertNotIn("详细参数", admin_html)
        self.assertIn("声音（固定）", admin_html)
        self.assertIn("样子/服装（固定）", admin_html)
        self.assertIn("语气风格（固定）", admin_html)
        self.assertIn('id="btn-enable-all-vrm"', admin_html)
        self.assertIn("/admin/avatar/models/status/batch", admin_js)
        self.assertNotIn("assetUrl('灵.vrm')", tourist_html)
        self.assertIn("sessionStorage", tourist_js)
        self.assertNotIn("localStorage", tourist_js)
        self.assertIn('modelId:', tourist_js)
        self.assertIn('id="avatar-model-select"', tourist_html)
        self.assertIn("选择数字人风格", tourist_html)
        self.assertNotIn("选择数字人模型", tourist_html)
        self.assertIn("getAvatarStyleLabel", tourist_js)
        self.assertIn("AVATAR_STYLE_LABELS_BY_MODEL_ID", tourist_js)
        for model_name in ("景.vrm", "区.vrm", "灵.vrm", "山.vrm"):
            self.assertIn(f'"{model_name}"', tourist_js)
        for style_name in (
            "新中式·亲和导览",
            "现代休闲·活力互动",
            "传统汉服·文化讲解",
            "户外山野·文雅导览",
        ):
            self.assertIn(style_name, tourist_js)
        self.assertNotIn("model.name || model.modelId", tourist_js)
        self.assertIn("containsModelFilename", tourist_js)
        self.assertNotIn("error?.message", tourist_html)
        self.assertIn("if (previousRoot) loadingEl.style.display = 'none';", tourist_html)


if __name__ == "__main__":
    unittest.main()
