#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
import time
import uuid
import requests
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

from dotenv import load_dotenv
from runtime_paths import APP_ROOT, BACKEND_DIR, MODEL_DIR

load_dotenv(BACKEND_DIR / ".env")

import ai_service
from ai_service import chat_with_api, chat_with_api_stream, synthesize_tts, synthesize_tts_bytes, transcribe_audio, VOICE_MAP, TTS_VOICE, warmup_tts, _strip_control_json, _split_provider_output, sanitize_final_visible_text
from bundle_importer import build_bundle_knowledge
import database as db
from rag_vector import search_knowledge_vector, rebuild_collection
from hyde_retriever import search_with_hyde, generate_hyde_document, FACT_INDICATORS
from query_rewriter import rewrite_query
from grounding_check import verify_grounding
import deep_report
import amap_service
import blueprints
from blueprints import admin_core

BASE_DIR = APP_ROOT
TOURIST_DIR = BASE_DIR / "tourist-client"
ADMIN_DIR = BASE_DIR / "admin"
SESSION_MEMORY: dict[str, dict[str, Any]] = {}
SESSION_MEMORY_TTL_SECONDS = 30 * 60  # 30 分钟不活跃即回收
SESSION_MEMORY_LOCK = threading.Lock()
_SESSION_MEMORY_SWEEP_INTERVAL = 5 * 60
VOICE_UPLOAD_ATTEMPTS: dict[str, list[float]] = {}
VOICE_UPLOAD_ATTEMPTS_LOCK = threading.Lock()
VOICE_UPLOAD_ATTEMPT_WINDOW_SECONDS = 60
VOICE_UPLOAD_ATTEMPT_LIMIT = 12

# 关键修复: 同一 session 只允许一个进行中的流式生成. 新请求到达时取消旧生成,
# 避免上一轮(尤其其 TTS 合成)占着共享 TTS 事件循环, 把新一轮拖过 30s 前端超时而被吞掉.
_ACTIVE_GENS: dict[str, dict] = {}
_ACTIVE_GENS_LOCK = threading.Lock()


def _clear_active_gen(session_id: str, gen_token: dict) -> None:
    """清理本请求在 _ACTIVE_GENS 中的记录(仅当仍是最新那条)."""
    with _ACTIVE_GENS_LOCK:
        cur = _ACTIVE_GENS.get(session_id)
        if cur and cur.get("token") is gen_token:
            _ACTIVE_GENS.pop(session_id, None)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB 上传上限（语音/文件）
_LOCAL_CORS_ORIGINS = {"http://localhost:8088", "http://127.0.0.1:8088"}
_CONFIGURED_CORS_ORIGINS = {
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
_PUBLIC_CORS_ORIGINS = sorted(_LOCAL_CORS_ORIGINS | _CONFIGURED_CORS_ORIGINS)
CORS(
    app,
    resources={
        r"/api/v1/admin/.*": {"origins": []},
        r"/api/v1/.*": {"origins": _PUBLIC_CORS_ORIGINS},
        r"/static/audio/.*": {"origins": _PUBLIC_CORS_ORIGINS},
    },
    supports_credentials=False,
)

db.seed_data()

# ============= GLOBAL ERROR HANDLERS =============
@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"code": 404, "message": "接口不存在"}), 404
    return e, 404

@app.errorhandler(413)
def _too_large(e):
    return jsonify({"code": 413, "message": "上传文件超过 10MB 上限"}), 413

@app.errorhandler(Exception)
def _unhandled_exception(e):
    if request.path.startswith("/__debugger__"):
        return e
    app.logger.exception("unhandled error: %s", e)
    if request.path.startswith("/api/"):
        return jsonify({"code": 500, "message": "服务异常，已记录日志，请稍后重试"}), 500
    return jsonify({"code": 500, "message": "服务异常"}), 500

def _restore_runtime_settings(saved: dict[str, str]) -> None:
    """Restore only persisted settings that remain user-editable."""
    saved_voice = saved.get("ttsVoice")
    if isinstance(saved_voice, str) and saved_voice in ai_service.VOICE_MAP:
        ai_service.TTS_VOICE = ai_service.VOICE_MAP[saved_voice]


# Fixed system fields must come from the active runtime/environment, not stale
# values left in the legacy settings table. TTS voice remains an editable
# exception and is restored from its persisted friendly name.
_restore_runtime_settings(db.get_settings())


def _resolve_avatar_config(model_id: str | None = None) -> dict[str, Any]:
    """Resolve request-local avatar attributes from an enabled VRM model."""
    return db.get_avatar_public_config(MODEL_DIR, model_id=model_id)


# ============= BLUEPRINTS =============
# 管理端核心(认证/仪表盘/报表/设置/会话)注册在此; 其余蓝图按 P4 计划逐步迁移。
# re-export: 测试直接以 `main.<符号>` 方式访问/打补丁, 必须保持同一对象可见。
from blueprints.admin_core import (
    ADMIN_SESSION_TTL_SECONDS,
    LOGIN_ATTEMPTS,
    LOGIN_ATTEMPT_LIMIT,
    LOGIN_ATTEMPT_WINDOW_SECONDS,
    get_admin_credentials,
    issue_admin_token,
    get_admin_session,
    require_admin,
    _log_op,
)
from blueprints.common import parse_pagination, csv_safe_value

app.register_blueprint(admin_core.bp)
from blueprints import admin_content
app.register_blueprint(admin_content.bp)
from blueprints import data_screen
app.register_blueprint(data_screen.bp)


# ============= ADMIN AUTH =============

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _voice_upload_is_rate_limited(client_key: str) -> bool:
    """Bound ASR work per client without penalizing normal voice interactions."""
    now_ts = time.monotonic()
    with VOICE_UPLOAD_ATTEMPTS_LOCK:
        attempts = [
            ts for ts in VOICE_UPLOAD_ATTEMPTS.get(client_key, [])
            if now_ts - ts < VOICE_UPLOAD_ATTEMPT_WINDOW_SECONDS
        ]
        if len(attempts) >= VOICE_UPLOAD_ATTEMPT_LIMIT:
            VOICE_UPLOAD_ATTEMPTS[client_key] = attempts
            return True
        attempts.append(now_ts)
        VOICE_UPLOAD_ATTEMPTS[client_key] = attempts
        return False


# ============= KNOWLEDGE IMPORT =============

def import_bundle_knowledge():
    existing = db.get_all_knowledge()
    bundle_sources = {item["source"] for item in existing if item.get("source") and (".docx" in item["source"] or "示范景区公开资料包" in item["source"])}

    imported = build_bundle_knowledge(BASE_DIR)
    ts = now_str()
    for item in imported:
        if item.get("source", "") not in bundle_sources:
            db.add_knowledge(
                title=item.get("title", "导入知识"),
                category=item.get("category", "景区资料"),
                tags=item.get("tags", []),
                content=item.get("content", ""),
                source=item.get("source", "示范景区公开资料包"),
                source_hash=item.get("source_hash", ""),
            )

    rebuilt = db.get_all_knowledge()
    rebuild_collection(rebuilt)


# ============= TEXT UTILITIES =============

def normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def tokenize(text: str) -> list[str]:
    raw = re.split(r"[，。！？、,.!?\s:/：；;\-]+", text or "")
    tokens = [t.strip().lower() for t in raw if t.strip()]
    compact = normalize(text)
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]+", compact)
    for chunk in chinese_chunks:
        if len(chunk) <= 8:
            tokens.append(chunk)
        for size in (2, 3, 4):
            if len(chunk) < size:
                continue
            for idx in range(len(chunk) - size + 1):
                tokens.append(chunk[idx:idx + size])
    return list(dict.fromkeys(t for t in tokens if t))


# ============= INTEREST / EMOTION / STYLE INFERENCE =============

def infer_interest(message: str, preferred: str | None = None) -> str:
    if preferred in {"history", "nature", "family", "relax"}:
        return preferred
    mapping = {
        "history": ["历史", "文化", "建筑", "佛教", "讲解", "知识"],
        "nature": ["自然", "风景", "拍照", "山水", "风光"],
        "family": ["亲子", "孩子", "家庭", "带娃", "互动"],
        "relax": ["轻松", "慢慢", "休息", "舒适", "不累"],
    }
    for interest, words in mapping.items():
        if any(w in message for w in words):
            return interest
    return "history"


from emotion_engine import build_emotion_response, analyze_sentiment_score, estimate_tts_duration_ms, mp3_duration_ms
from emotion_state import emotion_sm

SIMPLE_EMOTION_MAP = {
    "delighted": {"label": "积极回应", "cssClass": "emotion-delighted"},
    "focused":   {"label": "高效导览", "cssClass": "emotion-focused"},
    "caring":    {"label": "安抚陪伴", "cssClass": "emotion-caring"},
    "sad":       {"label": "同情理解", "cssClass": "emotion-sad"},
    "surprised": {"label": "惊喜回应", "cssClass": "emotion-surprised"},
    "neutral":   {"label": "正常讲解", "cssClass": "emotion-neutral"},
}

EMOTION_LABEL_MAP = {
    "joy":  "积极回应", "trust": "亲和讲解", "fear": "担忧关注",
    "surprise": "惊喜回应", "sadness": "同情理解", "disgust": "耐心倾听",
    "anger": "认真讲解", "anticipation": "期待推荐",
}

EMOTION_CSS_MAP = {
    "joy": "emotion-delighted", "trust": "emotion-warm", "fear": "emotion-caring",
    "surprise": "emotion-surprised", "sadness": "emotion-caring", "disgust": "emotion-neutral",
    "anger": "emotion-focused", "anticipation": "emotion-warm",
}

def infer_emotion_simple(message: str, expression_bias: str = "warm") -> str:
    sent = analyze_sentiment_score(message)
    label = sent.get("label", "neutral")
    if label in ("strong_positive", "positive"):
        return "delighted"
    if label == "urgent":
        return "focused"
    if label in ("concern", "negative", "strong_negative"):
        return "caring"
    return "warm"


def emotion_payload(emotion: str) -> dict[str, str]:
    mapping = {
        "warm": {"label": "亲和讲解", "avatarState": "smile", "gesture": "open-hand", "cssClass": "emotion-warm"},
        "delighted": {"label": "积极回应", "avatarState": "bright-smile", "gesture": "wave", "cssClass": "emotion-delighted"},
        "focused": {"label": "高效导览", "avatarState": "attentive", "gesture": "point", "cssClass": "emotion-focused"},
        "caring": {"label": "安抚陪伴", "avatarState": "gentle", "gesture": "comfort", "cssClass": "emotion-caring"},
        "sad": {"label": "同情理解", "avatarState": "sad", "gesture": "comfort", "cssClass": "emotion-sad"},
        "surprised": {"label": "惊喜", "avatarState": "surprised", "gesture": "wave", "cssClass": "emotion-surprised"},
        "neutral": {"label": "正常讲解", "avatarState": "neutral", "gesture": "idle", "cssClass": "emotion-neutral"},
    }
    return mapping.get(emotion, mapping["warm"])


def route_for_interest(interest: str) -> dict:
    routes = db.get_routes()
    for r in routes:
        if r["interest"] == interest:
            return r
    return routes[0] if routes else {}


# ============= WEAK GPS POSITIONING INFERENCE =============

SPOT_COORDINATES = {
    "灵山广场": {"lat": 31.485, "lng": 120.115, "zone": "入口区"},
    "灵山大佛": {"lat": 31.488, "lng": 120.118, "zone": "核心区"},
    "九龙灌浴": {"lat": 31.486, "lng": 120.116, "zone": "核心区"},
    "梵宫": {"lat": 31.487, "lng": 120.120, "zone": "核心区"},
    "祥符禅寺": {"lat": 31.489, "lng": 120.117, "zone": "核心区"},
    "五印坛城": {"lat": 31.486, "lng": 120.122, "zone": "东区"},
    "佛足坛": {"lat": 31.484, "lng": 120.113, "zone": "入口区"},
    "五明桥": {"lat": 31.484, "lng": 120.113, "zone": "入口区"},
    "五智门": {"lat": 31.485, "lng": 120.114, "zone": "入口区"},
    "无尽意斋": {"lat": 31.487, "lng": 120.119, "zone": "核心区"},
    "拈花广场": {"lat": 31.490, "lng": 120.125, "zone": "东区"},
    "梵天花海": {"lat": 31.492, "lng": 120.128, "zone": "东区"},
    "香月花街": {"lat": 31.491, "lng": 120.126, "zone": "东区"},
    "拈花堂": {"lat": 31.490, "lng": 120.127, "zone": "东区"},
    "五灯湖": {"lat": 31.489, "lng": 120.129, "zone": "东区"},
    "鹿鸣谷": {"lat": 31.493, "lng": 120.132, "zone": "远郊"},
    "游客中心": {"lat": 31.483, "lng": 120.112, "zone": "入口区"},
    "湖景步道": {"lat": 31.491, "lng": 120.130, "zone": "东区"},
    "观景平台": {"lat": 31.490, "lng": 120.131, "zone": "东区"},
    "静心休憩区": {"lat": 31.488, "lng": 120.121, "zone": "核心区"},
    "文化商店": {"lat": 31.487, "lng": 120.123, "zone": "核心区"},
    "休闲补给区": {"lat": 31.485, "lng": 120.119, "zone": "入口区"},
    "马山": {"lat": 31.429, "lng": 120.095, "zone": "核心区"},
}

ROUTE_GRAPH = {
    "入口区": ["核心区"],
    "核心区": ["入口区", "东区"],
    "东区": ["核心区", "远郊"],
    "远郊": ["东区"],
}


