"""Regression contract for the browser audio transcription fallback."""

from __future__ import annotations

from pathlib import Path
import subprocess
import threading
import time
import unittest


MAIN = Path(__file__).resolve().parents[1] / "main.py"
CLIENT = MAIN.parents[1] / "tourist-client" / "app.js"
AI_SERVICE = MAIN.parent / "ai_service.py"


class VoiceTranscriptionContractTests(unittest.TestCase):
    def test_transcription_endpoint_reuses_the_existing_upload_and_asr_handler(self):
        source = MAIN.read_text(encoding="utf-8")

        self.assertIn('@app.route("/api/v1/chat/transcribe-upload", methods=["POST"])', source)
        self.assertIn('if request.path.endswith("/transcribe-upload"):', source)

    def test_audio_route_only_allows_the_two_hashed_mp3_namespaces(self):
        source = MAIN.read_text(encoding="utf-8")

        self.assertIn('re.fullmatch(r"(?:tts_cache_|sf_)[0-9a-f]{16}\\.mp3", safe)', source)

    def test_browser_uses_speech_recognition_then_media_recorder_fallback(self):
        source = CLIENT.read_text(encoding="utf-8")

        self.assertIn("window.MediaRecorder", source)
        self.assertIn('apiUrl("/api/v1/chat/transcribe-upload")', source)
        self.assertIn("startRecorderFallback", source)
        self.assertIn("sendMessage(text)", source)

    def test_segment_player_advances_through_a_failed_middle_audio_slot(self):
        source = CLIENT.read_text(encoding="utf-8")
        audio_event = source[source.index('} else if (eventType === "audio_segment")'):source.index('} catch (e) {', source.index('} else if (eventType === "audio_segment")'))]

        self.assertIn("segmentPlayer.add(data.index, data.audioUrl, data.audioBase64, data.text || '')", audio_event)
        self.assertNotIn("SKIPPED: no audioUrl", audio_event)
        class_start = source.index("class SegmentPlayer")
        self.assertLess(source.index("this.currentIndex++;", class_start), source.index("if (!audioSrc)", class_start))

        class_end = source.index("const segmentPlayer = new SegmentPlayer", class_start)
        player_class = source[class_start:class_end]
        script = """
const window = {};
const state = { speechEnabled: true };
const audioManager = {
  active: false,
  _connectAudio() {},
  _syncLipWithAudio() {},
  getContext() { return { state: 'running' }; },
  playAudio() {},
};
const performance = { now: () => 1 };
const setTimeout = () => 1;
const clearTimeout = () => {};
const setInterval = () => 1;
const clearInterval = () => {};
const normalizeBackendAudioUrl = (url) => url;
const createPlaybackAudio = (src) => {
  const handlers = {};
  return {
    src, ended: false, readyState: 4, networkState: 1, currentTime: 1,
    addEventListener(type, handler) { handlers[type] = handler; },
    play() { return Promise.resolve(); }, pause() {},
    trigger(type) { if (handlers[type]) handlers[type](); },
  };
};
const animateSpeaking = () => {};
""" + player_class + """
const player = new SegmentPlayer();
player.total = 3;
player.add(0, '/first.mp3', null, 'first');
const first = player.currentAudio;
player.add(1, null, null, 'failed');
player.add(2, '/third.mp3', null, 'third');
first.trigger('ended');
if (player.currentIndex !== 3 || !player.currentAudio || player.currentAudio.src !== '/third.mp3') process.exit(1);
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_done_audio_fallback_is_disabled_after_segment_stream_starts(self):
        source = CLIENT.read_text(encoding="utf-8")
        class_start = source.index("class SegmentPlayer")
        class_end = source.index("const segmentPlayer = new SegmentPlayer", class_start)
        player_class = source[class_start:class_end]
        script = """
