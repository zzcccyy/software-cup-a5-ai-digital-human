import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.dont_write_bytecode = True

import ai_service  # noqa: E402

FAKE_HASH = "0123456789abcdef"
TTS_TEXT = "你好,请问灵山胜境怎么走?"


class TtsFallbackUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audio_dir = Path(self.tmp.name)
        self.patch_audio = mock.patch.object(ai_service, "AUDIO_DIR", self.audio_dir)
        self.patch_hash = mock.patch.object(ai_service, "_cache_hash", return_value=FAKE_HASH)
        self.patch_sleep = mock.patch.object(ai_service.time, "sleep")
        self.patch_audio.start()
        self.patch_hash.start()
        self.patch_sleep.start()
        self.url_path = f"/static/audio/tts_cache_{FAKE_HASH}.mp3"

    def tearDown(self):
        self.patch_sleep.stop()
        self.patch_hash.stop()
        self.patch_audio.stop()
        self.tmp.cleanup()

    def test_fallback_returns_real_sf_url(self):
        """edge-tts 降级产物必须返回真实 sf_ URL, 不能返回从未写入的 tts_cache_ URL."""
        with mock.patch.object(
            ai_service, "synthesize_tts_bytes",
            return_value=(b"\x00" * 200, "/static/audio/sf_0123456789abcdef.mp3"),
        ):
            url = ai_service.synthesize_tts(TTS_TEXT)
        self.assertEqual(url, "/static/audio/sf_0123456789abcdef.mp3")

    def test_fallback_final_degrade_returns_fb_url(self):
        """bytes 全失败时走 SiliconFlow 兜底, 返回兜底 URL."""
        with mock.patch.object(ai_service, "synthesize_tts_bytes", return_value=(None, None)), \
             mock.patch.object(
                 ai_service, "_try_siliconflow_fallback",
                 return_value=(b"\x00" * 200, "/static/audio/sf_0123456789abcdef.mp3"),
             ):
            url = ai_service.synthesize_tts(TTS_TEXT)
        self.assertEqual(url, "/static/audio/sf_0123456789abcdef.mp3")

    def test_normal_success_returns_cache_url_unchanged(self):
        """edge-tts 成功路径: bytes 版返回的 URL 即缓存名, 行为不变."""
        with mock.patch.object(
            ai_service, "synthesize_tts_bytes",
            return_value=(b"\x00" * 200, self.url_path),
        ):
            url = ai_service.synthesize_tts(TTS_TEXT)
        self.assertEqual(url, self.url_path)

    def test_all_engines_failed_returns_none(self):
        with mock.patch.object(ai_service, "synthesize_tts_bytes", return_value=(None, None)), \
             mock.patch.object(ai_service, "_try_siliconflow_fallback", return_value=(None, None)):
            url = ai_service.synthesize_tts(TTS_TEXT)
        self.assertIsNone(url)

    def test_existing_cache_short_circuit(self):
        """缓存命中分支(文件存在才返回)保持不动."""
        cache_file = self.audio_dir / f"tts_cache_{FAKE_HASH}.mp3"
        cache_file.write_bytes(b"\x00" * 200)
        with mock.patch.object(ai_service, "synthesize_tts_bytes") as m:
            url = ai_service.synthesize_tts(TTS_TEXT)
        m.assert_not_called()
        self.assertEqual(url, self.url_path)


