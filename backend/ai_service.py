#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import time
import os
import json
import re
import hashlib
import hmac
import base64
import uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode

from functools import lru_cache
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
import edge_tts
import websocket
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from runtime_paths import BACKEND_DIR

load_dotenv(BACKEND_DIR / ".env")


# ============== Provider Selection ==============
# Set to "deepseek" to use DeepSeek, "xunfei" for Xunfei Spark, or "siliconflow" for SiliconFlow
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")

# ============== SiliconFlow API ==============
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SILICONFLOW_API_BASE = os.getenv(
    "SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1"
)

# ============== Xunfei Spark API ==============
XUNFEI_APP_ID = os.getenv("XUNFEI_APP_ID", "")
XUNFEI_API_KEY = os.getenv("XUNFEI_API_KEY", "")
XUNFEI_API_SECRET = os.getenv("XUNFEI_API_SECRET", "")
XUNFEI_API_BASE = os.getenv("XUNFEI_API_BASE", "wss://spark-api.xf-yun.com/v4.0/chat")

# ============== DeepSeek API ==============
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_BASE = os.getenv(
    "DEEPSEEK_API_BASE", "https://api.deepseek.com/v1"
)

# ============== Model Selection ==============
CHAT_MODEL = os.getenv("CHAT_MODEL", "Qwen/Qwen2.5-7B-Instruct")
try:
    STREAM_MAX_TOKENS = max(128, min(int(os.getenv("LLM_STREAM_MAX_TOKENS", "600")), 1200))
except ValueError:
    STREAM_MAX_TOKENS = 600

# ============== TTS / ASR ==============
TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
ASR_MODEL = os.getenv("SILICONFLOW_ASR_MODEL", "FunAudioLLM/SenseVoiceSmall")
ASR_FALLBACK_MODEL = os.getenv("SILICONFLOW_ASR_FALLBACK_MODEL", "TeleAI/TeleSpeechASR")
AUDIO_DIR = BACKEND_DIR / "static" / "audio"

# Admin-friendly voice name -> edge-tts voice name
VOICE_MAP: dict[str, str] = {
    "温柔女声": "zh-CN-XiaoxiaoNeural",
    "活泼少女": "zh-CN-XiaoyiNeural",
    "热情女声": "zh-CN-XiaohanNeural",
    "知性女声": "zh-CN-XiaomoNeural",
    "磁性男声": "zh-CN-YunjianNeural",
    "深沉男声": "zh-CN-YunyeNeural",
    "阳光男声": "zh-CN-YunxiNeural",
    "稳重男声": "zh-CN-YunyangNeural",
}

STYLE_PROMPT_MAP: dict[str, str] = {
    "亲和讲解员": "语气亲切口语化，像朋友一样和游客自然聊天。",
    "知识型讲解": "知识丰富专业，引用历史典故，但保持流畅好懂。",
    "活泼互动型": "精力充沛，语调起伏，使用感叹词和反问句。",
    "文雅讲解": "语言优美典雅，适当运用修辞手法。",
}

MAX_TTS_CHARS = 3000

# ── Shared event loop for TTS (avoids creating/destroying loop per call) ──
import threading

_tts_loop: asyncio.AbstractEventLoop | None = None
_tts_loop_lock = threading.Lock()


def _get_tts_loop() -> asyncio.AbstractEventLoop:
    global _tts_loop
    if _tts_loop is not None and _tts_loop.is_running():
        return _tts_loop
    with _tts_loop_lock:
        if _tts_loop is not None and _tts_loop.is_running():
            return _tts_loop
        _tts_loop = asyncio.new_event_loop()
        t = threading.Thread(target=_tts_loop.run_forever, daemon=True)
        t.start()
        print("[TTS-loop] shared event loop started")
        return _tts_loop


def _tts_run(coro):
    loop = _get_tts_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30)


def _tts_run_with_timeout(coro, timeout: float = 8.0):
    """关键修复: 带超时的 TTS 调用, 避免单次 hang 拖死整个请求链路."""
    loop = _get_tts_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        # 兼容 concurrent.futures.TimeoutError 和 built-in TimeoutError
        if "TimeoutError" in type(e).__name__ or "timeout" in str(e).lower():
            try:
                future.cancel()
            except Exception:
                pass
            raise TimeoutError(f"TTS synthesis exceeded {timeout}s timeout") from e
        raise


# ── 请求级 TTS 任务跟踪与取消 ──
# 关键修复: 支持按 tag 取消某个请求在共享 TTS 事件循环上的在途合成任务.
# 当同一 session 发起新请求时, 旧请求会被取消, 其 TTS 任务立即停掉,
# 释放共享事件循环, 避免拖慢新一轮回答 (否则新一轮可能因 30s 前端超时而被吞掉).
_TTS_TASKS: dict[str, set] = {}
_TTS_TASKS_LOCK = threading.Lock()


async def _run_tracked(coro, tag: str):
    task = asyncio.current_task()
    with _TTS_TASKS_LOCK:
        _TTS_TASKS.setdefault(tag, set()).add(task)
    try:
        return await coro
    finally:
        with _TTS_TASKS_LOCK:
            s = _TTS_TASKS.get(tag)
            if s:
                s.discard(task)
                if not s:
                    _TTS_TASKS.pop(tag, None)