const window = {};
const state = { speechEnabled: true };
let fallbackCalls = 0;
const audioManager = {
  active: false,
  _connectAudio() {},
  _syncLipWithAudio() {},
  getContext() { return { state: 'running' }; },
  playAudio() { fallbackCalls++; },
};
const performance = { now: () => 1 };
const setTimeout = () => 1;
const clearTimeout = () => {};
const setInterval = () => 1;
const clearInterval = () => {};
const normalizeBackendAudioUrl = (url) => url;
const animateSpeaking = () => {};
""" + player_class + """
const player = new SegmentPlayer();
player._receivedSegmentEvent = true;
player.total = 1;
player.currentIndex = 1;
player.fallbackUrl = '/done-audio.mp3';
player._playNext();
if (fallbackCalls !== 0) process.exit(1);
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_stale_segment_play_promise_cannot_restore_playback_state(self):
        source = CLIENT.read_text(encoding="utf-8")
        class_start = source.index("class SegmentPlayer")
        class_end = source.index("const segmentPlayer = new SegmentPlayer", class_start)
        player_class = source[class_start:class_end]
        script = """
const window = {};
const state = { speechEnabled: true };
let resolvePlay;
const audioManager = {
  active: false,
  _connectAudio() {},
  _syncLipWithAudio() {},
  getContext() { return { state: 'running' }; },
  playAudio() {},
};
const performance = { now: () => 1 };
const setTimeout = () => 1;
const clearTimeout = () => {};
const setInterval = () => 1;
const clearInterval = () => {};
const normalizeBackendAudioUrl = (url) => url;
const createPlaybackAudio = (src) => {
  const handlers = {};
  return {
    src, ended: false, readyState: 4, networkState: 1, currentTime: 0,
    addEventListener(type, handler) { handlers[type] = handler; },
    play() { return new Promise(resolve => { resolvePlay = resolve; }); },
    pause() {},
    trigger(type) { if (handlers[type]) handlers[type](); },
  };
};
const animateSpeaking = () => {};
""" + player_class + """
const player = new SegmentPlayer();
player.total = 1;
player.add(0, '/first.mp3', null, 'first');
const first = player.currentAudio;
first.trigger('error');
resolvePlay();
Promise.resolve().then(() => {
  if (player.playedAny || player.playing || audioManager.active) process.exit(1);
});
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_pending_audio_context_resume_cannot_start_after_player_stop(self):
        source = CLIENT.read_text(encoding="utf-8")
        class_start = source.index("class SegmentPlayer")
        class_end = source.index("const segmentPlayer = new SegmentPlayer", class_start)
        player_class = source[class_start:class_end]
        script = """
const window = {};
const state = { speechEnabled: true };
let resolveResume;
let playCalls = 0;
const context = {
  state: 'suspended',
  resume() { return new Promise(resolve => { resolveResume = resolve; }); },
};
const audioManager = {
  active: false,
  _connectAudio() {},
  _syncLipWithAudio() {},
  getContext() { return context; },
  playAudio() {},
};
const performance = { now: () => 1 };
const setTimeout = () => 1;
const clearTimeout = () => {};
const setInterval = () => 1;
const clearInterval = () => {};
const normalizeBackendAudioUrl = (url) => url;
const createPlaybackAudio = (src) => ({
  src, ended: false, readyState: 4, networkState: 1, currentTime: 0,
  addEventListener() {},
  play() { playCalls++; return Promise.resolve(); },
  pause() {},
});
const animateSpeaking = () => {};
const speak = () => {};
""" + player_class + """
const player = new SegmentPlayer();
player.total = 1;
player.add(0, '/first.mp3', null, 'first');
player.stop();
resolveResume();
Promise.resolve().then(() => {
  if (playCalls !== 0) process.exit(1);
});
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_muting_preserves_the_current_segment_for_resume(self):
        source = CLIENT.read_text(encoding="utf-8")
        class_start = source.index("class SegmentPlayer")
        class_end = source.index("const segmentPlayer = new SegmentPlayer", class_start)
        player_class = source[class_start:class_end]
        script = """
const window = {};
const state = { speechEnabled: true };
let resolvePlay;
const audioManager = {
  active: false,
  _connectAudio() {},
  _syncLipWithAudio() {},
  getContext() { return { state: 'running' }; },
  playAudio() {},
};
const performance = { now: () => 1 };
const setTimeout = () => 1;
const clearTimeout = () => {};
const setInterval = () => 1;
const clearInterval = () => {};
const normalizeBackendAudioUrl = (url) => url;
let resumedAt = 0;
const createPlaybackAudio = (src) => {
  let currentTime = 0;
  return {
    src, ended: false, readyState: 4, networkState: 1,
    get currentTime() { return currentTime; },
    set currentTime(value) { currentTime = value; resumedAt = value; },
    addEventListener() {},
    play() { return new Promise(resolve => { resolvePlay = resolve; }); },
    pause() {},
  };
};
const animateSpeaking = () => {};
const speak = () => {};
""" + player_class + """
const player = new SegmentPlayer();
player.total = 2;
player.add(0, '/first.mp3', null, 'first');
player.currentAudio.currentTime = 4.25;
player.pauseForMute();
resolvePlay();
Promise.resolve().then(() => {
  if (player.currentIndex !== 0 || player.playedAny || player.playing) process.exit(1);
  player._playNext();
  if (player.currentIndex !== 1 || !player.currentAudio || player.currentAudio.src !== '/first.mp3' || resumedAt !== 4.25) process.exit(1);
});
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_exhausted_failed_segment_stream_uses_one_text_fallback(self):
        source = CLIENT.read_text(encoding="utf-8")
        class_start = source.index("class SegmentPlayer")
        class_end = source.index("const segmentPlayer = new SegmentPlayer", class_start)
        player_class = source[class_start:class_end]
        script = """