def detect_spot_name(message: str) -> str | None:
    mapping = {
        "灵山大佛": ["灵山大佛", "大佛"],
        "梵宫": ["梵宫"],
        "九龙灌浴": ["九龙灌浴"],
        "祥符禅寺": ["祥符禅寺"],
        "五印坛城": ["五印坛城"],
        "佛足坛": ["佛足坛"],
        "五明桥": ["五明桥"],
        "无尽意斋": ["无尽意斋"],
        "五智门": ["五智门"],
        "拈花广场": ["拈花广场"],
        "梵天花海": ["梵天花海"],
        "香月花街": ["香月花街"],
        "拈花堂": ["拈花堂"],
        "五灯湖": ["五灯湖"],
        "鹿鸣谷": ["鹿鸣谷"],
        "马山": ["马山", "马山镇"],
    }
    norm = normalize(message)
    for canonical, aliases in mapping.items():
        if any(a in norm for a in aliases):
            return canonical
    return None


_FOLLOWUP_PATTERNS = [
    "在哪", "在哪里", "在哪儿", "哪里", "哪儿",
    "多少钱", "票价", "价格多少",
    "什么时候", "几点", "开放时间", "营业时间",
    "怎么去", "怎么走", "如何到达", "怎么到",
    "是什么", "有什么", "啥",
    "怎么样", "如何", "好不好", "值得吗",
    "有什么特色", "有什么看点", "哪里好玩",
    "怎么过去", "在哪边", "哪个位置",
]


def _is_followup_query(message: str) -> bool:
    norm = normalize(message)
    if len(norm) <= 3:
        return True
    if any(p in norm for p in _FOLLOWUP_PATTERNS):
        return True
    return False


def infer_position(session_mem: dict | None, message: str) -> dict:
    """
    Infer the user's current location using:
    1. GPS coordinates (if provided in request)
    2. Mentioned spot names in this message
    3. Previous session location history
    4. Route position logic (if following a route, estimate next stop)
    """
    result = {
        "estimatedSpot": None,
        "estimatedZone": None,
        "confidence": "low",
        "nearbySpots": [],
        "method": "none",
    }

    mentioned = detect_spot_name(message)
    if mentioned and mentioned in SPOT_COORDINATES:
        coord = SPOT_COORDINATES[mentioned]
        result["estimatedSpot"] = mentioned
        result["estimatedZone"] = coord["zone"]
        result["confidence"] = "high"
        result["method"] = "direct_mention"
        result["nearbySpots"] = [s for s, c in SPOT_COORDINATES.items() if c["zone"] == coord["zone"] and s != mentioned][:3]
        return result

    if session_mem:
        last_spot = session_mem.get("lastSpot")
        last_zone = session_mem.get("lastZone")
        current_route = session_mem.get("currentRouteIndex", -1)
        route_stops = session_mem.get("routeStops", [])

        if current_route >= 0 and route_stops:
            next_idx = current_route + 1
            if next_idx < len(route_stops):
                stop_name = route_stops[next_idx] if isinstance(route_stops[next_idx], str) else route_stops[next_idx].get("name", "")
                if stop_name and stop_name in SPOT_COORDINATES:
                    coord = SPOT_COORDINATES[stop_name]
                    result["estimatedSpot"] = stop_name
                    result["estimatedZone"] = coord["zone"]
                    result["confidence"] = "medium"
                    result["method"] = "route_progression"
                    result["nearbySpots"] = [s for s, c in SPOT_COORDINATES.items() if c["zone"] == coord["zone"] and s != stop_name][:3]
                    return result

        if last_spot and last_spot in SPOT_COORDINATES:
            coord = SPOT_COORDINATES[last_spot]
            result["estimatedSpot"] = last_spot
            result["estimatedZone"] = coord["zone"]
            result["confidence"] = "low"
            result["method"] = "session_history"
            result["nearbySpots"] = [s for s, c in SPOT_COORDINATES.items() if c["zone"] == coord["zone"] and s != last_spot][:3]

    return result


# ============= KNOWLEDGE / FAQ / FACT MATCHING =============

def is_noisy_knowledge_item(item: dict) -> bool:
    source = (item.get("source") or "").lower()
    text = " ".join([item.get("title", ""), item.get("category", ""), " ".join(item.get("tags", [])), item.get("content", "")])
    if source.endswith(".xlsx"):
        return True
    if text.count("|") >= 4:
        return True
    if "tourist_id" in text.lower() or "景点id" in text.lower():
        return True
    if len(text) > 1200:
        return True
    known_spots = ["灵山大佛", "梵宫", "九龙灌浴", "无尽意斋", "拈花堂", "五灯湖", "鹿鸣谷", "香月花街", "梵天花海"]
    if sum(1 for s in known_spots if s in text) >= 4:
        return True
    return False


def sanitize_reply_text(text: str) -> str:
    cleaned = text or ""
    cleaned = re.sub(r"```json\s*\{.*?\}\s*```", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL)
    cleaned = _strip_control_json(cleaned)
    cleaned = re.sub(r"\b[A-Z]{1,5}-\d{2,5}\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(景点ID|知识库ID|source_hash|updated_at|tourist_id|user_nickname)\s*[:：]?\s*\S+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[，。；、]{2,}", "。", cleaned)
    cleaned = _strip_markdown(cleaned)
    cleaned = re.sub(r"<[^>]*>", "", cleaned)
    return cleaned.strip(" ,，。；、:：")


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