def _tts_submit_tracked(coro, tag: str):
    loop = _get_tts_loop()
    return asyncio.run_coroutine_threadsafe(_run_tracked(coro, tag), loop)


def cancel_tts_for_tag(tag: str | None) -> None:
    """取消 tag 对应的在途 TTS 合成任务 (asyncio Task)."""
    if not tag:
        return
    tasks = []
    with _TTS_TASKS_LOCK:
        tasks = list(_TTS_TASKS.get(tag, set()))
    if not tasks:
        return
    loop = _get_tts_loop()
    for t in tasks:
        try:
            loop.call_soon_threadsafe(t.cancel)
        except Exception:
            pass
    print(f"[TTS] 取消 tag={tag} 的 {len(tasks)} 个在途合成任务", flush=True)


def warmup_tts():
    """Pre-connect to edge-tts to eliminate cold-start delay on first request."""
    try:
        async def _warm():
            comm = edge_tts.Communicate("你好", TTS_VOICE)
            audio = b""
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    audio += chunk["data"]
            return len(audio)
        size = _tts_run(_warm())
        print(f"[TTS-warmup] OK, {size/1024:.1f}KB")
    except Exception as e:
        print(f"[TTS-warmup] failed: {e}")


# ============== HTTP Connection Pool ==============
def _create_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_http = _create_session()


# ============== API Status ==============
def get_api_status() -> dict:
    return {
        "siliconflow": bool(SILICONFLOW_API_KEY),
        "deepseek": bool(DEEPSEEK_API_KEY),
        "xunfei": bool(XUNFEI_APP_ID),
        "chat": bool(SILICONFLOW_API_KEY or DEEPSEEK_API_KEY or XUNFEI_APP_ID),
    }


def api_enabled() -> bool:
    return bool(SILICONFLOW_API_KEY or DEEPSEEK_API_KEY or XUNFEI_APP_ID)


def is_factual_query(message: str) -> bool:
    """Determine if a query is primarily factual."""
    fact_words = ["多高", "高度", "多少米", "门票", "票价", "多少钱", "开放时间", "几点",
                  "位于", "在哪", "地址", "历史", "建于", "始建于", "什么时候", "传说",
                  "多远", "多大", "多久", "怎么去", "怎么走", "公交", "地铁", "停车",
                  "多大", "多长", "多少", "价格", "费用", "收费", "免费"]
    return any(w in message for w in fact_words)


def get_answer_temperature(message: str) -> float:
    """Dynamic temperature: low for facts, high for creativity."""
    if is_factual_query(message):
        return 0.1
    if any(w in message for w in ["路线", "推荐", "安排", "怎么玩", "怎么逛", "好玩"]):
        return 0.4
    return 0.5


def _get_system_prompt(interest: str = "", gps: list[float] | None = None, route_info: dict | None = None,
                       has_knowledge_context: bool = False) -> str:
    interest_labels = {"history": "历史文化", "nature": "自然风光", "family": "亲子互动", "relax": "舒缓漫游"}
    base = "你叫小灵，是灵山胜境景区的AI数字人导游。请用口语化、热情亲切的语气和游客对话。\n"
    if interest and interest in interest_labels:
        base += f"游览偏好：{interest_labels[interest]}。据此调整回答风格。\n"
    if gps:
        base += f"GPS：{gps[0]},{gps[1]}\n"
    if route_info and route_info.get("name"):
        base += f"当前路线：{route_info.get('name')}，用时{route_info.get('duration', '约3小时')}。\n"
    base += ("你拥有系统内置的高德地图导航能力，系统会自动检测游客的导航需求并在页面右侧打开地图面板。"
             "所以当游客询问'怎么走''怎么去''在哪里''导航''帮我导航''多远''路线'等问题时，"
             "你应该直接告知'已为您打开地图导航到[景点名]'，而不是让游客去打开外部App。"
             "你可以结合对话上下文（如之前提到的地点）来确定导航目的地。\n")

    # Enhanced factual accuracy rules
    if has_knowledge_context:
        base += (
            "\n【关键规则——请严格遵守】\n"
            "1. 【优先引用资料】系统消息中提供了「参考资料」和「本地已知信息」，这些信息是官方准确的。\n"
            "2. 【基于事实回答】所有关于景区高度、价格、时间、地址等事实性数据，必须严格基于参考资料回答。\n"
            "3. 【禁止编造】不确定或参考资料中没有的信息，直接说\"我暂时没有查到相关信息，建议您咨询景区服务中心\"，不要自行编造。\n"
            "4. 【数字一致】引用数字（高度、价格、时间等）时请确保与参考资料完全一致，不能修改。\n"
            "5. 【准确优先】如果问题涉及具体事实数据，优先给出简洁确切的回答而不是长篇介绍。\n"
            "6. 【分清概念】注意区分：灵山胜境和拈花湾是两个独立景区需要分别购票；梵宫和九龙灌浴是不同的景点；灵山大佛、祥符禅寺分别是独立的景点。\n"
        )
    else:
        base += (
            "\n如果没有参考资料可参考，对于不确定的问题请说："
            "\"我暂时没有查到相关信息，建议您咨询景区服务中心\"，不要编造。\n"
        )
    base += (
        "只输出面向游客的自然中文回答。不要输出 JSON、代码块、Markdown、内部字段或控制信息，"
        "不要输出拼音/罗马化注释、语言代码或语言标签。情绪和动作由系统处理，无需说明。\n"
    )
    return base