class TtsLongTextUnitTests(unittest.TestCase):
    """P0.4: 超长文本分段拼接 — 返回完整音频且 URL 符合 serve_audio 白名单."""

    LONG_TEXT = "灵山胜境" * 900  # 4500 字符 > MAX_TTS_CHARS=3000
    CHUNK_BYTES = b"\x00" * 500

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audio_dir = Path(self.tmp.name)
        self.patch_audio = mock.patch.object(ai_service, "AUDIO_DIR", self.audio_dir)
        self.patch_hash = mock.patch.object(ai_service, "_cache_hash", return_value=FAKE_HASH)
        self.patch_sleep = mock.patch.object(ai_service.time, "sleep")
        self.patch_audio.start()
        self.patch_hash.start()
        self.patch_sleep.start()
        self.url_path = f"/static/audio/tts_cache_{FAKE_HASH}.mp3"
        self.chunk_calls = {"n": 0}

        real_func = ai_service.synthesize_tts_bytes

        def fake(text, voice_name="", tts_tag=None):
            # 顶层超长调用走真实拼接逻辑, 内部 chunk 调用返回 canned bytes
            if len(text) > ai_service.MAX_TTS_CHARS:
                return real_func(text, voice_name=voice_name, tts_tag=tts_tag)
            self.chunk_calls["n"] += 1
            return (self.CHUNK_BYTES, "/static/audio/sf_0123456789abcdef.mp3")

        self.patch_bytes = mock.patch.object(ai_service, "synthesize_tts_bytes", side_effect=fake)
        self.patch_bytes.start()

    def tearDown(self):
        self.patch_bytes.stop()
        self.patch_sleep.stop()
        self.patch_hash.stop()
        self.patch_audio.stop()
        self.tmp.cleanup()

    def test_bytes_long_text_returns_full_audio_and_whitelist_url(self):
        """bytes 版: 拼接后的完整音频写入缓存, 返回 URL 必须能被静态路由服务."""
        audio, url = ai_service.synthesize_tts_bytes(self.LONG_TEXT)
        self.assertIsNotNone(audio)
        self.assertEqual(len(audio), 2 * len(self.CHUNK_BYTES))
        self.assertEqual(url, self.url_path)
        cache_file = self.audio_dir / f"tts_cache_{FAKE_HASH}.mp3"
        self.assertTrue(cache_file.exists())
        self.assertEqual(cache_file.read_bytes(), audio)
        # URL 必须匹配 serve_audio 白名单 (tts_cache_[0-9a-f]{16}.mp3)
        import re
        self.assertIsNotNone(re.fullmatch(r"tts_cache_[0-9a-f]{16}\.mp3", url.rsplit("/", 1)[-1]))

    def test_bytes_long_text_second_call_hits_cache(self):
        """二次调用同文本: 命中拼接缓存, 不再重复合成 chunk."""
        ai_service.synthesize_tts_bytes(self.LONG_TEXT)
        first_calls = self.chunk_calls["n"]
        audio, url = ai_service.synthesize_tts_bytes(self.LONG_TEXT)
        self.assertEqual(self.chunk_calls["n"], first_calls)
        self.assertEqual(len(audio), 2 * len(self.CHUNK_BYTES))
        self.assertEqual(url, self.url_path)

    def test_tts_long_text_returns_whitelist_url(self):
        """synthesize_tts 超长分支: 委托 bytes 版拼接, 返回完整音频 URL."""
        url = ai_service.synthesize_tts(self.LONG_TEXT)
        self.assertEqual(url, self.url_path)
        cache_file = self.audio_dir / f"tts_cache_{FAKE_HASH}.mp3"
        self.assertTrue(cache_file.exists())
        self.assertEqual(cache_file.stat().st_size, 2 * len(self.CHUNK_BYTES))

    def test_tts_long_text_second_call_short_circuits(self):
        """synthesize_tts 二次调用: 缓存命中直接返回, chunk 合成次数不增加."""
        ai_service.synthesize_tts(self.LONG_TEXT)
        first_calls = self.chunk_calls["n"]
        url = ai_service.synthesize_tts(self.LONG_TEXT)
        self.assertEqual(url, self.url_path)
        self.assertEqual(self.chunk_calls["n"], first_calls)

    def test_short_text_regression_no_extra_file(self):
        """短文本(<3000): 走原路径, 不产生多余文件."""
        self.patch_bytes.stop()
        try:
            def _fake_run(coro, timeout=8.0):
                coro.close()
                return self.CHUNK_BYTES

            with mock.patch.object(ai_service, "_tts_run_with_timeout", side_effect=_fake_run):
                audio, url = ai_service.synthesize_tts_bytes("你好世界")
        finally:
            self.patch_bytes.start()
        self.assertEqual(len(audio), len(self.CHUNK_BYTES))
        self.assertEqual(url, self.url_path)
        files = list(self.audio_dir.glob("*.mp3"))
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