def split_tts_segments(text: str, max_chars: int = 300) -> list[str]:
    cleaned = re.sub(r'```json\s*\{.*?\}\s*```', '', text, flags=re.DOTALL)
    cleaned = re.sub(r'```.*?```', '', cleaned, flags=re.DOTALL)
    cleaned = _strip_control_json(cleaned)
    cleaned = _strip_markdown(cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if not cleaned:
        return []
    sentences = re.split(r'(?<=[。！？])', cleaned)
    segments, buf = [], ""
    for s in sentences:
        if not s.strip():
            continue
        if len(buf) + len(s) > max_chars and buf:
            segments.append(buf.strip())
            buf = s
        else:
            buf += s
    if buf.strip():
        segments.append(buf.strip())
    return segments or [text[:max_chars]]


def compress_reply(text: str, max_sentences: int = 999, max_chars: int = 2000) -> str:
    cleaned = sanitize_reply_text(text)
    if not cleaned:
        return cleaned
    sentences = [s.strip() for s in re.split(r"(?<=[。！？])", cleaned) if s.strip()]
    if sentences:
        cleaned = "".join(sentences[:max_sentences]).strip()
    if len(cleaned) > max_chars:
        clipped = cleaned[:max_chars].rstrip("，、；： ")
        if not clipped.endswith(("。", "！", "？")):
            clipped += "。"
        cleaned = clipped
    return cleaned


def safe_visitor_reply(candidate: str, fallback: str = "") -> str:
    """Return only visitor-safe text; never persist or render control leakage."""
    for value in (candidate, fallback):
        safe = sanitize_final_visible_text(compress_reply(value or ""))
        if safe:
            return safe
    return "我暂时没有查到相关信息，建议您咨询景区服务中心。"


def pop_complete_stream_sentences(text: str) -> tuple[list[str], str]:
    """Return only sentence-complete text so incomplete provider output stays private."""
    pending = text or ""
    sentences: list[str] = []
    while True:
        match = re.match(r"^\s*(.*?[。！？!?；;\n])", pending, flags=re.DOTALL)
        if not match:
            break
        sentence = match.group(1).strip()
        if sentence:
            sentences.append(sentence)
        pending = pending[match.end():]
    return sentences, pending


def strip_streamed_tts_prefix(final_reply: str, streamed_reply: str) -> str:
    """Remove an already-synthesized prefix while treating whitespace as non-semantic."""
    final_idx = 0
    streamed_idx = 0
    while final_idx < len(final_reply) and streamed_idx < len(streamed_reply):
        if final_reply[final_idx].isspace():
            final_idx += 1
            continue
        if streamed_reply[streamed_idx].isspace():
            streamed_idx += 1
            continue
        if final_reply[final_idx] != streamed_reply[streamed_idx]:
            return final_reply
        final_idx += 1
        streamed_idx += 1
    while streamed_idx < len(streamed_reply) and streamed_reply[streamed_idx].isspace():
        streamed_idx += 1
    return final_reply[final_idx:].lstrip() if streamed_idx == len(streamed_reply) else final_reply


def detect_query_style(message: str) -> str:
    norm = normalize(message)
    if any(k in norm for k in ["推荐", "怎么玩", "怎么逛", "安排", "适合"]):
        return "recommendation"
    if any(k in norm for k in ["特色", "亮点", "看点", "值得", "为什么"]):
        return "feature"
    if any(k in norm for k in ["故事", "传说", "典故", "来历", "起源", "历史背景", "文化背景"]):
        return "story"
    if any(k in norm for k in ["是什么", "介绍", "讲讲", "是什么样", "什么地方"]):
        return "intro"
    return "generic"


def detect_fact_query(message: str) -> str | None:
    norm = normalize(message)
    if any(k in norm for k in ["适合", "适合老人", "适合小孩", "适合孩子", "适合全家", "适合带娃"]):
        return "suitability"
    mapping = {
        "height": ["多高", "高度", "高多少", "通高"],
        "material": ["什么材质", "什么材料", "什么做的", "材料", "制成", "铸造", "铸成", "构成", "材质"],
        "ticket": ["门票", "票价", "多少钱", "收费", "免费", "票务", "价格"],
        "opening_hours": ["开放时间", "几点开门", "几点关门", "营业时间", "几点开", "几点关"],
        "showtime": ["演出时间", "表演时间", "演出几点", "表演几点", "吉祥颂", "演出场次", "演几场", "几点演",
                     "九龙灌浴.*时间", "表演.*开始", "演出.*开始"],
        "date_fact": ["哪年", "哪一年", "何时", "什么时候建", "什么时候修", "什么时候开放", "什么时候建成",
                      "建成", "落成", "开光", "始建", "修建", "建造时间", "开放于"],
        "location": ["在哪", "在哪里", "位置", "地址", "怎么走", "位于"],
        "service_facility": ["轮椅", "卫生间", "厕所", "停车", "无障碍", "医务室", "存包"],
        "activity": ["特色活动", "有什么活动", "有什么好玩", "体验项目", "互动体验", "抄经", "吉祥颂"],
        "policy": ["注意事项", "注意什么", "有什么注意", "禁忌", "规定"],
        "discount": ["优惠", "半价", "免费", "免票", "老人票", "学生票", "儿童票", "优待", "什么条件"],
    }
    for fact_type, keywords in mapping.items():
        for keyword in keywords:
            if keyword in norm:
                return fact_type
            if any(ch in keyword for ch in ".*+?[]{}()|\\"):
                try:
                    if re.search(keyword, norm):
                        return fact_type
                except re.error:
                    pass
    return None


def search_faq(message: str) -> dict | None:
    norm = normalize(message)
    # Try FTS5 first
    fts_results = db.search_faq_fts(message)
    for item in fts_results:
        q = normalize(item.get("question", ""))
        if not q:
            continue
        if len(q) >= 4 and (q in norm or norm in q):
            return item
    # Fallback: exact match across all FAQ items
    items = db.get_faq()
    for item in items:
        q = normalize(item.get("question", ""))
        if not q:
            continue
        if len(q) >= 4 and (q in norm or norm in q):
            return item
    # Keyword metadata is curated for Chinese paraphrases that do not share
    # enough text with the FAQ title to pass the substring check above.
    keyword_results = db.search_faq_by_keywords(message)
    if keyword_results:
        item = keyword_results[0]
        try:
            keywords = json.loads(item.get("keywords", "[]"))
        except (json.JSONDecodeError, TypeError):
            keywords = []
        matched = [keyword for keyword in keywords if keyword and keyword in norm]
        if matched and max(len(keyword) for keyword in matched) >= 3:
            return item
    return None


def _faq_by_question(question: str) -> dict | None:
    for item in db.get_faq():
        if item.get("question") == question:
            return item
    return None


def match_operational_faq(message: str) -> dict | None:
    """Match high-risk visitor facts before generic FAQ and knowledge retrieval."""
    norm = normalize(message)
    rules = [
        ("门票包含哪些项目？", [["门票", "大门票", "儿童票", "吉祥颂", "梵宫演出", "演出", "表演", "九龙灌浴"],
                         ["包含", "包括", "含", "另购", "单独买", "单买", "另外买", "免费", "收费", "多少钱", "价格", "需不需要", "要不要"]]),
        ("儿童票政策是什么？", [["儿童", "小孩", "孩子", "小朋友"], ["免票", "免费", "多高", "身高", "收费", "票"]]),
        ("景区开放时间？", [["开放", "开门", "开园", "关门", "闭园", "停止入园", "营业"], ["时间", "几点", "入园", "开", "关"]]),
        ("门票多少钱？", [["门票", "大门票", "成人票", "优惠票", "老人票", "学生票", "票价"], ["多少钱", "价格", "收费", "票价"]]),
        ("九龙灌浴表演时间？", [["九龙灌浴"], ["时间", "几点", "演出", "表演", "场次"]]),
        ("景区有演出吗？", [["梵宫", "吉祥颂"], ["时间", "几点", "演出", "表演", "场次"]]),
        ("停车场在哪里？", [["停车", "停车场"], []]),
        ("卫生间位置？", [["卫生间", "厕所", "洗手间"], []]),
        ("有无障碍设施吗？", [["轮椅", "无障碍"], []]),
        ("宠物可以带进景区吗？", [["宠物", "带狗", "带猫"], []]),
        ("游览一遍需要多久？", [["游玩", "游览", "逛", "一圈"], ["多久", "多长", "几小时", "时间"]]),
        ("景区特色活动有哪些？", [["活动", "体验", "好玩"], []]),
    ]
    for question, required_groups in rules:
        if all(not group or any(token in norm for token in group) for group in required_groups):
            item = _faq_by_question(question)
            if item:
                return item
    return None


def match_strict_fact(message: str) -> dict | None:
    results = db.search_faq_by_keywords(message)
    if results:
        item = results[0]
        norm = normalize(message)
        q = normalize(item.get("question", ""))
        # Quality gate: if query and FAQ question aren't substring-related,
        # require keyword coverage ≥ 50% of query length to avoid weak matches
        if q not in norm and norm not in q:
            try:
                kws = json.loads(item.get("keywords", "[]"))
            except (json.JSONDecodeError, TypeError):
                kws = []
            matched_len = sum(len(kw) for kw in kws if kw in norm)
            query_len = max(len(norm), 1)
            if matched_len / query_len <= 0.5:
                return None
        return {
            "topic": item.get("category", "景区信息"),
            "source": item.get("question", ""),
            "keywords": json.loads(item.get("keywords", "[]")),
            "answer": item.get("answer", ""),
        }
    return None


def _extract_count(message: str, keywords: list[str]) -> int:
    digit_patterns = [r"(\d+)\s*(?:位|个|名)?", r"([零一二两三四五六七八九十百]+)\s*(?:位|个|名)?"]
    cn_map = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

    def parse_cn(s: str) -> int:
        if s.isdigit():
            return int(s)
        if s == "十":
            return 10
        if "十" in s:
            parts = s.split("十", 1)
            tens = cn_map.get(parts[0], 1) if parts[0] else 1
            ones = cn_map.get(parts[1], 0) if parts[1] else 0
            return tens * 10 + ones
        return cn_map.get(s, 0)

    for kw in keywords:
        for pat in digit_patterns:
            m = re.search(rf"{pat}{kw}|{kw}{pat}", message)
            if not m:
                continue
            groups = [g for g in m.groups() if g]
            if groups:
                c = parse_cn(groups[0])
                if c > 0:
                    return c
    return 0


def match_ticket_pricing(message: str) -> dict | None:
    norm = normalize(message)
    ticket_kw = ["门票", "票价", "多少钱", "几张票", "价格", "花多少钱", "总价"]
    people_kw = ["大人", "成人", "儿童", "小孩", "孩子", "老人", "老年人", "学生"]
    if not any(k in norm for k in ticket_kw):
        return None
    if not any(k in norm for k in people_kw):
        return None

    adult = _extract_count(message, ["大人", "成人"])
    child = _extract_count(message, ["小孩", "孩子", "儿童"])
    senior = _extract_count(message, ["老人", "老年人"])
    student = _extract_count(message, ["学生"])

    if adult == child == senior == student == 0:
        return None

    total = adult * 210 + (senior + student) * 105
    parts = []
    if adult:
        parts.append(f"成人 {adult} 位，共 {adult * 210} 元")
    if senior:
        parts.append(f"老人 {senior} 位，共 {senior * 105} 元")
    if student:
        parts.append(f"学生 {student} 位，共 {student * 105} 元")
    if child:
        parts.append(f"儿童 {child} 位，1.4 米以下免费，超高儿童需以景区现场规则为准")

    reply = f"按当前票务规则帮您算一下：{'；'.join(parts)}。"
    if adult or senior or student:
        reply += f" 目前可直接确定的合计是 {total} 元。"
    if child:
        reply += " 如果孩子身高在 1.4 米以下，这部分通常免费。"
    reply += " 梵宫《灵山吉祥颂》演出票需另购。"
    return {"topic": "门票组合报价", "source": "门票标准答复", "answer": reply}


def match_smalltalk(message: str) -> str | None:
    norm = normalize(message)
    if any(k in norm for k in ["你好", "您好", "hi", "hello", "嗨", "哈喽", "在吗", "有人吗", "你是谁", "你会什么", "你能做什么"]):
        return "您好，我是景区 AI 导览助手小灵。我可以陪您聊天，也可以帮您介绍景点、推荐游览路线、回答门票和服务设施等问题。您可以直接问我，比如'有什么必看景点'或'帮我推荐一条亲子路线'。"
    if any(k in norm for k in ["男生还是女生", "你是男的还是女的", "你是女孩还是男孩", "你多少岁", "你多大了", "你叫什么", "你的名字"]):
        return "我是小灵，灵山胜境的 AI 导览助手，很高兴为您服务！我没有性别，但设计上偏亲和温暖，您可以把我当作一个懂景区知识、乐于帮忙的朋友。"
    return None


# ============= VECTOR KNOWLEDGE SEARCH =============

def search_knowledge(message: str) -> list[dict]:
    is_fact = any(w in message for w in FACT_INDICATORS)
    if is_fact:
        rewritten = rewrite_query(message)
        hyde_doc = generate_hyde_document(message)
        results = search_with_hyde(message, search_knowledge_vector, top_k=10,
                                    rewritten_query=rewritten, precomputed_hyde=hyde_doc)
    else:
        rewritten = rewrite_query(message)
        results = search_with_hyde(message, search_knowledge_vector, top_k=10, rewritten_query=rewritten)
    cleaned = []
    for item in results:
        if is_noisy_knowledge_item(item):
            continue
        item["content"] = sanitize_reply_text(item.get("content", ""))
        cleaned.append(item)
    return cleaned[:4]


def compute_confidence(evidence: list[dict], message: str) -> float:
    if not evidence:
        return 0.0
    max_score = max(item.get("score", 0) for item in evidence)
    if any(item.get("retriever") == "keyword-fallback" for item in evidence):
        max_score = min(max_score, 0.5)
    norm = normalize(message)
    if detect_fact_query(message):
        spot = detect_spot_name(message)
        if spot:
            has_spot = any(spot in item.get("content", "") for item in evidence)
            if not has_spot:
                max_score *= 0.5
        return min(max_score, 0.95)
    return max_score


def safe_fallback_reply(message: str, confidence: float) -> str | None:
    if confidence < 0.35 and detect_fact_query(message):
        return "这个问题我暂时没有找到准确的景区资料，建议您咨询景区游客服务中心以获得最准确的信息。"
    if confidence < 0.25:
        return "我暂时没有查到完全对应的景区信息，您可以换个问法或者咨询景区现场工作人员。"
    return None


def _candidate_sentences(evidence: list[dict]) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for item in evidence[:3]:
        content = sanitize_reply_text(item.get("content", ""))
        for seg in re.split(r"[。；]", content):
            s = seg.strip()
            if len(s) < 8 or s in seen:
                continue
            seen.add(s)
            candidates.append(s)
    return candidates


def _score_sentence(sentence: str, message: str, spot_name: str | None, query_style: str) -> int:
    score = 0
    tokens = [t for t in tokenize(message) if len(t) >= 2 and t not in {"什么", "一下", "这个", "那个"}]
    for t in tokens:
        if t in sentence:
            score += 2
    if spot_name and spot_name in sentence:
        score += 4
    if query_style == "intro" and ("是" in sentence or "位于" in sentence):
        score += 2
    if query_style == "feature" and any(k in sentence for k in ["核心", "特色", "适合", "亮点", "象征"]):
        score += 3
    if query_style == "recommendation" and any(k in sentence for k in ["适合", "建议", "可", "体验"]):
        score += 3
    if query_style == "story" and any(k in sentence for k in ["历史", "传说", "典故", "建于", "始建", "起源", "由来", "创建"]):
        score += 5
    if any(k in sentence for k in ["开放时间", "禁止", "费用", "公告", "预约"]):
        score -= 2
    return score


def summarize_evidence_answer(message: str, evidence: list[dict]) -> str | None:
    if not evidence:
        return None
    primary = evidence[0]
    query_style = detect_query_style(message)
    spot_name = detect_spot_name(message)
    candidates = _candidate_sentences(evidence)
    if not candidates:
        return None

    ranked = sorted(candidates, key=lambda s: _score_sentence(s, message, spot_name, query_style), reverse=True)
    selected = [s for s in ranked if _score_sentence(s, message, spot_name, query_style) > 0][:2] or ranked[:2]

    if query_style == "intro":
        first = selected[0]
        if spot_name and spot_name not in first:
            first = f"{spot_name}是景区里的一个特色点位。" if "是" not in first else f"{spot_name}{first}"
        selected[0] = first

    if query_style == "feature" and spot_name and selected:
        if "值得" in message:
            selected[0] = f"{spot_name}还是比较值得去看的，{selected[0].lstrip('，。')}"
        elif "适合" in message:
            selected[0] = f"{spot_name}比较适合想深度体验景区内容的游客，{selected[0].lstrip('，。')}"
        elif spot_name not in selected[0]:
            selected[0] = f"{spot_name}的亮点主要在于{selected[0]}"

    if query_style == "recommendation" and spot_name and selected:
        if "适合" not in selected[0]:
            selected[0] = f"{spot_name}比较适合想深入体验景区内容的游客。"

    if query_style == "story" and selected:
        if spot_name and spot_name not in selected[0]:
            selected[0] = f"{spot_name}有着悠久的历史，{selected[0].lstrip('，。')}"

    answer = "。".join(s.rstrip("。") for s in selected if s).strip()
    if answer and not answer.endswith("。"):
        answer += "。"
    return compress_reply(answer)


def extract_spot_intro(message: str, evidence: list[dict] | None = None) -> dict | None:
    spot_name = detect_spot_name(message)
    if not spot_name:
        return None
    norm = normalize(message)
    if not any(k in norm for k in ["是什么", "介绍", "讲讲", "怎么样", "值得", "看看", "亮点", "特色", "适合", "推荐"]):
        return None
    fact_type = detect_fact_query(message)
    if fact_type:
        return None

    if evidence is None:
        evidence = search_knowledge(message)
    if not evidence:
        return None

    summary = summarize_evidence_answer(message, evidence)
    if summary:
        if not summary.startswith(spot_name):
            summary = f"{spot_name}是景区里的一个特色点位。{summary}"
        return {"topic": f"{spot_name}介绍", "source": evidence[0].get("title") or spot_name, "answer": compress_reply(summary)}
    return None


def extract_spot_fact(message: str, evidence: list[dict] | None = None) -> dict | None:
    fact_type = detect_fact_query(message)
    spot_name = detect_spot_name(message)
    if not fact_type or not spot_name:
        return None

    if evidence is None:
        evidence = search_knowledge(message)
    for item in evidence:
        content = item.get("content", "")
        if spot_name not in content:
            continue
        cleaned = sanitize_reply_text(content)

        if fact_type == "height":
            if "佛脚" in message:
                m = re.search(r"佛脚到地面距离\s*([0-9]+)\s*米", content)
                if m:
                    return {"topic": f"{spot_name}高度", "source": item.get("title") or spot_name, "answer": f"{spot_name}的佛脚到地面距离{m.group(1)}米，相当于27层楼高。"}
                m = re.search(r"佛脚.*?地面.*?([0-9]+)\s*米", content)
                if m:
                    return {"topic": f"{spot_name}高度", "source": item.get("title") or spot_name, "answer": f"{spot_name}的佛脚到地面距离{m.group(1)}米。"}
                return {"topic": f"{spot_name}高度", "source": item.get("title") or spot_name, "answer": f"{spot_name}的佛脚到地面距离81米，相当于27层楼高。"}
            m = re.search(r"(?:通高|高|佛像高)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:米|m)", content, re.IGNORECASE)
            if m:
                meters = m.group(1)
                return {"topic": f"{spot_name}高度", "source": item.get("title") or spot_name, "answer": f"{spot_name}通高{meters}米。"}

        if fact_type == "location":
            segs = re.split(r"[。；]", cleaned)
            for seg in segs:
                if any(t in seg for t in ["位于", "处于", "在景区", "地址"]):
                    ans = sanitize_reply_text(seg)
                    if ans:
                        return {"topic": f"{spot_name}位置", "source": item.get("title") or spot_name, "answer": ans if ans.startswith(spot_name) else f"{spot_name}{ans}"}
            return {"topic": f"{spot_name}位置", "source": item.get("title") or spot_name, "answer": f"{spot_name}位于景区核心区，是灵山胜境的标志性景点之一，具体位置可查看景区导览图。"}

        if fact_type == "material":
            mat_kw = ["青铜", "铜铸", "铜像", "青铜铸造", "铜", "铸造", "铸铜", "锻铜", "石材", "石雕"]
            segs = re.split(r"[。；]", cleaned)
            for seg in segs:
                if any(t in seg for t in mat_kw):
                    ans = sanitize_reply_text(seg)
                    if ans:
                        return {"topic": f"{spot_name}材质", "source": item.get("title") or spot_name, "answer": ans if ans.startswith(spot_name) else f"{spot_name}{ans}"}
            return {"topic": f"{spot_name}材质", "source": item.get("title") or spot_name, "answer": f"{spot_name}为青铜铸造，高88米，是目前世界上最高的青铜佛像之一。"}

        if fact_type == "ticket":
            if "免费" in cleaned or "无门票" in cleaned:
                return {"topic": f"{spot_name}门票", "source": item.get("title") or spot_name, "answer": f"{spot_name}免费开放，无需单独购票。"}

        if fact_type in ("service_facility", "activity") and any(t in cleaned for t in ["轮椅", "卫生间", "活动", "抄经", "演出"]):
            return {"topic": f"{spot_name}服务", "source": item.get("title") or spot_name, "answer": sanitize_reply_text(cleaned[:200])}
    return None


def match_food_recommendation(message: str, evidence: list[dict] | None = None) -> dict | None:
    norm = normalize(message)
    food_kw = ["特色餐饮", "餐饮", "美食", "吃什么", "有什么吃的", "有什么好吃的",
               "推荐餐饮", "推荐吃的", "哪里吃饭", "吃饭推荐", "素斋", "素面",
               "小孩吃", "孩子吃", "儿童餐", "小朋友吃", "适合小孩", "适合孩子",
               "推荐吃", "吃的推荐", "好吃", "吃啥", "吃点啥"]
    if not any(k in norm for k in food_kw):
        return None

    if evidence is None:
        evidence = search_knowledge(message)
    food_lines = []
    for item in evidence:
        c = sanitize_reply_text(item.get("content", ""))
        if c and any(t in c for t in ["素斋", "素面", "餐饮", "禅茶", "美食"]):
            food_lines.append(c)

    has_child = any(k in norm for k in ["小孩", "孩子", "儿童", "小朋友", "亲子", "宝宝", "带娃"])

    named = []
    for p in [r"梵宫素斋自助", r"素面套餐", r"灵山精舍素斋", r"禅茶品鉴"]:
        if any(re.search(p, line) for line in food_lines):
            named.append(p)

    if has_child:
        base = "景区内亲子游客推荐去梵宫品尝素斋自助，菜品丰富且清淡健康，适合小朋友。此外景区也有素面套餐等简餐选择。"
        if named:
            lead = "、".join(named[:3])
            return {"topic": "亲子餐饮", "source": "餐饮推荐", "answer": f"景区里比较适合带孩子吃的有{lead}，整体以清淡素食为主，小朋友一般都能接受。如果孩子比较小，素面套餐可能更合适。"}
        return {"topic": "亲子餐饮", "source": "餐饮推荐", "answer": base}

    if not named:
        return {"topic": "特色餐饮", "source": "餐饮推荐", "answer": "景区内以素斋、素面和禅意茶饮为主，比较适合想体验清淡饮食和佛教文化氛围的游客。"}
    lead = "、".join(named[:3])
    ans = f"景区里比较有代表性的特色餐饮有{lead}，整体以清淡素食和禅意餐饮体验为主。"
    if "梵宫素斋自助" in named:
        ans += "如果想体验佛教文化氛围，梵宫素斋会更有代表性。"
    return {"topic": "特色餐饮", "source": "餐饮推荐", "answer": ans}


# ============= ANSWER BUILDER =============

def build_dialog_context(message: str, interest: str, avatar_config: dict | None = None, session_memory: dict | None = None) -> dict:
    # Cross-turn context: augment short follow-up queries with last mentioned spot
    augmented = message
    if session_memory and session_memory.get("lastSpot") and _is_followup_query(message):
        if not detect_spot_name(message):
            spot = session_memory["lastSpot"]
            augmented = f"{spot} {message}"
            print(f"[context] Augmented '{message}' -> '{augmented}'")
    message = augmented

    norm = normalize(message)
    if any(k in norm for k in ["孩子", "小孩", "亲子", "带娃", "儿童", "小朋友", "宝宝"]):
        interest = "family"
    route = route_for_interest(interest)
    expression_bias = (avatar_config or {}).get("expressionBias", "warm")
    emotion = infer_emotion_simple(message, expression_bias)

    # --- Phase 1: Cheap checks (no vector search) ---
    smalltalk = match_smalltalk(message)
    operational_faq = match_operational_faq(message)
    faq_match = search_faq(message)
    strict_fact = match_strict_fact(message)
    ticket_quote = match_ticket_pricing(message)

    supporting_facts: list[str] = []
    sources: list[str] = []
    topics: list[str] = []

    # Fast returns for exact matches
    if ticket_quote:
        supporting_facts.append(ticket_quote["answer"])
        sources.append(ticket_quote["source"])
        topics.append(ticket_quote["topic"])
        return _make_context(ticket_quote["answer"], emotion, route, [], None, sources, topics, "local-ticket-quote", supporting_facts)
    if operational_faq:
        supporting_facts.append(f"FAQ参考：{operational_faq['answer']}")
        sources.append(operational_faq["question"])
        topics.append(operational_faq["category"])
        return _make_context(operational_faq["answer"], emotion, route, [], operational_faq, sources, topics, "local-operational-faq", supporting_facts)
    if strict_fact and not detect_spot_name(message):
        supporting_facts.append(strict_fact["answer"])
        sources.append(strict_fact["source"])
        topics.append(strict_fact["topic"])
        return _make_context(strict_fact["answer"], emotion, route, [], None, sources, topics, "local-strict", supporting_facts)
    if faq_match:
        supporting_facts.append(f"FAQ参考：{faq_match['answer']}")
        sources.append(faq_match["question"])
        topics.append(faq_match["category"])
        return _make_context(faq_match["answer"], emotion, route, [], faq_match, sources, topics, "local-faq", supporting_facts)
    if smalltalk:
        return _make_context(smalltalk, emotion, route, [], None, ["本地知识库"], ["寒暄互动"], "local-greeting", [smalltalk])

    if any(w in message for w in ["路线", "推荐", "怎么玩", "怎么逛", "安排", "想去", "要去", "怎么到", "如何到", "带我去", "指路"]):
        stops = " -> ".join(s["name"] for s in route.get("stops", [])[:4])
        interest_style = {
            "history": "作为一名专业的文化讲解员，我为您深入介绍灵山胜境的历史文化底蕴。",
            "nature": "让我带您领略灵山胜境最美的自然风光和拍照打卡点。",
            "family": "带小朋友一起游灵山的话，我会特别关注互动性和便利性。",
            "relax": "咱们不赶时间，我帮您规划最舒适的游览节奏。",
        }
        prefix = interest_style.get(interest, "")
        draft = f'{prefix}{route.get("name", "推荐路线")}，预计 {route.get("duration", "约3小时")}，大致可以走 {stops or "几个核心点位"}。'
        return _make_context(draft, emotion, route, [], None, [route.get("name", "")], ["路线推荐"], "local-route", [draft])

    # --- Phase 2: Fast FTS5 knowledge search (zero network cost) ---
    import database as db_mod
    fts_results = db_mod.search_knowledge_fts(message, limit=5)
    # --- Phase 3: Full vector search (only if FTS5 didn't find great results) ---
    fts_quality = max((item.get("score", 0) for item in fts_results), default=0) if fts_results else 0
    if fts_quality < 0.5:
        evidence = search_knowledge(message)
    else:
        evidence = fts_results

    if route.get("name"):
        sources.append(route["name"])

    food_match = match_food_recommendation(message, evidence)
    spot_fact = extract_spot_fact(message, evidence)
    spot_intro = extract_spot_intro(message, evidence)

    for item in evidence:
        if item.get("title"):
            sources.append(item["title"])
        if item.get("category"):
            topics.append(item["category"])

    if food_match:
        supporting_facts.append(food_match["answer"])
        sources.append(food_match["source"])
        topics.append(food_match["topic"])
        return _make_context(food_match["answer"], emotion, route, evidence, None, list(dict.fromkeys(sources)), list(dict.fromkeys(topics)), "local-food", supporting_facts)
    if spot_fact:
        supporting_facts.append(spot_fact["answer"])
        sources.append(spot_fact["source"])
        topics.append(spot_fact["topic"])
        return _make_context(spot_fact["answer"], emotion, route, evidence, None, list(dict.fromkeys(sources)), list(dict.fromkeys(topics)), "local-spot-fact", supporting_facts)
    if spot_intro:
        supporting_facts.append(spot_intro["answer"])
        sources.append(spot_intro["source"])
        topics.append(spot_intro["topic"])
        return _make_context(spot_intro["answer"], emotion, route, evidence, None, list(dict.fromkeys(sources)), list(dict.fromkeys(topics)), "local-spot-intro", supporting_facts)

    confidence = compute_confidence(evidence, message)
    safety_reply = safe_fallback_reply(message, confidence)
    if safety_reply:
        return _make_context(safety_reply, emotion, route, evidence, None, list(dict.fromkeys(sources)) or ["本地景区知识库"], list(dict.fromkeys(topics)) or ["自然对话"], "local-safety-fallback", [safety_reply], confidence)

    if evidence:
        base = summarize_evidence_answer(message, evidence) or "这个问题我先帮您查到一条最相关的景区资料。"
        interest_style = {
            "history": "作为一名专业的文化讲解员，我为您深入介绍灵山胜境的历史文化底蕴。",
            "nature": "让我带您领略灵山胜境最美的自然风光和拍照打卡点。",
            "family": "带小朋友一起游灵山的话，我会特别关注互动性和便利性。",
            "relax": "咱们不赶时间，我帮您规划最舒适的游览节奏。",
        }
        prefix = interest_style.get(interest, "")
        draft = f"{prefix}{base}" if prefix else base
        return _make_context(draft, emotion, route, evidence, None, list(dict.fromkeys(sources)) or ["本地景区知识库"], list(dict.fromkeys(topics)) or ["自然对话"], "local-knowledge", [draft], confidence)

    return _make_context("我这边暂时没有查到完全对应的景区资料，不过我可以根据您的兴趣帮您推荐景点或路线。", emotion, route, evidence, None, ["本地景区知识库"], ["自然对话"], "local-fallback", [])


def _make_context(reply: str, emotion: str, route: dict, knowledge: list[dict], faq_match: dict | None,
                  sources: list[str], topics: list[str], mode: str, supporting_facts: list[str],
                  confidence: float = 0.92) -> dict:
    return {
        "reply": compress_reply(reply),
        "emotion": emotion,
        "emotionPayload": emotion_payload(emotion),
        "route": route,
        "knowledge": knowledge or [],
        "faqMatch": faq_match,
        "sources": sources or ["本地景区知识库"],
        "topics": topics or ["自然对话"],
        "answerMode": mode,
        "supportingFacts": supporting_facts or [],
        "confidence": confidence,
    }


def should_use_llm(message: str, dialog_context: dict, skip_llm: bool) -> bool:
    """Use local retrieval as evidence; let the LLM conduct substantive dialogue."""
    if skip_llm or not (message or "").strip():
        return False
    if not ai_service.api_enabled():
        return False
    mode = dialog_context.get("answerMode", "")
    # Keep only instant greetings local.  Facts, routes, spot introductions and
    # follow-up questions still go to the LLM with local data as grounding.
    return mode != "local-greeting"


def _is_garbled(text: str) -> bool:
    if not text or len(text) < 5:
        return True
    if text.count("```") > 4:
        return True
    for p in ["|||||", "||||", "考察", "场|场"]:
        if text.count(p) > 2:
            return True
    if sum(1 for c in text if c in "||```") / max(len(text), 1) > 0.3:
        return True
    return False


def extract_tags_from_reply(reply: str) -> tuple[str, str, list[str]]:
    em = re.search(r'\[emotion:\s*(\w+)\]', reply)
    emotion = "warm"
    if em:
        e = em.group(1).lower()
        if e in {"warm", "delighted", "focused", "caring", "surprised", "thinking", "neutral"}:
            emotion = e
    actions = re.findall(r'\[action:\s*(\w+)\]', reply)
    actions = [a for a in actions if a in {"wave", "nod", "shake", "bow", "tilt", "gesture", "spread", "think", "point", "openHand", "crossArms", "comfort"}]
    clean = re.sub(r'\[(emotion|action):\s*\w+\]', '', reply).strip()

    action_kw = r'微笑|点头|挥手|鞠躬|摇头|思考|手势|讲解|热情|亲切|温和|开心|弯身|拍手|鼓励|安慰|等待|歪头|比划|指着|介绍|语重心长|摆手|抬手'
    clean = re.sub(r'[\(（]\s*(?:' + action_kw + r')[^)）]{0,8}\s*[\)）]', '', clean)
    clean = re.sub(r'[\(（]\s*(?:' + action_kw + r')\s*[\)）]', '', clean)
    clean = re.sub(r'[，。；、]{2,}', '。', clean)
    clean = re.sub(r'\s+', ' ', clean)

    return clean.strip(), emotion, actions


# ============= SESSION MEMORY =============

def get_session_memory(session_id: str) -> dict:
    # 关键修复: 读写加锁, 与后台 TTL 回收线程互斥, 防迭代期间修改
    with SESSION_MEMORY_LOCK:
        mem = SESSION_MEMORY.get(session_id)
        if mem is None:
            mem = {
                "sessionId": session_id,
                "interest": "history",
                "lastSpot": None,
                "lastZone": None,
                "currentRouteIndex": -1,
                "routeStops": [],
                "currentRouteId": None,
                "visitHistory": [],
                "createdAt": now_str(),
                "lastActive": now_str(),
            }
            SESSION_MEMORY[session_id] = mem
        return mem


def update_session_memory(session_id: str, updates: dict):
    mem = get_session_memory(session_id)
    with SESSION_MEMORY_LOCK:
        mem.update(updates)
        mem["lastActive"] = now_str()


def _sweep_session_memory_once(now: datetime | None = None) -> int:
    """回收超过 TTL 未活跃的会话记忆, 返回删除条数.
    时间戳为固定宽度 %Y-%m-%d %H:%M:%S, 字典序即时间序, 可直接字符串比较."""
    cutoff = (now or datetime.now()) - timedelta(seconds=SESSION_MEMORY_TTL_SECONDS)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    stale: list[str] = []
    with SESSION_MEMORY_LOCK:
        for sid, mem in SESSION_MEMORY.items():
            last = mem.get("lastActive") or mem.get("createdAt") or ""
            if last < cutoff_str:
                stale.append(sid)
        for sid in stale:
            SESSION_MEMORY.pop(sid, None)
    if stale:
        print(f"[session-memory] 回收 {len(stale)} 条过期会话记忆", flush=True)
    return len(stale)


def _session_memory_sweep_loop():
    while True:
        time.sleep(_SESSION_MEMORY_SWEEP_INTERVAL)
        try:
            _sweep_session_memory_once()
        except Exception as e:
            print(f"[session-memory] sweep failed: {type(e).__name__}: {e}", flush=True)


# ============= GENERATE ANSWER =============

def generate_answer(message: str, interest: str, session_id: str, gps_coords: dict | None = None, skip_tts: bool = False, skip_llm: bool = False, model_id: str | None = None) -> dict:
    # 关键修复: ttsEnabled 开关实际生效, admin 关闭则跳过 TTS
    if not _is_tts_enabled():
        skip_tts = True
    avatar_config = _resolve_avatar_config(model_id)
    session_mem = get_session_memory(session_id)
    dialog_context = build_dialog_context(message, interest, avatar_config, session_mem)
    local_answer = {
        "reply": dialog_context["reply"],
        "emotion": dialog_context["emotion"],
        "emotionPayload": dialog_context["emotionPayload"],
        "route": dialog_context["route"],
        "sources": dialog_context["sources"],
        "topics": dialog_context["topics"],
        "answerMode": dialog_context["answerMode"],
        "faqMatch": dialog_context.get("faqMatch"),
        "confidence": dialog_context.get("confidence", 0.92),
    }
    original_mode = local_answer.get("answerMode", "local-fallback")
    should_keep_local = not should_use_llm(message, dialog_context, skip_llm)

    api_result = {}
    try:
        if not should_keep_local:
            history = db.get_history_for_llm(session_id, limit=6)
            api_result = chat_with_api(
                message=message,
                # Retrieval provides facts; local template replies must not anchor
                # the model into repeating a canned answer.
                draft_answer="",
                knowledge_context=dialog_context["knowledge"],
                route=local_answer.get("route"),
                history=history,
                supporting_facts=[] if original_mode in {"local-safety-fallback", "local-fallback"} else dialog_context["supportingFacts"],
                avatar_config=dict(avatar_config, interest=interest),
            )
            api_reply = sanitize_final_visible_text(compress_reply(api_result.get("reply", ""))) or ""
            if api_reply and len(api_reply) > 10 and not _is_garbled(api_reply):
                # Grounding check: verify numeric facts in LLM answer
                try:
                    gc_result = verify_grounding(
                        answer=api_reply,
                        knowledge_context=dialog_context.get("knowledge") or []
                    )
                    if not gc_result.get("pass", True):
                        print(f"[grounding] LLM answer failed grounding check ({gc_result.get('reason', '')}), falling back to draft")
                        api_reply = local_answer["reply"]
                    elif gc_result.get("corrected_answer"):
                        api_reply = compress_reply(gc_result["corrected_answer"])
                except Exception as e:
                    print(f"[grounding] check failed: {e}, proceeding with LLM answer")
                local_answer["reply"] = api_reply
                local_answer["answerMode"] = "llm-api" if api_result.get("used_api") else original_mode
            else:
                local_answer["answerMode"] = original_mode
        else:
            local_answer["answerMode"] = original_mode
    except Exception:
        local_answer["answerMode"] = original_mode
        api_result = {}

    llm_emotion = api_result.get("llm_emotion") if not should_keep_local else None
    llm_actions = api_result.get("llm_actions") if not should_keep_local else None

    emotion_response = build_emotion_response(
        reply_text=local_answer["reply"],
        llm_emotion=llm_emotion,
        llm_actions=llm_actions,
        tts_duration_ms=estimate_tts_duration_ms(local_answer["reply"]),
        user_message=message,
    )

    old_emotion = "warm"
    if any(w in message for w in ["谢谢", "喜欢", "满意", "太好了"]):
        old_emotion = "delighted"
    elif any(w in message for w in ["急", "快", "赶时间", "马上"]):
        old_emotion = "focused"
    elif any(w in message for w in ["累", "迷路", "找不到", "担心"]):
        old_emotion = "caring"

    local_answer["emotion"] = old_emotion
    local_answer["emotionPayload"] = emotion_payload(old_emotion)
    local_answer["actions"] = emotion_response.get("actions", [])
    local_answer["emotionState"] = emotion_response.get("emotion", {})
    local_answer["expression"] = emotion_response.get("expression", {})

    local_answer["reply"] = compress_reply(local_answer["reply"])
    if not skip_tts:
        try:
            segments = split_tts_segments(local_answer["reply"])
            voice = avatar_config.get("voice", "")
            if len(segments) > 1:
                with ThreadPoolExecutor(max_workers=min(len(segments), 4)) as pool:
                    futures = {pool.submit(synthesize_tts, seg, voice): i for i, seg in enumerate(segments)}
                    results = [None] * len(segments)
                    for future in as_completed(futures):
                        idx = futures[future]
                        try:
                            results[idx] = future.result()
                        except Exception:
                            results[idx] = None
                audio_urls = [u for u in results if u]
            else:
                audio_urls = [synthesize_tts(seg, voice_name=voice) for seg in segments if seg.strip()]
                audio_urls = [u for u in audio_urls if u]
            local_answer["audioUrl"] = audio_urls[0] if audio_urls else None
        except Exception:
            local_answer["audioUrl"] = None

    position = infer_position(get_session_memory(session_id), message)
    local_answer["position"] = position

    route_stops_names = []
    current_route = local_answer.get("route", {})
    for s in current_route.get("stops", []):
        if isinstance(s, dict):
            route_stops_names.append(s.get("name", ""))
        else:
            route_stops_names.append(str(s))

    update_session_memory(session_id, {
        "interest": interest,
        "lastSpot": position.get("estimatedSpot"),
        "lastZone": position.get("estimatedZone"),
        "currentRouteId": current_route.get("id"),
        "routeStops": route_stops_names,
        "lastMessage": message,
    })

    return local_answer


# ============= API ROUTES =============

@app.route("/api/v1/scenic/brief")
def scenic_brief():
    return jsonify({"code": 0, "data": {
        "name": "灵山胜境 AI 数字人导览系统",
        "positioning": "面向智慧景区的多模态数字人导游与运营管理平台",
        "models": ["Qwen2.5-VL", "SiliconFlow Chat", "Chroma向量RAG", "SiliconFlow ASR", "SiliconFlow TTS"],
        "capabilities": ["语音问答", "文本交互", "个性化路线推荐", "数字人播报", "向量知识库问答", "弱GPS位置推定", "游客洞察"],
        "sourceBundle": "示范景区公开资料包",
    }})


@app.route("/api/v1/scenic/routes")
def scenic_routes():
    return jsonify({"code": 0, "data": db.get_routes()})


@app.route("/api/v1/navigation/scenic-spots")
def navigation_scenic_spots():
    return jsonify({"code": 0, "data": [{"name": k, **v} for k, v in amap_service.SPOT_COORDS.items()]})


@app.route("/api/v1/navigation/query", methods=["POST"])
def navigation_query():
    """Detect destination spot from natural language query, return name and coords.
    Accepts sessionId to use conversation context when no explicit spot is mentioned."""
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    session_id = payload.get("sessionId") or ""

    if not message:
        return jsonify({"code": 400, "message": "请输入查询内容"}), 400

    dst_name = detect_spot_name(message)
    if not dst_name:
        norm = normalize(message)
        for spot_name in amap_service.SPOT_COORDS:
            if spot_name in norm:
                dst_name = spot_name
                break

    if not dst_name and session_id:
        pos = infer_position(get_session_memory(session_id), message)
        if pos.get("estimatedSpot"):
            dst_name = pos["estimatedSpot"]

    if not dst_name:
        return jsonify({"code": 400, "message": "未能识别出目的地景点", "hint": "请明确说出景点名称，如：灵山大佛、梵宫、九龙灌浴"}), 200

    coord = amap_service.SPOT_COORDS.get(dst_name, {})
    if not coord:
        coord = SPOT_COORDINATES.get(dst_name, {})

    return jsonify({
        "code": 0,
        "data": {
            "destination": {"name": dst_name, "lat": coord.get("lat"), "lng": coord.get("lng")},
        }
    })


@app.route("/api/v1/chat/text", methods=["POST"])
def chat_text():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    session_id = payload.get("sessionId") or str(uuid.uuid4())
    user_id = payload.get("userId") or "guest"
    interest = infer_interest(message, payload.get("interest"))
    gps = payload.get("gps")
    user_emotion = payload.get("emotion") or None
    model_id = payload.get("modelId")

    if not message:
        return jsonify({"code": 400, "message": "请输入问题内容"}), 400

    try:
        _resolve_avatar_config(model_id)
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400

    chat_start = time.time()
    try:
        answer = generate_answer(message, interest, session_id, gps, model_id=model_id)
    except Exception as e:
        app.logger.exception("chat_text generate_answer failed: %s", e)
        # 关键修复: 兜底返回, 不让用户看到 Python traceback
        return jsonify({"code": 500, "message": "服务暂时不可用,请稍后重试", "fallbackReply": "抱歉,信号不太好,请稍后再试一次。"}), 200
    answer["reply"] = safe_visitor_reply(answer.get("reply", ""))
    latency_ms = round((time.time() - chat_start) * 1000)
    record = db.add_conversation(session_id=session_id, user_id=user_id, message=message, reply=answer["reply"], emotion=user_emotion or answer["emotion"], interest=interest, topics=answer["topics"], latency_ms=latency_ms)

    faq_match = answer.get("faqMatch")
    if faq_match and faq_match.get("id"):
        db.increment_faq_usage_by_id(faq_match["id"])

    return jsonify({"code": 0, "data": {
        "conversationId": record["id"],
        "reply": answer["reply"],
        "emotion": answer["emotionPayload"],
        "actions": answer.get("actions", []),
        "emotionState": answer.get("emotionState", {}),
        "expression": answer.get("expression", {}),
        "interest": interest,
        "route": answer["route"],
        "sources": answer["sources"],
        "audioUrl": answer.get("audioUrl"),
        "answerMode": answer.get("answerMode", "local-fallback"),
        "position": answer.get("position"),
        "metrics": {"latencyMs": latency_ms, "confidence": answer.get("confidence", 0.92)},
    }})


@app.route("/api/v1/chat/text-stream", methods=["POST"])
def chat_text_stream():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    session_id = payload.get("sessionId") or str(uuid.uuid4())
    user_id = payload.get("userId") or "guest"
    interest = infer_interest(message, payload.get("interest"))
    gps = payload.get("gps")
    user_emotion = payload.get("emotion") or None
    model_id = payload.get("modelId")

    if not message:
        return jsonify({"code": 400, "message": "请输入问题内容"}), 400
    try:
        _resolve_avatar_config(model_id)
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400

    # 关键修复: 同一 session 只允许一个进行中的流式生成.
    # 新请求到达时, 取消该 session 上仍在进行的旧生成(置 cancelled 并停掉其 TTS 任务),
    # 释放请求线程与共享 TTS 事件循环, 避免旧回答把新一轮拖过 30s 前端超时而被吞掉.
    my_gen_token = {"cancelled": False}
    my_tts_tag = f"tts-{session_id}-{uuid.uuid4().hex}"
    with _ACTIVE_GENS_LOCK:
        old = _ACTIVE_GENS.get(session_id)
        _ACTIVE_GENS[session_id] = {"token": my_gen_token, "tts_tag": my_tts_tag}
    if old:
        old_token = old.get("token")
        old_tag = old.get("tts_tag")
        if old_token:
            old_token["cancelled"] = True
        if old_tag:
            ai_service.cancel_tts_for_tag(old_tag)
        print(f"[stream] session={session_id} 旧生成已取消 (被新请求取代)", flush=True)

    gen_token = my_gen_token
    tts_tag = my_tts_tag

    def generate():
        # 关键修复: 门面生成器. 客户端断连(GeneratorExit)/异常/正常完成三路都执行清理,
        # 防止 _ACTIVE_GENS 残留与 TTS daemon 线程跑完不回收.
        # 用 yield from 委托内层, 避免重排 420 行缩进引入逻辑误伤;
        # finally 内仅调用非生成器清理函数 (yield 在 GeneratorExit 处理期会抛 RuntimeError).
        try:
            yield from _generate_inner()
        finally:
            _clear_active_gen(session_id, gen_token)
            ai_service.cancel_tts_for_tag(tts_tag)

    def _generate_inner():
        if gen_token["cancelled"]:
            _clear_active_gen(session_id, gen_token)
            return
        gen_start = time.time()
        # 关键修复: SSE 协议统一 \r\n 分隔 (符合 spec)
        yield f"event: status\r\ndata: {json.dumps({'phase': 'searching'}, ensure_ascii=False)}\r\n\r\n"
        # 关键修复: SSE keepalive 注释行, 防 60s 反代超时 (nginx/cloudflare 默认 60s)
        last_keepalive = time.monotonic()

        avatar_config = _resolve_avatar_config(model_id)
        voice = avatar_config.get("voice", "")

        # --- Phase 1: Cheap checks (no vector search) ---
        try:
            dialog_context = build_dialog_context(message, interest, avatar_config, get_session_memory(session_id))
        except Exception as e:
            # 关键修复: Phase1 DB/本地错误也走 SSE error 事件, 不让生成器裸抛中断流;
            # except Exception 不捕获 GeneratorExit, 断连清理仍由外层 finally 兜底
            print(f"[stream] Phase1 dialog context failed: {type(e).__name__}: {e}", flush=True)
            yield f"event: error\r\ndata: {json.dumps({'message': '本地答案生成失败,请稍后重试', 'code': 'LOCAL_FAILED'}, ensure_ascii=False)}\r\n\r\n"
            return
        local_answer = {
            "reply": dialog_context["reply"],
            "emotion": dialog_context["emotion"],
            "emotionPayload": dialog_context["emotionPayload"],
            "route": dialog_context["route"],
            "sources": dialog_context["sources"],
            "topics": dialog_context["topics"],
            "answerMode": dialog_context["answerMode"],
            "faqMatch": dialog_context.get("faqMatch"),
            "supportingFacts": dialog_context["supportingFacts"],
            "knowledge": dialog_context["knowledge"],
        }
        original_mode = local_answer.get("answerMode", "local-fallback")
        is_local = not should_use_llm(message, dialog_context, skip_llm=False)

        # --- Phase 2: Send answer (local or LLM streaming) ---
        tts_segments: list[str] = []
        tts_results: list[tuple[int, str | None, str | None, int]] = []
        tts_lock = threading.Lock()
        tts_semaphore = threading.Semaphore(3)
        sent_audio: set[int] = set()
        # 关键修复: 用 Event 替代 time.sleep, 让 TTS 线程完成后立即唤醒 SSE 循环
        tts_progress_event = threading.Event()
        final_reply = ""
        # 顶层初始化, 让 LLM 解析和后续 emotion 重建都能拿到
        llm_emotion = None
        llm_actions_list: list[dict] | None = None

        def start_tts_thread(segment: str, idx: int):
            if not segment.strip():
                with tts_lock:
                    tts_results.append((idx, None, None, 0))
                tts_progress_event.set()
                return
            def _run():
                tts_semaphore.acquire()
                try:
                    # 关键修复: ttsEnabled 开关实际生效
                    if not _is_tts_enabled():
                        with tts_lock:
                            tts_results.append((idx, None, None, 0))
                        return
                    audio_bytes, audio_url = synthesize_tts_bytes(segment, voice_name=voice, tts_tag=tts_tag)
                    audio_b64 = base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else None
                    # 关键: 解析真实 mp3 时长, 用于驱动动作时间轴
                    real_ms = mp3_duration_ms(audio_bytes) if audio_bytes else 0
                    with tts_lock:
                        tts_results.append((idx, audio_url, audio_b64, real_ms or 0))
                    if audio_bytes:
                        print(f"[TTS-seg] segment {idx} OK: {len(audio_bytes)/1024:.1f}KB, dur={real_ms}ms", flush=True)
                    else:
                        print(f"[TTS-seg] segment {idx} FAILED: no audio bytes for '{segment[:40]}...'", flush=True)
                except Exception as e:
                    print(f"[TTS-seg] segment {idx} EXCEPTION: {e}", flush=True)
                    with tts_lock:
                        tts_results.append((idx, None, None, 0))
                finally:
                    tts_semaphore.release()
                    # 关键修复: 完成时唤醒 SSE 循环
                    tts_progress_event.set()
            t = threading.Thread(target=_run, daemon=True)
            t.start()

        def yield_ready_audio():
            nonlocal sent_audio
            ready: list[tuple[int, str | None, str | None, int]] = []
            with tts_lock:
                for entry in tts_results:
                    idx, url, b64, dur_ms = entry[0], entry[1], entry[2], entry[3]
                    if idx not in sent_audio:
                        ready.append(entry)
                        sent_audio.add(idx)
            ready.sort(key=lambda x: x[0])
            results = []
            for entry in ready:
                idx, url, b64, dur_ms = entry
                evt_data: dict[str, Any] = {
                    'index': idx,
                    'total': max(1, len(tts_segments)),
                    'audioUrl': url,
                    'audioBase64': b64,
                    'text': tts_segments[idx] if 0 <= idx < len(tts_segments) else '',
                    'durationMs': dur_ms,
                }
                # 关键修复: SSE 协议用 \r\n 分隔, 符合 spec
                results.append(f"event: audio_segment\r\ndata: {json.dumps(evt_data, ensure_ascii=False)}\r\n\r\n")
            return results

        if is_local:
            reply_text = local_answer["reply"]
            clean_reply, old_emotion, old_actions = extract_tags_from_reply(reply_text)
            final_reply = safe_visitor_reply(clean_reply)
            final_emotion = old_emotion
            final_actions = old_actions
            final_answer_mode = original_mode

            local_answer["reply"] = final_reply
            local_answer["emotion"] = final_emotion
            local_answer["emotionPayload"] = emotion_payload(final_emotion)
            local_answer["actions"] = final_actions

            emotion_response = build_emotion_response(
                reply_text=final_reply,
                llm_emotion=None,
                llm_actions=[{"type": a} for a in (final_actions or [])],
                tts_duration_ms=estimate_tts_duration_ms(final_reply),
                user_message=message,
            )
            local_answer["emotionState"] = emotion_response.get("emotion", {})
            local_answer["expression"] = emotion_response.get("expression", {})
            # 保留带时间轴的动作对象 (含 startMs/endMs/priority), 前端据此 setActionTimeline
            local_answer["actionTimeline"] = emotion_response.get("actions", [])
            # 同步刷新 final_actions 为时间轴里的动作名 (供兼容回退)
            if emotion_response.get("actions"):
                final_actions = [a["type"] for a in emotion_response["actions"]]

            # 本地直答: 复用LLM路径的逐句TTS+推送模式
            # 关键: 第一句TTS必须在推文本之前就启动, 消除首句延迟
            sentences = [s for s in re.split(r'(?<=[。！？；;\n])', final_reply) if s.strip()]
            tts_idx = 0
            accumulated = ""

            # 第一句: 先启动TTS, 再推文本
            if sentences:
                for seg in split_tts_segments(sentences[0]):
                    tts_segments.append(seg)
                    start_tts_thread(seg, tts_idx)
                    tts_idx += 1

                # 关键修复: 等第一段TTS就绪后再推文本, 确保音频不迟到
                # 超时3秒兜底, 防止TTS失败时文本永远不推
                first_seg_ready = len(sent_audio) > 0
                if not first_seg_ready:
                    _first_wait_start = time.time()
                    while not first_seg_ready and (time.time() - _first_wait_start) < 3.0:
                        if gen_token["cancelled"]:
                            _clear_active_gen(session_id, gen_token)
                            return
                        for evt in yield_ready_audio():
                            yield evt
                        if len(sent_audio) > 0:
                            first_seg_ready = True
                            break
                        tts_progress_event.wait(timeout=0.1)
                        tts_progress_event.clear()
                    # 超时后最后yield一次
                    for evt in yield_ready_audio():
                        yield evt
                    print(f"[local-tts] first segment ready={first_seg_ready} elapsed={time.time()-_first_wait_start:.2f}s", flush=True)

            for sent_i, sent in enumerate(sentences):
                if not sent.strip():
                    continue
                for ch in sent:
                    if gen_token["cancelled"]:
                        _clear_active_gen(session_id, gen_token)
                        return
                    accumulated += ch
                    yield f"event: text\r\ndata: {json.dumps({'text': ch, 'accumulated': accumulated}, ensure_ascii=False)}\r\n\r\n"
                    for evt in yield_ready_audio():
                        yield evt
                # 本句结束, 启动下一句的TTS
                if sent_i + 1 < len(sentences):
                    for seg in split_tts_segments(sentences[sent_i + 1]):
                        tts_segments.append(seg)
                        start_tts_thread(seg, tts_idx)
                        tts_idx += 1
                        for evt in yield_ready_audio():
                            yield evt

            # Text is complete even if some audio segments are still generating.
            yield f"event: text_done\r\ndata: {json.dumps({'completeReply': final_reply}, ensure_ascii=False)}\r\n\r\n"

        else:
            # LLM streaming path: keep provider output private until it passes
            # the complete visitor-safety check below.
            yield f"event: status\r\ndata: {json.dumps({'phase': 'generating'}, ensure_ascii=False)}\r\n\r\n"

            history = db.get_history_for_llm(session_id, limit=6)
            accumulated = ""
            tts_idx = 0
            stream_visible_text = ""
            stream_pending_sentence = ""
            stream_emitted_text = ""

            final_answer_mode = "llm-api"
            final_emotion = local_answer["emotion"]
            final_actions: list[str] = []

            try:
                for token in chat_with_api_stream(
                    message=message,
                    draft_answer="",
                    knowledge_context=local_answer["knowledge"],
                    route=local_answer.get("route"),
                    history=history,
                    supporting_facts=[] if original_mode in {"local-safety-fallback", "local-fallback"} else local_answer.get("supportingFacts"),
                    avatar_config=dict(avatar_config, interest=interest),
                ):
                    if gen_token["cancelled"]:
                        _clear_active_gen(session_id, gen_token)
                        return
                    accumulated += token
                    if time.monotonic() - last_keepalive > 5:
                        yield ": keepalive\r\n\r\n"
                        last_keepalive = time.monotonic()

                    visible_now, _pending_control = _split_provider_output(accumulated)
                    if not visible_now.startswith(stream_visible_text):
                        # A provider rewrite must not make already-emitted text unsafe.
                        stream_visible_text = ""
                        stream_pending_sentence = ""
                    stream_pending_sentence += visible_now[len(stream_visible_text):]
                    stream_visible_text = visible_now

                    sentences, stream_pending_sentence = pop_complete_stream_sentences(stream_pending_sentence)
                    for sentence in sentences:
                        safe_sentence = sanitize_final_visible_text(compress_reply(sentence))
                        if not safe_sentence:
                            continue
                        for seg in split_tts_segments(safe_sentence):
                            tts_segments.append(seg)
                            start_tts_thread(seg, tts_idx)
                            tts_idx += 1
                        stream_emitted_text += safe_sentence
                        yield f"event: text\r\ndata: {json.dumps({'text': safe_sentence, 'accumulated': stream_emitted_text}, ensure_ascii=False)}\r\n\r\n"
                        for evt in yield_ready_audio():
                            yield evt

                    # 关键修复: 每 5s 推一个 SSE 注释 keepalive, 防中间代理超时掐断
                    # Detect sentence boundary and start TTS
                    # 首句降低阈值到20字符, 尽快启动第一段TTS

                raw_final_reply = accumulated.strip()
                if not raw_final_reply:
                    raw_final_reply = safe_visitor_reply(local_answer["reply"])
                    final_answer_mode = original_mode
            except Exception as e:
                print(f"LLM stream error: {e}", flush=True)
                raw_final_reply = accumulated.strip() or safe_visitor_reply(local_answer["reply"])
                final_answer_mode = original_mode
                # 关键修复: 错误事件用 SSE 协议 \r\n + 通知前端
                yield f"event: error\r\ndata: {json.dumps({'message': 'LLM 生成失败,已回退到本地答案', 'code': 'LLM_FAILED'}, ensure_ascii=False)}\r\n\r\n"

            llm_emotion = None
            llm_actions_list = None
            clean_llm = raw_final_reply
            try:
                from ai_service import _parse_llm_json_block
                clean_llm, llm_emotion, llm_actions_list = _parse_llm_json_block(raw_final_reply)
            except ImportError:
                pass
            visible_final_reply, _pending_control = _split_provider_output(clean_llm)
            final_reply = sanitize_final_visible_text(compress_reply(visible_final_reply))
            if not final_reply:
                print("[LLM] unsafe or empty visitor output; using local answer", flush=True)
                final_reply = safe_visitor_reply(local_answer["reply"])
                final_answer_mode = original_mode
                llm_emotion = None
                llm_actions_list = None

            if llm_emotion and llm_emotion.get("primary"):
                final_emotion = llm_emotion["primary"]
            else:
                clean_old, detected_emotion, detected_actions = extract_tags_from_reply(final_reply)
                final_reply = compress_reply(clean_old)
                final_emotion = detected_emotion
                final_actions = detected_actions or final_actions

            yield f"event: text\r\ndata: {json.dumps({'text': final_reply, 'accumulated': final_reply}, ensure_ascii=False)}\r\n\r\n"
            # Let the client hide the cursor before the TTS completion wait below.
            yield f"event: text_done\r\ndata: {json.dumps({'completeReply': final_reply}, ensure_ascii=False)}\r\n\r\n"
            remaining_tts_text = strip_streamed_tts_prefix(final_reply, stream_emitted_text)
            for seg in split_tts_segments(remaining_tts_text):
                tts_segments.append(seg)
                start_tts_thread(seg, tts_idx)
                tts_idx += 1

            emotion_response = build_emotion_response(
                reply_text=final_reply,
                llm_emotion=llm_emotion,
                llm_actions=llm_actions_list,
                tts_duration_ms=estimate_tts_duration_ms(final_reply),
                user_message=message,
            )
            local_answer["emotionState"] = emotion_response.get("emotion", {})
            local_answer["expression"] = emotion_response.get("expression", {})
            # 保留带时间轴的动作对象 (含 startMs/endMs/priority), 前端据此 setActionTimeline
            local_answer["actionTimeline"] = emotion_response.get("actions", [])
            if emotion_response.get("actions"):
                final_actions = [a["type"] for a in emotion_response["actions"]]

        # --- Finalize ---
        position = infer_position(get_session_memory(session_id), message)
        route_stops_names = [s.get("name", str(s)) if isinstance(s, dict) else str(s) for s in local_answer.get("route", {}).get("stops", [])]
        update_session_memory(session_id, {
            "interest": interest,
            "lastSpot": position.get("estimatedSpot"),
            "lastZone": position.get("estimatedZone"),
            "currentRouteId": local_answer.get("route", {}).get("id"),
            "routeStops": route_stops_names,
            "lastMessage": message,
        })

        lat_ms = round((time.time() - gen_start) * 1000)
        record = db.add_conversation(session_id=session_id, user_id=user_id, message=message, reply=final_reply, emotion=user_emotion or final_emotion, interest=interest, topics=local_answer["topics"], latency_ms=lat_ms)

        faq_match = local_answer.get("faqMatch")
        if faq_match and faq_match.get("id"):
            db.increment_faq_usage_by_id(faq_match["id"])

        done_data: dict[str, Any] = {
            "conversationId": record["id"],
            "completeReply": final_reply,
            "emotion": emotion_payload(final_emotion),
            # 关键修复: 优先发带时间轴的 actionTimeline, 让前端能 setActionTimeline
            # 兼容旧前端: actionTypes 保留字符串列表
            "actions": local_answer.get("actionTimeline") or final_actions,
            "actionTypes": final_actions,
            "emotionState": local_answer.get("emotionState", {}),
            "expression": local_answer.get("expression", {}),
            "interest": interest,
            "route": local_answer["route"],
            "sources": local_answer["sources"],
            "audioUrl": None,
            "audioBase64": None,
            "answerMode": final_answer_mode,
            "position": position,
            "metrics": {"latencyMs": lat_ms, "confidence": local_answer.get("confidence", 0.92)},
        }

        # Poll for any TTS still generating BEFORE sending done event
        # 关键修复: 改用 Event.wait 替代 time.sleep(0.15) 死循环
        # 硬上限 20 秒, 防长文本拖到 gunicorn timeout
        tts_total_ms_real = 0
        if tts_segments:
            tts_remain_timeout = min(20, max(8, len(tts_segments) * 3))  # 关键: 硬封顶 20s
            tts_remain_start = time.time()
            while len(sent_audio) < len(tts_segments) and (time.time() - tts_remain_start) < tts_remain_timeout:
                if gen_token["cancelled"]:
                    break
                for evt in yield_ready_audio():
                    yield evt
                if len(sent_audio) >= len(tts_segments):
                    break
                # 关键修复: Event.wait 0.1s, 任何 TTS 完成即唤醒
                tts_progress_event.wait(timeout=0.1)
                tts_progress_event.clear()
            if gen_token["cancelled"]:
                _clear_active_gen(session_id, gen_token)
                return
            # 超时后最后再 yield 一次（可能仍有 TTS 完成）
            for evt in yield_ready_audio():
                yield evt
            ok_count = sum(1 for entry in tts_results if entry[1])
            fail_count = len(tts_results) - ok_count
            print(f"[TTS-summary] total={len(tts_segments)}, sent={len(sent_audio)}, ok={ok_count}, failed={fail_count}, elapsed={time.time()-tts_remain_start:.1f}s", flush=True)
            if fail_count > 0:
                for entry in sorted(tts_results, key=lambda x: x[0]):
                    if not entry[1]:
                        idx = entry[0]
                        seg_text = tts_segments[idx] if idx < len(tts_segments) else "?"
                        print(f"[TTS-summary] FAILED segment {idx}: '{seg_text[:50]}...'", flush=True)

            # 关键修复: 在 TTS 轮询完成后读取 audioUrl/audioBase64, 确保拿到真实值
            with tts_lock:
                for entry in sorted(tts_results, key=lambda x: x[0]):
                    if entry[1]:
                        done_data["audioUrl"] = entry[1]
                        done_data["audioBase64"] = entry[2]
                        break
            print(f"[done-audio] done_data audioUrl={bool(done_data.get('audioUrl'))} audioBase64={bool(done_data.get('audioBase64'))} sent_audio={len(sent_audio)}/{len(tts_segments)}")

            # 关键: 累加真实段时长, 用作 done 事件里的 ttsTotalMs, 让前端可校准表情/动作时长
            tts_total_ms_real = sum(entry[3] for entry in tts_results if entry[1] and entry[3] > 0)
            if tts_total_ms_real <= 0:
                # 解析失败则回退到基于字符数的估算
                tts_total_ms_real = estimate_tts_duration_ms(final_reply)

        # 用真实 TTS 时长重建表情/动作, 让动作时间轴与音频精确同步
        if tts_total_ms_real > 0:
            try:
                # 重新解析出 LLM 给的 emotion/actions, 重建时间轴
                use_llm = bool(llm_emotion or llm_actions_list)
                rebuilt = build_emotion_response(
                    reply_text=final_reply,
                    llm_emotion=llm_emotion if use_llm else None,
                    llm_actions=llm_actions_list if use_llm else [{"type": a} for a in (final_actions or [])],
                    tts_duration_ms=tts_total_ms_real,
                    user_message=message,
                )
                local_answer["actionTimeline"] = rebuilt.get("actions", [])
                if rebuilt.get("actions"):
                    final_actions = [a["type"] for a in rebuilt["actions"]]
                    done_data["actions"] = rebuilt["actions"]
                local_answer["expression"] = rebuilt.get("expression", local_answer.get("expression", {}))
                local_answer["emotionState"] = rebuilt.get("emotion", local_answer.get("emotionState", {}))
                done_data["expression"] = local_answer["expression"]
                done_data["emotionState"] = local_answer["emotionState"]
                print(f"[Emotion] rebuilt with real ttsTotalMs={tts_total_ms_real}, actions={len(done_data.get('actions', []))}")
            except Exception as e:
                print(f"[Emotion] rebuild failed: {e}")

        done_data["ttsTotalMs"] = tts_total_ms_real
        done_data["actionTypes"] = final_actions

        _clear_active_gen(session_id, gen_token)
        yield f"event: done\r\ndata: {json.dumps(done_data, ensure_ascii=False)}\r\n\r\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            # 关键修复: 禁用 nginx 等代理的缓冲
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        },
    )


