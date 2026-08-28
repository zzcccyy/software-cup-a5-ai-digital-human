# 语音合成服务 - SiliconFlow CosyVoice 备用 TTS
# 关键修复: 不再硬编码 E:/ 路径, 接 voice_name 参数, 解除 200 字截断
import hashlib
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from runtime_paths import BACKEND_DIR

import requests

API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
API_BASE = os.getenv("SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1")

# 模型/voice 字典: 管理员可选的 voice → SiliconFlow 实际参数
VOICE_PARAMS_MAP: dict[str, dict] = {
    "温柔女声": {"model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "中文女声-温柔", "max_chars": 1500},
    "活泼少女": {"model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "中文女声-活力", "max_chars": 1500},
    "知性女声": {"model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "中文女声-知性", "max_chars": 1500},
    "磁性男声": {"model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "中文男声-磁性", "max_chars": 1500},
    "沉稳男声": {"model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "中文男声-沉稳", "max_chars": 1500},
}
DEFAULT_PARAMS = {"model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "中文女声-温柔", "max_chars": 1500}

# ── 并发治理: per-hash single-flight 锁 (引用计数防无界增长) + 原子写盘 ──
_TTS_LOCKS: dict[str, tuple[threading.RLock, int]] = {}
_TTS_LOCKS_GUARD = threading.Lock()


@contextmanager
def _tts_hash_lock(hash_key: str):
    with _TTS_LOCKS_GUARD:
        entry = _TTS_LOCKS.get(hash_key)
        if entry is None:
            entry = (threading.RLock(), 0)
            _TTS_LOCKS[hash_key] = entry
        lock, refs = entry
        _TTS_LOCKS[hash_key] = (lock, refs + 1)
    try:
        with lock:
            yield lock
    finally:
        with _TTS_LOCKS_GUARD:
            entry = _TTS_LOCKS.get(hash_key)
            if entry is not None and entry[0] is lock:
                refs = entry[1] - 1
                if refs <= 0:
                    del _TTS_LOCKS[hash_key]
                else:
                    _TTS_LOCKS[hash_key] = (lock, refs)


def _atomic_write(path: str, data: bytes) -> None:
    """先写唯一临时文件再 os.replace 原子改名, 读者不会读到半截文件.
    Windows 下 Defender 实时扫描会短暂锁住新写入的文件导致 replace 被拒
    (WinError 5), 重试 3 次; 仍失败则回退直接写入, 保证缓存可用."""
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    with open(path, "wb") as f:
        f.write(data)


def text_to_speech(text: str, output_path: str | None = None, voice_name: str = "温柔女声", timeout: int = 15):
    """文字转语音 (SiliconFlow CosyVoice 备用引擎).
    Args:
        text: 待合成文本
        output_path: 输出目录, 默认 backend/static/audio
        voice_name: VOICE_PARAMS_MAP 里的 key
        timeout: HTTP 超时秒数
    Returns:
        成功返回 /static/audio/xxx.mp3 URL, 失败返回 None
    """
    if not API_KEY:
        return None
    if output_path is None:
        output_path = str(BACKEND_DIR / "static" / "audio")
    os.makedirs(output_path, exist_ok=True)

    params = VOICE_PARAMS_MAP.get(voice_name, DEFAULT_PARAMS)
    clean = (text or "").strip()
    if not clean or len(clean) < 2:
        return None
    # 关键修复: 用配置化的 max_chars 替代硬编码 200
    clean = clean[: params["max_chars"]]

    # 关键修复: 文件名用 16 字符 hash 避免碰撞
    h = hashlib.md5(f"{clean}_{voice_name}".encode("utf-8")).hexdigest()[:16]
    filename = f"sf_{h}.mp3"
    filepath = os.path.join(output_path, filename)
    url_path = f"/static/audio/{filename}"

    # 命中缓存直接返回
    if os.path.exists(filepath) and os.path.getsize(filepath) > 100:
        return url_path

    # 关键修复: per-hash 锁 + 锁内双检, 并发同文本时只调一次 API
    with _tts_hash_lock(h):
        if os.path.exists(filepath) and os.path.getsize(filepath) > 100:
            return url_path

        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": params["model"],
            "input": clean,
            "voice": params["voice"],
            "response_format": "mp3",
            "speed": 1.0,
        }
        try:
            resp = requests.post(
                f"{API_BASE}/audio/speech",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 200 and len(resp.content) > 100:
                # 关键修复: 原子写盘, 避免读者读到半截文件
                _atomic_write(filepath, resp.content)
                print(f"[tts_service] OK voice={voice_name} {len(resp.content)/1024:.1f}KB", flush=True)
                return url_path
            print(f"[tts_service] TTS错误: {resp.status_code} - {resp.text[:200]}", flush=True)
            return None
        except Exception as e:
            print(f"[tts_service] TTS异常: {type(e).__name__}: {e}", flush=True)
            return None


def text_to_speech_browser(text: str) -> str:
    """浏览器直接播放的TTS (使用 Web Speech API) - 仅返回 JS 代码片段."""
    safe = (text or "").replace("'", "\\'").replace("\n", " ")
    return f"""<script>
if ('speechSynthesis' in window) {{
    const msg = new SpeechSynthesisUtterance('{safe}');
    msg.lang = 'zh-CN';
    msg.rate = 1.0;
    speechSynthesis.speak(msg);
}}
</script>"""
