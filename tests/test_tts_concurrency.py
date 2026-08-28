import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True

import ai_service  # noqa: E402
import tts_service  # noqa: E402

FAKE_HASH = "0123456789abcdef"
SHORT_TEXT = "你好,请问灵山胜境怎么走?"


class TtsConcurrencyUnitTests(unittest.TestCase):
    """P3.2: per-hash single-flight 锁 + 原子写盘 + 失败不误删他人缓存."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audio_dir = Path(self.tmp.name)
        self.patch_audio = mock.patch.object(ai_service, "AUDIO_DIR", self.audio_dir)
        self.patch_audio.start()
        self.addCleanup(self.patch_audio.stop)
        ai_service._TTS_LOCKS.clear()

    def _patch_hash(self):
        return mock.patch.object(ai_service, "_cache_hash", return_value=FAKE_HASH)

    def test_concurrent_same_text_synthesizes_once(self):
        """10 并发同文本 → edge-tts 只合成 1 次, 缓存只写一次, 无 .tmp 残留, 锁表无泄漏."""
        expected_url = f"/static/audio/tts_cache_{FAKE_HASH}.mp3"
        with self._patch_hash(), \
             mock.patch.object(ai_service, "_tts_run_with_timeout", return_value=b"\x00" * 500) as m:
            results = []

            def worker():
                results.append(ai_service.synthesize_tts_bytes(SHORT_TEXT))

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(m.call_count, 1)
            for audio, url in results:
                self.assertEqual(url, expected_url)
                self.assertEqual(len(audio), 500)
            cache = self.audio_dir / f"tts_cache_{FAKE_HASH}.mp3"
            self.assertTrue(cache.exists())
            self.assertEqual(cache.stat().st_size, 500)
            # 原子写盘: 无临时文件残留
            self.assertEqual(list(self.audio_dir.glob("*.tmp")), [])
            # 引用计数回收: per-hash 锁表无残留
            self.assertEqual(ai_service._TTS_LOCKS, {})

    def test_concurrent_long_text_synthesizes_each_chunk_once(self):
        """长文本 2 段: 8 并发 → 每个 chunk 只合成 1 次 (共 2 次), 拼接结果完整."""
        self.patch_hash = None  # 长文本各 chunk hash 不同, 不能用固定 hash
        LONG = "灵山胜境" * 900  # 4500 字符 → 2 chunks
        with mock.patch.object(ai_service, "_tts_run_with_timeout", return_value=b"\x00" * 500) as m:
            results = []

            def worker():
                results.append(ai_service.synthesize_tts_bytes(LONG))

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            self.assertEqual(m.call_count, 2)
            for audio, url in results:
                self.assertIsNotNone(audio)
                self.assertEqual(len(audio), 2 * 500)
            parent_hash = ai_service._cache_hash(LONG, ai_service.TTS_VOICE)
            cache = self.audio_dir / f"tts_cache_{parent_hash}.mp3"
            self.assertTrue(cache.exists())
            self.assertEqual(cache.stat().st_size, 2 * 500)
            self.assertEqual(list(self.audio_dir.glob("*.tmp")), [])
            self.assertEqual(ai_service._TTS_LOCKS, {})

    def test_edge_tts_semaphore_restored_after_calls(self):
        with self._patch_hash(), \
             mock.patch.object(ai_service, "_tts_run_with_timeout", return_value=b"\x00" * 500):
            ai_service.synthesize_tts_bytes(SHORT_TEXT)
            ai_service.synthesize_tts_bytes(SHORT_TEXT)
        self.assertEqual(ai_service._EDGE_TTS_SEMAPHORE._value, 4)

    def test_pre_existing_valid_cache_not_deleted_on_failure(self):
        """他人已写好的有效缓存: 即使合成失败也不得误删."""
        cache = self.audio_dir / f"tts_cache_{FAKE_HASH}.mp3"
        cache.write_bytes(b"\x00" * 200)
        with self._patch_hash(), \
             mock.patch.object(ai_service, "_tts_run_with_timeout", return_value=None) as m:
            audio, url = ai_service.synthesize_tts_bytes(SHORT_TEXT)
        self.assertEqual(m.call_count, 0)  # 直接命中缓存, 不再合成
        self.assertIsNotNone(audio)
        self.assertTrue(cache.exists())
        self.assertEqual(cache.stat().st_size, 200)

    def test_stale_file_not_deleted_when_not_created_by_me(self):
        """损坏残留文件 (非本请求创建): 失败时保留, 不误删."""
        stale = self.audio_dir / f"tts_cache_{FAKE_HASH}.mp3"
        stale.write_bytes(b"x")  # 1 字节损坏残留
        with self._patch_hash(), \
             mock.patch.object(ai_service, "_tts_run_with_timeout", return_value=None):
            audio, url = ai_service.synthesize_tts_bytes(SHORT_TEXT)
        self.assertIsNone(audio)
        self.assertTrue(stale.exists())


class TtsServiceConcurrencyTests(unittest.TestCase):
    """P3.2: tts_service.text_to_speech per-hash 锁 + 原子写盘."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.patch_key = mock.patch.object(tts_service, "API_KEY", "fake-key")
        self.patch_key.start()
        self.addCleanup(self.patch_key.stop)
        self.fake_resp = mock.Mock(status_code=200, content=b"\x00" * 500, text="")
        self.patch_post = mock.patch.object(tts_service.requests, "post", return_value=self.fake_resp)
        self.post_mock = self.patch_post.start()
        self.addCleanup(self.patch_post.stop)
        tts_service._TTS_LOCKS.clear()

    def test_concurrent_same_text_calls_api_once(self):
        results = []

        def worker():
            results.append(tts_service.text_to_speech("灵山胜境怎么走?", output_path=self.out))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(self.post_mock.call_count, 1)
        self.assertEqual(len(set(results)), 1)
        self.assertTrue(results[0].startswith("/static/audio/sf_"))
        files = os.listdir(self.out)
        self.assertEqual(len([f for f in files if f.endswith(".mp3")]), 1)
        self.assertEqual([f for f in files if f.endswith(".tmp")], [])
        self.assertEqual(tts_service._TTS_LOCKS, {})


if __name__ == "__main__":
    unittest.main()