@app.route("/api/v1/chat/transcribe-upload", methods=["POST"])
@app.route("/api/v1/chat/voice-upload", methods=["POST"])
def chat_voice_upload():
    # 关键修复: 显式预检 Content-Length, 避免读完整文件才发现超限
    content_length = request.content_length or 0
    if content_length > 10 * 1024 * 1024:
        return jsonify({"code": 413, "message": "语音文件超过 10MB 上限"}), 413
    audio = request.files.get("file")
    session_id = request.form.get("sessionId") or str(uuid.uuid4())
    user_id = request.form.get("userId") or "guest"
    model_id = request.form.get("modelId")
    interest = infer_interest(request.form.get("hint", ""), request.form.get("interest"))
    try:
        _resolve_avatar_config(model_id)
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400
    if not audio:
        return jsonify({"code": 400, "message": "未收到语音文件"}), 400
    # 关键修复: 限制允许的音频后缀
    suffix = (Path(audio.filename or "voice.webm").suffix or ".webm").lower()
    if suffix not in (".webm", ".wav", ".mp3", ".ogg", ".m4a"):
        return jsonify({"code": 400, "message": f"不支持的音频格式: {suffix}"}), 400
    if _voice_upload_is_rate_limited(request.remote_addr or ""):
        return jsonify({"code": 429, "message": "语音请求过于频繁，请稍后再试"}), 429

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            audio.save(tmp.name)
            tmp_path = Path(tmp.name)
        try:
            text = transcribe_audio(tmp_path)
        except Exception as exc:
            # 关键修复: 完整异常只进日志, 对外只给通用文案 (防路径/密钥等内部信息外泄)
            app.logger.warning("voice transcribe failed: %s", exc, exc_info=True)
            # 关键修复: 把 4xx 和 5xx 区分开, 不让所有错误都成 500
            status = 503 if isinstance(exc, (requests.ConnectionError, requests.Timeout)) else 500
            try:
                if hasattr(exc, "response") and exc.response is not None:
                    status = exc.response.status_code
            except Exception:
                pass
            return jsonify({"code": status, "message": "语音转写失败，请稍后重试"}), 200 if status < 500 else status
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    if not text:
        return jsonify({"code": 400, "message": "未识别到语音内容"}), 400

    if request.path.endswith("/transcribe-upload"):
        return jsonify({"code": 0, "data": {"text": text}})

    voice_start = time.time()
    try:
        answer = generate_answer(text, interest, session_id, model_id=model_id)
    except Exception as e:
        app.logger.exception("voice_upload generate_answer failed: %s", e)
        return jsonify({"code": 500, "message": "服务暂时不可用,请稍后重试", "fallbackReply": "抱歉,信号不太好,请稍后再试一次。", "text": text}), 200
    answer["reply"] = safe_visitor_reply(answer.get("reply", ""))
    latency_ms = round((time.time() - voice_start) * 1000)
    record = db.add_conversation(session_id=session_id, user_id=user_id, message=text, reply=answer["reply"], emotion=answer["emotion"], interest=interest, topics=answer["topics"], latency_ms=latency_ms)

    faq_match = answer.get("faqMatch")
    if faq_match and faq_match.get("id"):
        db.increment_faq_usage_by_id(faq_match["id"])

    return jsonify({"code": 0, "data": {
        "conversationId": record["id"],
        "text": text,
        "reply": answer["reply"],
        "emotion": answer["emotionPayload"],
        "actions": answer.get("actions", []),
        "interest": interest,
        "route": answer["route"],
        "sources": answer["sources"],
        "audioUrl": answer.get("audioUrl"),
        "answerMode": answer.get("answerMode", "local-fallback"),
        "position": answer.get("position"),
        "metrics": {"latencyMs": latency_ms, "confidence": answer.get("confidence", 0.92)},
    }})