# ============== LLM Chat Functions ==============
def _siliconflow_chat(messages: list[dict], max_tokens: int = 1000, temperature: float = 0.3) -> str | None:
    try:
        r = _http.post(
            f"{SILICONFLOW_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {SILICONFLOW_API_KEY}"},
            json={"model": CHAT_MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        print(f"SiliconFlow LLM error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"SiliconFlow LLM exception: {e}")
    return None


def _decode_sse_line(raw_line: bytes | str) -> str:
    """Decode provider SSE bytes deterministically instead of trusting HTTP headers."""
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8", errors="replace")
    return raw_line


def _siliconflow_chat_stream(messages: list[dict], max_tokens: int = STREAM_MAX_TOKENS):
    r = None
    try:
        r = _http.post(
            f"{SILICONFLOW_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {SILICONFLOW_API_KEY}"},
            json={"model": CHAT_MODEL, "messages": messages, "max_tokens": max_tokens, "stream": True},
            timeout=(10, 25),
            stream=True,
        )
        if r.status_code != 200:
            print(f"SiliconFlow stream error: {r.status_code} {r.text[:200]}")
            return
        for raw_line in r.iter_lines(decode_unicode=False):
            line = _decode_sse_line(raw_line)
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                delta = data.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
            except json.JSONDecodeError:
                continue
    except Exception as e:
        print(f"SiliconFlow stream exception: {e}")
    finally:
        # 关键修复: 客户端断连(GeneratorExit)/异常/正常消费完三路都关闭响应连接,
        # 防止请求级连接泄漏占满连接池.
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def _deepseek_chat(messages: list[dict], max_tokens: int = 1000, temperature: float = 0.3) -> str | None:
    try:
        r = _http.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={"model": "deepseek-chat", "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        print(f"DeepSeek LLM error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"DeepSeek LLM exception: {e}")
    return None


def analysis_with_api(
    messages: list[dict],
    max_tokens: int = 1800,
    temperature: float = 0.2,
) -> str | None:
    """Run a non-streaming structured analysis through the configured provider."""
    if not api_enabled():
        return None
    if LLM_PROVIDER == "deepseek":
        return _deepseek_chat(messages, max_tokens=max_tokens, temperature=temperature)
    if LLM_PROVIDER == "xunfei":
        return _xunfei_chat(messages, max_tokens=max_tokens)
    return _siliconflow_chat(messages, max_tokens=max_tokens, temperature=temperature)


def _deepseek_chat_stream(messages: list[dict], max_tokens: int = STREAM_MAX_TOKENS):
    r = None
    try:
        r = _http.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={"model": "deepseek-chat", "messages": messages, "max_tokens": max_tokens, "stream": True},
            timeout=(10, 25),
            stream=True,
        )
        if r.status_code != 200:
            print(f"DeepSeek stream error: {r.status_code} {r.text[:200]}")
            return
        for raw_line in r.iter_lines(decode_unicode=False):
            line = _decode_sse_line(raw_line)
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                delta = data.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
            except json.JSONDecodeError:
                continue
    except Exception as e:
        print(f"DeepSeek stream exception: {e}")
    finally:
        # 关键修复: 与 _siliconflow_chat_stream 对齐, 断连/异常/正常三路关闭连接.
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def _xunfei_chat(messages: list[dict], max_tokens: int = 400) -> str | None:
    if not XUNFEI_APP_ID:
        return None
    now = datetime.now()
    date_str = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    host = "spark-api.xf-yun.com"
    tmp = f"host: {host}\ndate: {date_str}\nPOST /v4.0/chat HTTP/1.1"
    h = hmac.new(
        XUNFEI_API_SECRET.encode("utf-8"),
        tmp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    auth = base64.b64encode(h).decode("utf-8")
    auth_header = (
        f'api_key="{XUNFEI_API_KEY}",algorithm="hmac-sha256",'
        f'headers="host date request-line",signature="{auth}"'
    )
    url = (
        f"{XUNFEI_API_BASE}?authorization={urlencode({'': auth_header})[1:]}&"
        f"date={urlencode({'': date_str})[1:]}&host={host}"
    )
    deadline = time.monotonic() + 25.0
    ws = None
    try:
        ws = websocket.create_connection(url, timeout=5)
    except Exception as e:
        print(f"Xunfei connect failed: {e}")
        return None
    try:
        payload = {
            "header": {"app_id": XUNFEI_APP_ID},
            "parameter": {"chat": {"domain": "4.0Ultra", "max_tokens": max_tokens}},
            "payload": {"message": {"text": messages}},
        }
        ws.send(json.dumps(payload))
        result = ""
        while True:
            # 关键修复: 总时限 25s, 防止服务端持续吐分片无限挂起 (占满 Waitress 线程)
            if time.monotonic() > deadline:
                print("Xunfei overall timeout (25s)")
                return None
            resp = json.loads(ws.recv())
            code = resp["header"]["code"]
            if code != 0:
                print(f"Xunfei error: {code} {resp['header'].get('message','')}")
                break
            content = resp["payload"]["choices"]["text"][0].get("content", "")
            result += content
            if resp["header"]["status"] == 2:
                break
        return result
    except Exception as e:
        print(f"Xunfei exception: {e}")
        return None
    finally:
        # 关键修复: 任何路径(含异常/超时)都关闭连接, 防止泄漏
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass


# ============== Legacy API Chat (used by admin/fallback) ==============
def _compress_reply(text: str, max_len: int = 500) -> str:
    text = re.sub(r'```json\s*\{.*?\}\s*```', '', text, flags=re.DOTALL)
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = _strip_control_json(text)
    text = text.strip().strip("`\"'").strip()
    return text[:max_len] if len(text) > max_len else text


def _parse_llm_json_block(raw: str) -> tuple[str, dict | None, list[dict] | None]:
    """Parse JSON block from LLM output. Returns (cleaned_text, emotion_dict, actions_list)."""
    json_match = re.search(r'```json\s*(\{.*\})\s*```', raw, re.DOTALL)
    if not json_match:
        return raw.strip(), None, None
    try:
        data = json.loads(json_match.group(1))
        clean = raw[:json_match.start()] + raw[json_match.end():]
        clean = clean.strip().strip('`"\' \n')
        emotion = data.get("emotion")
        actions = data.get("actions", [])
        if emotion and not isinstance(emotion.get("primary"), str):
            emotion = None
        valid_set = {"wave","nod","shake","bow","tilt","gesture","spread","point","think","openHand","crossArms","comfort"}
        seen = set()
        ordered = []
        for a in actions:
            t = a.get("type") if isinstance(a, dict) else None
            if t in valid_set and t not in seen:
                seen.add(t)
                ordered.append({"type": t})
        actions = ordered
        return clean, emotion, actions if actions else None
    except (json.JSONDecodeError, KeyError):
        return raw.strip(), None, None


# ============== Streaming control blocks ==============
def _split_provider_output(text: str) -> tuple[str, str]:
    """Return visible answer text and a hidden JSON control fence, if present."""
    text = text or ""
    lowered = text.lower()
    visible_parts: list[str] = []
    cursor = 0
    while cursor < len(text):
        marker = lowered.find("```json", cursor)
        if marker < 0:
            tail = text[cursor:]
            for partial_fence in ("```jso", "```js", "```j", "```", "``", "`"):
                if tail.lower().endswith(partial_fence):
                    return "".join(visible_parts) + tail[:-len(partial_fence)], partial_fence
            return "".join(visible_parts) + tail, ""

        visible_parts.append(text[cursor:marker])
        close = text.find("```", marker + len("```json"))
        if close < 0:
            return "".join(visible_parts), text[marker:]
        cursor = close + len("```")

    return "".join(visible_parts), ""


def sanitize_final_visible_text(text: str) -> str | None:
    """Return visitor-safe text, or None when control output leaks or is malformed."""
    candidate = (text or "").strip()
    if not candidate:
        return candidate

    lowered = candidate.lower()
    if "```" in candidate:
        return None
    if re.search(r"(?:zh-(?:cn|tw)|\bpinyin\b|拼音|\bromanization\b|\blanguage\s*(?:tag|code)?\s*[:：])", lowered):
        return None
    if re.search(r"\b(?:emotion|primary|secondary|intensity|actions)\s*[:=]", lowered):
        return None
    reserved_terms = ("emotion", "primary", "secondary", "intensity", "actions")
    if sum(term in lowered for term in reserved_terms) >= 2:
        return None
    if re.search(r"[\[{]\s*[\"']?(?:emotion|primary|secondary|intensity|actions)[\"']?\s*[:：]", lowered):
        return None
    return candidate


# ============== LLM Response Cache ==============
import threading
import time

_LLM_CACHE: dict[str, tuple[dict, float]] = {}
_LLM_CACHE_TTL = 600  # 10 minutes
_LLM_CACHE_MAX = 256
_llm_cache_lock = threading.Lock()


def _make_llm_cache_key(
    message: str,
    knowledge_context: list[dict] | None,
    history: list[dict] | None,
    interest: str = "",
    route: dict | None = None,
    draft_answer: str = "",
    supporting_facts: list[str] | None = None,
) -> str:
    """关键修复: 缓存键纳入 interest/route/draft_answer/supporting_facts,
    避免同一问句在不同偏好/路线/草稿下复用同一结果."""
    kparts = [message.strip().lower()]
    if interest:
        kparts.append(f"interest={interest.strip().lower()}")
    if route:
        kparts.append(f"route={str(route.get('id', '')).strip().lower()}")
    if draft_answer:
        kparts.append(f"draft={draft_answer.strip()[:80]}")
    if knowledge_context:
        for k in knowledge_context[:3]:
            kparts.append(k.get("content", k.get("text", ""))[:80])
    if supporting_facts:
        for f in supporting_facts[:3]:
            kparts.append(f"fact={f.strip()[:80]}")
    if history:
        for h in history[-2:]:
            kparts.append(h.get("content", "")[:40])
    raw = "|".join(kparts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _get_cached_llm(key: str) -> dict | None:
    with _llm_cache_lock:
        if key in _LLM_CACHE:
            result, ts = _LLM_CACHE[key]
            if time.time() - ts < _LLM_CACHE_TTL:
                return result.copy()
            del _LLM_CACHE[key]
    return None


def _set_cached_llm(key: str, result: dict):
    with _llm_cache_lock:
        if len(_LLM_CACHE) >= _LLM_CACHE_MAX:
            oldest_key = min(_LLM_CACHE, key=lambda k: _LLM_CACHE[k][1])
            del _LLM_CACHE[oldest_key]
        _LLM_CACHE[key] = (result, time.time())


def chat_with_api(
    message: str,
    draft_answer: str = "",
    knowledge_context: list[dict] | None = None,
    route: dict | None = None,
    history: list[dict] | None = None,
    supporting_facts: list[str] | None = None,
    avatar_config: dict | None = None,
) -> dict:
    cache_key = _make_llm_cache_key(
        message,
        knowledge_context,
        history,
        interest=(avatar_config or {}).get("interest", ""),
        route=route,
        draft_answer=draft_answer,
        supporting_facts=supporting_facts,
    )
    cached = _get_cached_llm(cache_key)
    if cached:
        return cached

    try:
        interest = (avatar_config or {}).get("interest", "")
        has_kb = bool(knowledge_context or supporting_facts)
        system = _get_system_prompt(interest, route_info=route, has_knowledge_context=has_kb)
        temperature = get_answer_temperature(message)
        messages = [{"role": "system", "content": system}]
        if supporting_facts:
            facts_text = "\n".join(f"- {f}" for f in supporting_facts[:5])
            messages.append({"role": "system", "content": f"本地已知信息：\n{facts_text}\n请基于以上信息并结合你的知识回答。"})
        if knowledge_context:
            ctx = "\n".join(
                f"[{k.get('type', '知识')}]: {k.get('content', k.get('text', ''))}"
                for k in knowledge_context[:5]
            )
            messages.append({"role": "system", "content": f"参考资料：\n{ctx}"})
        if draft_answer:
            messages.append({"role": "system", "content": f"本地草稿回复供参考（请润色但保留核心事实）：{draft_answer}"})
        for h in (history or [])[-6:]:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": message})
        if LLM_PROVIDER == "deepseek":
            raw = _deepseek_chat(messages, temperature=temperature)
        elif LLM_PROVIDER == "xunfei":
            raw = _xunfei_chat(messages)
        else:
            raw = _siliconflow_chat(messages, temperature=temperature)
        used_api = bool(raw and raw.strip())
        raw_text = raw if used_api else draft_answer
        reply, llm_emotion, llm_actions = _parse_llm_json_block(raw_text)
        reply = _compress_reply(reply)
        result = {
            "reply": reply,
            "route": str(route.get("id", "")) if route else "llm",
            "llm_emotion": llm_emotion,
            "llm_actions": llm_actions,
            "used_api": used_api,
        }
        _set_cached_llm(cache_key, result)
        return result
    except Exception as e:
        print(f"chat_with_api error: {e}")
        return {"reply": draft_answer, "route": str(route.get("id", "")) if route else "local", "used_api": False}


def chat_with_api_stream(
    message: str,
    draft_answer: str = "",
    knowledge_context: list[dict] | None = None,
    route: dict | None = None,
    history: list[dict] | None = None,
    supporting_facts: list[str] | None = None,
    avatar_config: dict | None = None,
):
    try:
        interest = (avatar_config or {}).get("interest", "")
        has_kb = bool(knowledge_context or supporting_facts)
        system = _get_system_prompt(interest, route_info=route, has_knowledge_context=has_kb)
        messages = [{"role": "system", "content": system}]
        if supporting_facts:
            facts_text = "\n".join(f"- {f}" for f in supporting_facts[:5])
            messages.append({"role": "system", "content": f"本地已知信息：\n{facts_text}\n请基于以上信息并结合你的知识回答。"})
        if knowledge_context:
            ctx = "\n".join(
                f"[{k.get('type', '知识')}]: {k.get('content', k.get('text', ''))}"
                for k in knowledge_context[:5]
            )
            messages.append({"role": "system", "content": f"参考资料：\n{ctx}"})
        if draft_answer:
            messages.append({"role": "system", "content": f"本地草稿回复供参考（请润色但保留核心事实）：{draft_answer}"})
        for h in (history or [])[-6:]:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": message})

        if LLM_PROVIDER == "deepseek":
            yield from _deepseek_chat_stream(messages)
        elif LLM_PROVIDER == "xunfei":
            raw = _xunfei_chat(messages)
            if raw:
                yield raw
        else:
            yield from _siliconflow_chat_stream(messages)
    except Exception as e:
        print(f"chat_with_api_stream error: {e}")


# ============== ASR ==============
def audio_upload_mime_type(file_path: Path) -> str:
    """Set the multipart content type from the validated upload suffix."""
    return {
        ".webm": "audio/webm",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
    }.get(file_path.suffix.lower(), "application/octet-stream")


def transcribe_audio(file_path: Path) -> str:
    if not SILICONFLOW_API_KEY:
        raise RuntimeError("SiliconFlow API key not configured")

    asr_base = SILICONFLOW_API_BASE
    asr_key = SILICONFLOW_API_KEY
    endpoint = f"{asr_base}/audio/transcriptions"
    models = list(dict.fromkeys(filter(None, (ASR_MODEL, ASR_FALLBACK_MODEL))))
    attempts_per_model = 2
    last_connection_error: Exception | None = None
    last_response: requests.Response | None = None

    for model_index, model in enumerate(models):
        for attempt in range(1, attempts_per_model + 1):
            try:
                # Reopen the file for every attempt so multipart uploads restart
                # at byte zero after a connection reset.
                with file_path.open("rb") as audio_file:
                    response = _http.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {asr_key}"},
                        data={"model": model},
                        files={"file": (file_path.name, audio_file, audio_upload_mime_type(file_path))},
                        timeout=(10, 90),
                    )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_connection_error = exc
                if attempt < attempts_per_model:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                break

            if response.status_code == 200:
                data = response.json()
                return (data.get("text") or "").strip()

            last_response = response
            trace_id = response.headers.get("x-siliconcloud-trace-id", "-")
            print(
                f"[ASR] model={model} status={response.status_code} "
                f"attempt={attempt}/{attempts_per_model} trace={trace_id}",
                flush=True,
            )

            if response.status_code in {500, 502, 503, 504}:
                if attempt < attempts_per_model:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                break

            # Authentication, permission and request-format failures should be
            # surfaced immediately instead of being repeated against another
            # model.
            response.raise_for_status()

        if model_index < len(models) - 1:
            print(f"[ASR] switching fallback model: {model} -> {models[model_index + 1]}", flush=True)

    if last_response is not None:
        trace_id = last_response.headers.get("x-siliconcloud-trace-id", "-")
        detail = ""
        try:
            payload = last_response.json()
            detail = str(payload.get("message") or payload.get("code") or "")
        except Exception:
            detail = last_response.text[:120]
        message = f"语音转写服务暂时异常（HTTP {last_response.status_code}，trace={trace_id}）"
        if detail:
            message += f"：{detail}"
        raise requests.HTTPError(message, response=last_response)

    raise requests.ConnectionError(
        "语音转写服务连接中断，主备模型均已自动重试，请稍后再试"
    ) from last_connection_error


# ============== TTS (edge-tts) ==============

# ── TTS 缓存并发治理 ──
# 关键修复: per-hash single-flight 锁 (RLock, 支持 synthesize_tts → synthesize_tts_bytes
# 同哈希重入) + 原子写盘 (唯一临时文件 + os.replace) + 失败只删自己创建的文件 +
# edge-tts 全局并发上限, 防多会话/预热并发风暴.
_EDGE_TTS_SEMAPHORE = threading.Semaphore(4)
_TTS_LOCKS: dict[str, tuple[threading.RLock, int]] = {}
_TTS_LOCKS_GUARD = threading.Lock()


@contextmanager
def _tts_hash_lock(hash_key: str):
    """per-hash single-flight 锁, 引用计数防无界增长 (最后一个持有者释放后移除)."""
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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """先写唯一临时文件再 os.replace 原子改名, 读者不会读到半截文件.
    Windows 下 Defender 实时扫描会短暂锁住新写入的文件导致 replace 被拒
    (WinError 5), 重试 3 次; 仍失败则回退直接写入, 保证缓存可用."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    path.write_bytes(data)


def _strip_control_json(text: str) -> str:
    """使用花括号计数，健壮地移除文本中嵌套的 JSON 控制块（emotion/actions）。
    支持任意深度的嵌套花括号，不会漏掉嵌套 JSON。"""
    result = []
    i = 0
    while i < len(text):
        if text[i] == '{':
            depth = 0
            j = i
            while j < len(text):
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                    if depth == 0:
                        block = text[i:j+1]
                        control_keys = ('"emotion"', '"actions"', '"primary"', '"secondary"', '"intensity"')
                        if any(k in block for k in control_keys):
                            i = j + 1
                            break
                        result.append(text[i])
                        i += 1
                        break
                j += 1
            else:
                result.append(text[i])
                i += 1
        else:
            result.append(text[i])
            i += 1
    return ''.join(result)


def _strip_markdown(text: str) -> str:
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    t = re.sub(r'\*(.+?)\*', r'\1', t, flags=re.DOTALL)
    t = re.sub(r'__(.+?)__', r'\1', t, flags=re.DOTALL)
    t = re.sub(r'_(.+?)_', r'\1', t, flags=re.DOTALL)
    t = re.sub(r'`(.+?)`', r'\1', t, flags=re.DOTALL)
    t = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', t, flags=re.DOTALL)
    t = re.sub(r'#{1,6}\s+', '', t)
    t = t.replace('*', '')
    return t


def _normalize_tts_text(text: str) -> str:
    """清理文本中的特殊字符，避免 edge-tts 合成失败"""
    t = text.strip()
    t = re.sub(r'```json\s*\{.*?\}\s*```', '', t, flags=re.DOTALL)
    t = re.sub(r'```.*?```', '', t, flags=re.DOTALL)
    t = _strip_control_json(t)
    t = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', t)
    t = re.sub(r'[\u200b-\u200f\u2028-\u202f\ufeff]', '', t)
    t = re.sub(r'[（）\(\)]', '', t)
    t = _strip_markdown(t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def _edge_tts_sync(text: str, voice_id: str, cache_path: Path) -> bool:
    """[已废弃] 旧版每次 new+close event loop, 现统一走 _tts_run 共享 loop.
    保留仅为兼容旧引用 (实际已无 import)."""
    try:
        clean_text = _normalize_tts_text(text)
        if not clean_text or len(clean_text) < 2:
            return False
        # 关键修复: 改用共享 loop, 不再每次 new/close
        async def _synth():
            communicate = edge_tts.Communicate(clean_text, voice_id)
            audio = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio += chunk["data"]
            return audio
        audio_bytes = _tts_run(_synth())
        if audio_bytes and len(audio_bytes) > 100:
            cache_path.write_bytes(audio_bytes)
            return True
        return False
    except Exception as e:
        print(f"[edge-tts] 异常: {type(e).__name__}: {e}, text='{text[:40]}...'")
        return False


def _cache_hash(text: str, voice_id: str) -> str:
    """关键修复: 用 16 字符 hash (64bit) 替代 8 字符 (32bit),
    生日悖论: 8 字符约 6.5 万文本后 50% 碰撞, 16 字符 5×10^9 后才 50% 碰撞."""
    return hashlib.md5(f"{text}_{voice_id}".encode("utf-8")).hexdigest()[:16]


def clean_tts_cache(max_files: int = 500) -> int:
    """关键修复: TTS 缓存 LRU 清理, 超 max_files 删最老的 30%.
    返回删除的文件数."""
    try:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(AUDIO_DIR.glob("tts_cache_*.mp3"), key=lambda p: p.stat().st_mtime)
        if len(files) <= max_files:
            return 0
        # 删到只剩 max_files 的 70%
        target = int(max_files * 0.7)
        to_delete = files[:max(0, len(files) - target)]
        for f in to_delete:
            try:
                f.unlink()
            except OSError:
                pass
        return len(to_delete)
    except Exception as e:
        print(f"[clean_tts_cache] failed: {e}")
        return 0


def _try_siliconflow_fallback(text: str, voice_name: str) -> tuple[bytes, str] | tuple[None, None]:
    """关键修复: 死代码激活 - edge-tts 失败时降级到 SiliconFlow CosyVoice."""
    try:
        import tts_service
        # tts_service.text_to_speech 写文件, 不直接返 bytes
        url = tts_service.text_to_speech(text, output_path=str(AUDIO_DIR), voice_name=voice_name)
        if not url:
            return (None, None)
        # 解析 url 拿到实际文件
        fname = url.rsplit("/", 1)[-1]
        fp = AUDIO_DIR / fname
        if fp.exists():
            data = fp.read_bytes()
            return (data, url)
    except Exception as e:
        print(f"[siliconflow-fallback] failed: {e}")
    return (None, None)


def synthesize_tts(text: str, voice_name: str = "", tts_tag: str | None = None) -> str | None:
    text = _normalize_tts_text(text)
    if not text or len(text) < 2:
        return None

    voice_id = VOICE_MAP.get(voice_name, TTS_VOICE if voice_name else TTS_VOICE)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    text_hash = _cache_hash(text, voice_id)
    cache_path = AUDIO_DIR / f"tts_cache_{text_hash}.mp3"
    url_path = f"/static/audio/tts_cache_{text_hash}.mp3"

    if cache_path.exists() and cache_path.stat().st_size > 100:
        return url_path

    # 关键修复: per-hash 锁 + 锁内双检, 并发同文本时只合成一次
    with _tts_hash_lock(text_hash):
        if cache_path.exists() and cache_path.stat().st_size > 100:
            return url_path

        if len(text) > MAX_TTS_CHARS:
            print(f"[TTS] 文本过长({len(text)}>{MAX_TTS_CHARS}), 分批合成")
            # 关键修复: 超长文本分批合成, 拼接后写入自然缓存名 (tts_cache_{全文hash}.mp3),
            # 与 serve_audio 白名单 (tts_cache_[0-9a-f]{16}.mp3) 兼容, 下次直接命中缓存
            chunks = [text[i:i+MAX_TTS_CHARS] for i in range(0, len(text), MAX_TTS_CHARS)]
            combined_audio = b""
            for chunk in chunks:
                sub_audio, _ = synthesize_tts_bytes(chunk, voice_name=voice_name, tts_tag=tts_tag)
                if sub_audio:
                    combined_audio += sub_audio
            if combined_audio and len(combined_audio) > 100:
                try:
                    _atomic_write_bytes(cache_path, combined_audio)
                except OSError as e:
                    print(f"[TTS] 超长文本缓存写失败: {e}", flush=True)
                return url_path
            return None

        # 关键修复: 重试从 5 次降到 2 次 + 指数退避
        MAX_ATTEMPTS = 2
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if cache_path.exists() and cache_path.stat().st_size > 100:
                    return url_path

                # 用共享 loop 的 bytes 版, 不再每次 new+close
                audio_bytes, real_url = synthesize_tts_bytes(text, voice_name=voice_name, tts_tag=tts_tag)
                if audio_bytes and len(audio_bytes) > 100:
                    print(f"[TTS] OK attempt={attempt}, {len(audio_bytes)/1024:.1f}KB", flush=True)
                    # 关键修复: 降级路径下 real_url 指向真实 sf_ 文件,
                    # 不能返回从未写入的 tts_cache_ URL (前端 404)
                    return real_url or url_path

                if attempt < MAX_ATTEMPTS:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
            except Exception as e:
                print(f"[TTS] 异常 attempt={attempt}/{MAX_ATTEMPTS} [{type(e).__name__}]: {e}", flush=True)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(0.5 * (2 ** (attempt - 1)))

        # 关键修复: edge-tts 全部失败 → 降级到 SiliconFlow CosyVoice
        print(f"[TTS] edge-tts {MAX_ATTEMPTS}次均失败,降级到 SiliconFlow CosyVoice", flush=True)
        fb_audio, fb_url = _try_siliconflow_fallback(text, voice_name)
        if fb_url:
            return fb_url

        print(f"[TTS] 全部引擎失败, 返回 None", flush=True)
        return None


def synthesize_tts_bytes(text: str, voice_name: str = "", tts_tag: str | None = None) -> tuple[bytes, str] | tuple[None, None]:
    """Generate TTS audio, cache to file, return (raw_mp3_bytes, static_url_path).
    关键修复: 统一走共享 event loop; hash 16 字符; 重试 2 次; edge-tts 失败降级 SiliconFlow.
    关键修复: 加总超时 (8s) 防止某次 hang 拖死整个请求.
    关键修复: per-hash single-flight 锁 + 原子写盘 + 失败只删自建文件 + edge-tts 全局信号量."""
    text = _normalize_tts_text(text)
    if not text or len(text) < 2:
        return (None, None)

    voice_id = VOICE_MAP.get(voice_name, TTS_VOICE if voice_name else TTS_VOICE)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    text_hash = _cache_hash(text, voice_id)
    cache_path = AUDIO_DIR / f"tts_cache_{text_hash}.mp3"
    url_path = f"/static/audio/tts_cache_{text_hash}.mp3"

    if cache_path.exists() and cache_path.stat().st_size > 100:
        try:
            return (cache_path.read_bytes(), url_path)
        except OSError:
            pass

    with _tts_hash_lock(text_hash):
        # 双检: 锁内再查一次缓存, 并发同文本时只合成一次
        if cache_path.exists() and cache_path.stat().st_size > 100:
            try:
                return (cache_path.read_bytes(), url_path)
            except OSError:
                pass

        # 失败清理只针对本请求创建的文件, 不误删并发他人刚写好的有效缓存
        created_by_me = not cache_path.exists()

        if len(text) > MAX_TTS_CHARS:
            # 关键修复: 分批合成, 拼接后写入自然缓存名并返回其 URL,
            # 不再返回第一段的 URL (前端播放只听到第一段)
            chunks = [text[i:i+MAX_TTS_CHARS] for i in range(0, len(text), MAX_TTS_CHARS)]
            combined_audio = b""
            for chunk in chunks:
                sub_audio, _ = synthesize_tts_bytes(chunk, voice_name=voice_name, tts_tag=tts_tag)
                if sub_audio:
                    combined_audio += sub_audio
            if combined_audio and len(combined_audio) > 100:
                try:
                    _atomic_write_bytes(cache_path, combined_audio)
                except OSError as e:
                    print(f"[TTS-bytes] 超长文本缓存写失败: {e}", flush=True)
                return (combined_audio, url_path)
            return (None, None)

        # 关键修复: 重试 2 次, 指数退避
        MAX_ATTEMPTS = 2
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if cache_path.exists() and cache_path.stat().st_size > 100:
                    try:
                        return (cache_path.read_bytes(), url_path)
                    except OSError:
                        pass

                async def _synthesize():
                    # 关键修复: 单片段 8s 硬超时, 超过即抛 TimeoutError
                    communicate = edge_tts.Communicate(text, voice_id)
                    audio = b""
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            audio += chunk["data"]
                    return audio

                # 关键修复: edge-tts 全局并发上限 Semaphore(4), 防预热/多会话风暴
                _EDGE_TTS_SEMAPHORE.acquire()
                try:
                    # 关键修复: 8s 总超时; 若本请求被取消(tag)则立即停掉, 释放共享 loop
                    try:
                        if tts_tag:
                            audio_bytes = _tts_submit_tracked(_synthesize(), tts_tag).result(timeout=8.0)
                        else:
                            audio_bytes = _tts_run_with_timeout(_synthesize(), timeout=8.0)
                    except (asyncio.CancelledError, concurrent.futures.CancelledError):
                        print(f"[TTS-bytes] 合成被取消 (tag={tts_tag})", flush=True)
                        return (None, None)
                    except AttributeError:
                        # 兼容旧 _tts_run
                        audio_bytes = _tts_run(_synthesize())
                finally:
                    _EDGE_TTS_SEMAPHORE.release()

                if audio_bytes and len(audio_bytes) > 100:
                    try:
                        _atomic_write_bytes(cache_path, audio_bytes)
                    except OSError as e:
                        print(f"[TTS-bytes] 缓存写失败: {e}", flush=True)
                    print(f"[TTS-bytes] OK attempt={attempt}, {len(audio_bytes)/1024:.1f}KB", flush=True)
                    return (audio_bytes, url_path)

                # 失败只删自己创建的文件 (他人有效缓存保留, 下次直接命中)
                if created_by_me:
                    cache_path.unlink(missing_ok=True)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
            except Exception as e:
                print(f"[TTS-bytes] 异常 attempt={attempt}/{MAX_ATTEMPTS} [{type(e).__name__}]: {e}", flush=True)
                if created_by_me:
                    cache_path.unlink(missing_ok=True)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(0.5 * (2 ** (attempt - 1)))

        # 关键修复: edge-tts 失败降级 SiliconFlow
        print(f"[TTS-bytes] edge-tts 失败,降级 SiliconFlow", flush=True)
        return _try_siliconflow_fallback(text, voice_name)