const window = {};
const state = { speechEnabled: true };
let speechCalls = 0;
const audioManager = {
  active: false,
  _connectAudio() {},
  _syncLipWithAudio() {},
  getContext() { return { state: 'running' }; },
  playAudio() {},
};
const performance = { now: () => 1 };
const setTimeout = () => 1;
const clearTimeout = () => {};
const setInterval = () => 1;
const clearInterval = () => {};
const normalizeBackendAudioUrl = (url) => url;
const createPlaybackAudio = () => null;
const animateSpeaking = () => {};
const speak = () => { speechCalls++; };
""" + player_class + """
const player = new SegmentPlayer();
player.total = 1;
player.add(0, null, null, 'failed');
player.finalizeSegmentStream('reply text');
player._playNext();
if (speechCalls !== 1) process.exit(1);
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_done_finalizes_missing_slots_without_replaying_done_audio(self):
        source = CLIENT.read_text(encoding="utf-8")
        class_start = source.index("class SegmentPlayer")
        class_end = source.index("const segmentPlayer = new SegmentPlayer", class_start)
        player_class = source[class_start:class_end]
        script = """
const window = {};
const state = { speechEnabled: true };
let fallbackCalls = 0;
const audioManager = {
  active: false,
  _connectAudio() {},
  _syncLipWithAudio() {},
  getContext() { return { state: 'running' }; },
  playAudio() { fallbackCalls++; },
};
const performance = { now: () => 1 };
const setTimeout = () => 1;
const clearTimeout = () => {};
const setInterval = () => 1;
const clearInterval = () => {};
const normalizeBackendAudioUrl = (url) => url;
const createPlaybackAudio = (src) => ({
  src, ended: false, readyState: 4, networkState: 1, currentTime: 1,
  addEventListener() {},
  play() { return Promise.resolve(); },
  pause() {},
});
const animateSpeaking = () => {};
const speak = () => {};
""" + player_class + """
const player = new SegmentPlayer();
player.total = 2;
player.add(1, '/second.mp3', null, 'second');
player.fallbackUrl = '/done-audio.mp3';
player.finalizeSegmentStream('reply');
player._playNext();
if (fallbackCalls !== 0 || !player.currentAudio || player.currentAudio.src !== '/second.mp3') process.exit(1);
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_releasing_before_microphone_access_resolves_cancels_the_recorder_start(self):
        source = CLIENT.read_text(encoding="utf-8")

        self.assertIn("recorderStartPending", source)
        self.assertIn("cancelPendingRecorderStart", source)
        self.assertIn("if (cancelPendingRecorderStart)", source)
        self.assertIn("recorderStream?.getTracks().forEach(track => track.stop())", source)
        stop_listener = source[source.index("function stopListening() {"):]
        self.assertIn("recorderStartPending", stop_listener)
        self.assertIn("stopRecorderFallback()", stop_listener)

    def test_recorder_upload_preserves_supported_mime_type_and_matching_extension(self):
        source = CLIENT.read_text(encoding="utf-8")

        self.assertIn('"audio/mp4"', source)
        self.assertIn("recorderFilename", source)
        self.assertIn("form.append(\"file\", audioBlob, recorderFilename(recordedMimeType))", source)

    def test_shared_upload_handler_rate_limits_asr_before_transcription(self):
        source = MAIN.read_text(encoding="utf-8")

        self.assertIn("VOICE_UPLOAD_ATTEMPT_LIMIT", source)
        self.assertIn("def _voice_upload_is_rate_limited", source)
        self.assertIn("if _voice_upload_is_rate_limited(request.remote_addr or \"\"):", source)
        self.assertIn('"code": 429', source)

        import ast
        tree = ast.parse(source, filename=str(MAIN))
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_voice_upload_is_rate_limited")
        namespace = {
            "time": time,
            "VOICE_UPLOAD_ATTEMPTS": {},
            "VOICE_UPLOAD_ATTEMPTS_LOCK": threading.Lock(),
            "VOICE_UPLOAD_ATTEMPT_WINDOW_SECONDS": 60,
            "VOICE_UPLOAD_ATTEMPT_LIMIT": 2,
        }
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(MAIN), "exec"), namespace)
        limit = namespace["_voice_upload_is_rate_limited"]
        self.assertFalse(limit("127.0.0.1"))
        self.assertFalse(limit("127.0.0.1"))
        self.assertTrue(limit("127.0.0.1"))

    def test_asr_mime_matches_the_validated_audio_suffix(self):
        source = AI_SERVICE.read_text(encoding="utf-8")

        self.assertIn("def audio_upload_mime_type", source)
        self.assertIn("audio_upload_mime_type(file_path)", source)

        import ast
        tree = ast.parse(source, filename=str(AI_SERVICE))
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "audio_upload_mime_type")
        namespace = {}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(AI_SERVICE), "exec"), namespace)
        mime_for = namespace["audio_upload_mime_type"]
        self.assertEqual(mime_for(Path("voice.m4a")), "audio/mp4")
        self.assertEqual(mime_for(Path("voice.ogg")), "audio/ogg")


if __name__ == "__main__":
    unittest.main()