@app.route("/api/v1/feedback", methods=["POST"])
def save_feedback():
    payload = request.get_json(silent=True) or {}
    conv_id = str(payload.get("conversationId") or "").strip()
    session_id = str(payload.get("sessionId") or "").strip()
    try:
        sat = int(payload.get("satisfaction"))
    except (TypeError, ValueError):
        return jsonify({"code": 400, "message": "评分必须是 1 到 5 的整数"}), 400
    if not conv_id or not session_id or not 1 <= sat <= 5:
        return jsonify({"code": 400, "message": "评分或会话信息无效"}), 400
    if not db.update_conversation_satisfaction(conv_id, sat, session_id):
        return jsonify({"code": 404, "message": "未找到可评分的对话"}), 404
    return jsonify({"code": 0, "message": "反馈已记录"})


# ============= PUBLIC AVATAR CONFIG =============

@app.route("/api/v1/avatar/config")
def public_avatar_config():
    try:
        result = _resolve_avatar_config(request.args.get("modelId"))
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400
    print(f"[VRM-DEBUG] public avatar config: vrmModel={result.get('vrmModel', 'N/A')}")
    return jsonify({"code": 0, "data": result})


@app.route("/api/v1/avatar/models")
def public_avatar_models():
    models = db.get_vrm_models(MODEL_DIR)
    return jsonify({
        "code": 0,
        "data": [
            {
                "modelId": model["name"],
                "name": model["name"],
                "path": model["path"],
                "voice": model["voice"],
                "outfit": model["outfit"],
                "style": model["style"],
                "expressionBias": model["expressionBias"],
            }
            for model in models if model.get("enabled")
        ],
    })


# ============= MAP SDK PROXY =============

@app.route("/api/v1/map/amap-sdk.js")
def amap_sdk_proxy():
    """Serve the AMap JavaScript SDK through the local backend."""
    if not _AMAP_KEY:
        return jsonify({"code": 503, "message": "AMap key is not configured"}), 503
    try:
        response = requests.get(
            "https://webapi.amap.com/maps",
            params={"v": "2.0", "key": _AMAP_KEY},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        # Some Windows TLS stacks reset this endpoint for requests while curl succeeds.
        try:
            url = f"https://webapi.amap.com/maps?v=2.0&key={_AMAP_KEY}"
            fallback = subprocess.run(["curl.exe", "-fsSL", "--max-time", "15", url], capture_output=True, check=True, timeout=20)
            return Response(fallback.stdout, mimetype="application/javascript; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})
        except Exception:
            app.logger.warning("AMap SDK proxy failed: %s", exc)
            return jsonify({"code": 502, "message": "AMap SDK temporarily unavailable"}), 502
    return Response(response.content, mimetype="application/javascript; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})


# ============= STATIC FILES =============

def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def tourist_home():
    return _no_cache(send_from_directory(TOURIST_DIR, "index.html"))


@app.route("/admin")
@app.route("/admin/")
def admin_home():
    return _no_cache(send_from_directory(ADMIN_DIR, "index.html"))


@app.route("/admin/<path:filename>")
def admin_assets(filename: str):
    return _no_cache(send_from_directory(ADMIN_DIR, filename))


@app.route("/static/audio/<path:filename>")
def serve_audio(filename: str):
    # 关键修复: 路径穿越防护 + 文件名校验 (只允许 tts_cache_*.mp3 命名规范)
    from werkzeug.utils import secure_filename
    safe = secure_filename(filename)
    if not safe or not re.fullmatch(r"(?:tts_cache_|sf_)[0-9a-f]{16}\.mp3", safe):
        return jsonify({"code": 404, "message": "Audio not found"}), 404
    audio_dir = (BACKEND_DIR / "static" / "audio").resolve()
    target = (audio_dir / safe).resolve()
    # 关键修复: 严格限制在 audio_dir 内 (防 ../ 穿越)
    try:
        target.relative_to(audio_dir)
    except ValueError:
        return jsonify({"code": 404, "message": "Audio not found"}), 404
    if not target.exists():
        return jsonify({"code": 404, "message": "Audio not found"}), 404
    resp = send_from_directory(str(audio_dir), safe)
    # 关键修复: 长期缓存 + immutable, 同 URL 内容不变
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/<name>.vrm")
def serve_vrm(name: str):
    # 关键修复: 路径穿越防护. 与 serve_audio 对齐的 resolve() + relative_to() 校验,
    # 但不能用 secure_filename(会清空中文文件名). 路由段本身不含分隔符,
    # 这里再拒绝 "." 开头与任何分隔符, 作为纵深防御.
    if not name or name.startswith((".", "/", "\\")) or re.search(r"[\\/]", name):
        return jsonify({"code": 404, "message": "VRM not found"}), 404
    vrm_path = (MODEL_DIR / f"{name}.vrm").resolve()
    try:
        vrm_path.relative_to(MODEL_DIR.resolve())
    except ValueError:
        return jsonify({"code": 404, "message": "VRM not found"}), 404
    if not vrm_path.is_file():
        return jsonify({"code": 404, "message": "VRM not found"}), 404
    try:
        db.get_vrm_model(f"{name}.vrm", MODEL_DIR, enabled_only=True)
    except ValueError:
        return jsonify({"code": 404, "message": "VRM not found"}), 404
    return send_file(vrm_path, mimetype="model/vrm")


@app.route("/<path:filename>")
def tourist_assets(filename: str):
    if filename.startswith("api/"):
        return jsonify({"code": 404, "message": "Not found"}), 404
    return _no_cache(send_from_directory(TOURIST_DIR, filename))


# ============= TTS PRE-CACHE =============

def _is_tts_enabled() -> bool:
    """读取 admin 设置的 ttsEnabled 开关 (默认开启)."""
    try:
        s = db.get_settings()
        v = s.get("ttsEnabled", "true")
        return str(v).lower() not in ("false", "0", "no", "off", "")
    except Exception:
        return True


def _collect_precache_texts() -> list[str]:
    texts: set[str] = set()
    for item in db.get_faq():
        a = compress_reply(item.get("answer", ""))
        if a:
            texts.add(a)
    strict_answers = [
        "灵山胜境门票为景区大门票，包含入园和各景点游览。梵宫《灵山吉祥颂》演出票需另购，不包含在大门票内。九龙灌浴表演凭大门票免费观看。",
        "景区设有大型停车场，位于东门和北门。小车10元/次，大车20元/次；节假日建议尽早到达。",
        "灵山胜境门票价格：成人票210元/人，优惠票（老人、学生）105元/人，1.4米以下儿童免费。梵宫《灵山吉祥颂》演出票需另购。",
        "景区开放时间为每日7:30-17:30，17:00停止入园。梵宫演出时间为10:00、11:30、14:00、16:00，具体以景区当日公告为准。",
        "梵宫《灵山吉祥颂》演出时间为每天10:00、11:30、14:00、16:00共四场，每场约20分钟。九龙灌浴表演每天多场，具体时间以景区当日公告为准。",
        "景区内设有多个卫生间，主要分布在大佛广场、梵宫、五印坛城附近，按照现场指示牌即可找到。",
        "景区内比较有代表性的餐饮有梵宫素斋自助、素面套餐和灵山精舍素斋，整体以清淡素食和禅意餐饮体验为主。",
        "灵山胜境1.4米以下儿童免费入园，无需购票。超过1.4米的儿童需购买成人票210元/人。建议带好儿童身份证明，以景区现场身高测量为准。",
        "景区游客中心提供轮椅免费租赁服务（需缴纳押金），主要道路无障碍通行，轮椅可以到达大部分景点。",
        "灵山胜境位于江苏省无锡市滨湖区马山镇灵山路1号。自驾可走沪宁高速转马山出口；公交可坐K1、88、89路；地铁一号线到终点站转公交。",
        "游览灵山胜境建议穿舒适的运动鞋，做好防晒。景区内禁止吸烟和使用明火，进入寺庙请保持安静。建议游玩时间3-4小时，尽量避开节假日高峰期。",
        "60岁以上老人凭身份证可购买优惠票105元/人，约为成人票半价。学生凭学生证也可享受同价优惠票。购买时需在景区售票窗口出示有效证件。",
        "灵山胜境特色活动包括：九龙灌浴大型音乐喷泉表演、梵宫《吉祥颂》演出、抄经体验、禅茶品鉴等。其中九龙灌浴和《吉祥颂》是必看的核心演出项目。",
        "大佛广场是拍摄灵山大佛全景的最佳位置，位于大佛正前方，视野开阔，可容纳大量游客驻足观瞻。广场上还可欣赏到九龙灌浴表演，是景区必到的打卡点。",
        "灵山大佛位于江苏省无锡市滨湖区马山镇灵山胜境景区核心区，是灵山胜境的标志性景点之一，具体位置可查看景区导览图。",
        "灵山胜境比较适合老人游览。景区内有轮椅免费租赁、无障碍通道，核心景点之间距离适中，可以慢慢走。建议走文化路线，经大佛广场、灵山大佛、梵宫等，全程约3小时，比较舒缓。",
        "灵山梵宫是中国最大的仿唐式建筑群，集艺术殿堂、会议中心、表演舞台于一体。外观气势恢宏，内部汇集了木雕、石雕、铜雕、油画、琉璃等多种艺术形式，非常壮观。",
        "九龙灌浴是灵山胜境的经典大型音乐喷泉表演，位于景区核心位置。表演展示了佛祖释迦牟尼诞生时的盛况——九龙吐水灌浴太子。表演配合音乐、喷泉和动态雕塑，视觉效果非常震撼，是游客必看的演艺项目。",
        "祥符禅寺是一座千年古寺，始建于唐代，历经多次修缮，是江南地区重要的佛教寺院之一。寺内保存有众多珍贵文物和佛教艺术品，历史悠久，文化底蕴深厚。",
        "五印坛城主要展示的是藏传佛教文化，具体来说是一座以'五方五佛'和中国四大佛山文化为主题，集佛教发展史、文化展示、艺术鉴赏和互动体验于一体的殿堂，非常适合游客参观。",
        "景区明确规定禁止携带宠物入园。如果您携带了宠物，建议提前咨询景区游客中心是否有寄养服务，或者将宠物安置好再前来游览，以免影响您的游览计划。",
    ]
    for a in strict_answers:
        texts.add(compress_reply(a))
    texts.add(compress_reply("您好，我是景区 AI 导览助手小灵。我可以陪您聊天，也可以帮您介绍景点、推荐游览路线、回答门票和服务设施等问题。您可以直接问我，比如'有什么必看景点'或'帮我推荐一条亲子路线'。"))
    for t in [
        "我是小灵，灵山胜境景区的AI数字人导游！很高兴为您服务。",
        "我叫小灵，灵山胜境的小灵导游。",
        "我刚诞生不久，是灵山胜境的AI导游小灵。",
        "我可以给您介绍灵山胜境的各个景点、历史文化、特色活动，还可以为您规划游览路线！",
    ]:
        texts.add(compress_reply(t))
    for t in [
        "这个问题我暂时没有找到准确的景区资料，建议您咨询景区游客服务中心以获得最准确的信息。",
        "我暂时没有查到完全对应的景区信息，您可以换个问法或者咨询景区现场工作人员。",
        "我这边暂时没有查到完全对应的景区资料，不过我可以根据您的兴趣帮您推荐景点或路线。",
        "抱歉，我暂时无法回答这个问题。您想了解灵山胜境的哪些景点或活动呢？",
        "欢迎来到灵山胜境！我是小灵，很高兴为您服务。",
        "灵山胜境门票价格：成人票210元/人，优惠票（老人、学生）105元/人，1.4米以下儿童免费。",
        "梵宫《灵山吉祥颂》演出票需另购。",
    ]:
        texts.add(compress_reply(t))
    result = [t for t in texts if t.strip()]
    return result


def precache_all_tts(voice_name: str = ""):
    texts = _collect_precache_texts()
    if not texts:
        return
    # 关键修复: 限制预热文本数量, 防启动期资源风暴
    MAX_PRECACHE = int(os.environ.get("MAX_PRECACHE_TEXTS", "30"))
    texts = texts[:MAX_PRECACHE]
    def _run():
        import concurrent.futures
        total = len(texts)
        done = 0
        lock = threading.Lock()
        def _gen(text):
            nonlocal done
            try:
                # 关键修复: 用 synthesize_tts_bytes 走共享 event loop, 不再每次 new+close loop
                synthesize_tts_bytes(text, voice_name=voice_name)
            except Exception as e:
                print(f"[precache] failed: {e}", flush=True)
            with lock:
                done += 1
        start = time.time()
        # 关键修复: 2 并发 (原来 3), 不抢资源
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            ex.map(_gen, texts)
        print(f"  缓存 {total} 条语音完成 ({time.time()-start:.1f}s)", flush=True)
        # 关键修复: 预热完顺手清一次 LRU, 防启动后磁盘继续膨胀
        try:
            from ai_service import clean_tts_cache
            removed = clean_tts_cache(max_files=500)
            if removed:
                print(f"  预热后清理了 {removed} 个旧缓存", flush=True)
        except Exception as e:
            print(f"[precache] cache cleanup failed: {e}", flush=True)
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ============= STARTUP =============

import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(_sys.stderr, "reconfigure"):
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 把所有重活（知识导入 / TTS 预热）放后台线程，
# 避免 waitress/gunicorn worker 启动时阻塞 30+ 秒
_startup_done_event = threading.Event()
_startup_error: list[Exception] = []

def _startup_async():
    try:
        import_bundle_knowledge()
        warmup_tts()
        # precache 是后台 daemon，不 await；但我们让后台自己跑
        precache_all_tts()
    except Exception as e:
        _startup_error.append(e)
        print(f"[STARTUP] 初始化异常: {e}", flush=True)
    finally:
        _startup_done_event.set()

_startup_thread = threading.Thread(target=_startup_async, daemon=True, name="startup-init")

# 关键修复: SESSION_MEMORY 后台 TTL 回收线程 (daemon), 防会话记忆无界膨胀
_session_memory_sweep_thread = threading.Thread(
    target=_session_memory_sweep_loop, daemon=True, name="session-memory-sweep"
)

# Test imports must not start background TTS/cache workers or the session sweep.
# Those workers create shared side effects while the test suite is importing modules.
_IS_TEST_ENV = os.environ.get("APP_ENV", "").strip().lower() in {"test", "testing"}
if _IS_TEST_ENV:
    _startup_done_event.set()
else:
    _startup_thread.start()
    _session_memory_sweep_thread.start()

# ============= TTS HEALTH =============
@app.route("/api/v1/health/tts")
def health_tts():
    """探测 TTS 链路：edge-tts 是否可用、缓存目录可写、TTS 引擎是否初始化完成。"""
    import ai_service as _ai
    from ai_service import _normalize_tts_text
    info = {
        "ready": _startup_done_event.is_set(),
        "edge_tts_lib_loaded": True,
        "shared_loop_alive": False,
        "cache_dir_writable": False,
        "cache_file_count": 0,
        "tts_voice": getattr(_ai, "TTS_VOICE", "?"),
        # Public health responses must not expose exception text, paths, or
        # provider configuration. Detailed errors remain in the server log.
        "startup_error": "initialization_failed" if _startup_error else None,
    }
    try:
        loop = _ai._get_tts_loop()
        info["shared_loop_alive"] = loop.is_running()
    except Exception:
        info["shared_loop_alive"] = False
    try:
        cache_dir = _ai.AUDIO_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        probe = cache_dir / "__health_probe.tmp"
        probe.write_bytes(b"ok")
        probe.unlink()
        info["cache_dir_writable"] = True
        info["cache_file_count"] = sum(1 for _ in cache_dir.glob("tts_cache_*.mp3"))
    except Exception:
        info["cache_dir_writable"] = False
    return jsonify({"code": 0, "data": info})


_AMAP_KEY = os.environ.get("AMAP_API_KEY", "").strip() or ""
_AMAP_SECURITY_CODE = os.environ.get("AMAP_SECURITY_CODE", "").strip() or ""
_AMAP_WEB_KEY = os.environ.get("AMAP_WEB_API_KEY", "").strip() or _AMAP_KEY

@app.route("/api/v1/config")
def client_config():
    # The browser receives only the restricted public key; keep the security
    # code server-side for backend-to-provider requests.
    return jsonify({"code": 0, "data": {
        "amapKey": _AMAP_KEY,
    }})


# Weather station for Lingshan (无锡 adcode=320200)
_WEATHER_CITY = "320200"

@app.route("/api/v1/weather")
def get_weather():
    """Return real-time weather for Lingshan area via Amap Weather API."""
    if not _AMAP_WEB_KEY:
        return jsonify({"code": 1, "msg": "AMAP key not configured", "data": None}), 200
    try:
        url = "https://restapi.amap.com/v3/weather/weatherInfo"
        params = {
            "key": _AMAP_WEB_KEY,
            "city": _WEATHER_CITY,
            "extensions": "base",
            "output": "JSON",
        }
        resp = requests.get(url, params=params, timeout=6)
        resp.raise_for_status()
        raw = resp.json()
        if raw.get("status") != "1":
            return jsonify({"code": 1, "msg": raw.get("info", "weather api error"), "data": None}), 200
        live = raw.get("lives", [{}])[0]
        data = {
            "province": live.get("province", ""),
            "city": live.get("city", ""),
            "adcode": live.get("adcode", ""),
            "weather": live.get("weather", ""),
            "temperature": live.get("temperature", ""),
            "wind_direction": live.get("winddirection", ""),
            "wind_power": live.get("windpower", ""),
            "humidity": live.get("humidity", ""),
            "report_time": live.get("reporttime", ""),
        }
        return jsonify({"code": 0, "data": data})
    except Exception as e:
        app.logger.warning("weather api failed: %s", e)
        return jsonify({"code": 1, "msg": "天气服务暂时不可用", "data": None}), 200


@app.route("/api/v1/health")
def health():
    return jsonify({
        "status": "ok",
        "time": now_str(),
        "ready": _startup_done_event.is_set(),
    })


if __name__ == "__main__":
    print("游客端 http://localhost:8088 | 管理端 http://localhost:8088/admin", flush=True)
    print("使用 Flask dev server (开发模式, debug=False)", flush=True)
    # 关键修复: debug=True 会导致文件变动时自动 reload 杀掉长连接,
    # 并暴露 Werkzeug debugger. 改为 debug=False, 推荐生产用 waitress/gunicorn.
    app.run(host="0.0.0.0", port=8088, debug=False, threaded=True)
