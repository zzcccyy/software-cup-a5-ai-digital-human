# -*- coding: utf-8 -*-
"""管理端核心蓝图: 认证 / 仪表盘 / 报表 / 设置 / 会话列表."""

from __future__ import annotations

import os
import secrets
import threading
import time
from functools import wraps
from typing import Any

from flask import Blueprint, Response, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

import ai_service
import conversation_analysis
import database as db
import deep_report
from blueprints.common import csv_safe_value, parse_pagination

bp = Blueprint("admin_core", __name__)

ADMIN_SESSION_TTL_SECONDS = 60 * 60 * 12
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_ATTEMPTS_LOCK = threading.Lock()
LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60
LOGIN_ATTEMPT_LIMIT = 8
_LOGIN_ATTEMPTS_SWEEP_THRESHOLD = 5000


def _env_admin_credentials_configured() -> bool:
    return bool(os.environ.get("ADMIN_USERNAME", "").strip() and os.environ.get("ADMIN_PASSWORD", "").strip())


def _explicit_dev_environment() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() in {"development", "dev", "local", "test"}

_KNOWLEDGE_MODE_DESCRIPTION = "本地景区知识库 + FTS5 + Chroma向量RAG + 资料包自动导入"
_EMOTION_ENGINE_DESCRIPTION = "规则情绪分析 + Plutchik 情绪状态机 + LLM 情绪标签"
_READ_ONLY_SETTINGS = {
    "aiModel",
    "knowledgeMode",
    "responseTargetMs",
    "emotionEngine",
    "asrMode",
    "ttsEnabled",
}
_WRITABLE_SETTINGS = {"adminUser", "admin_username", "admin_password", "ttsVoice"}


def get_admin_credentials() -> tuple[str, str]:
    """Return (expected_username, credential). credential is either a plaintext
    env password (ADMIN_PASSWORD) or a stored password hash (scrypt:/pbkdf2:)."""
    env_username = os.environ.get("ADMIN_USERNAME", "").strip()
    env_password = os.environ.get("ADMIN_PASSWORD", "")
    if env_username and env_password.strip():
        return env_username, env_password
    app_env = os.environ.get("APP_ENV", "").strip().lower()
    if app_env == "production":
        raise RuntimeError("ADMIN_USERNAME and ADMIN_PASSWORD must be set when APP_ENV=production")
    s = db.get_settings()
    username = s.get("adminUser") or s.get("admin_username") or "admin"
    password = s.get("admin_password") or ""
    # Backfill: old DBs created before P2.1 may lack admin_password row.
    if not password:
        if not _explicit_dev_environment():
            raise RuntimeError("admin_password is missing; set APP_ENV=development or configure admin credentials")
        from werkzeug.security import generate_password_hash
        password = generate_password_hash("admin123")
        db.update_settings({"admin_password": password})
    if not _explicit_dev_environment() and (
        password == "admin123"
        or (_is_hashed_password(password) and check_password_hash(password, "admin123"))
    ):
        raise RuntimeError("default admin password is disabled outside explicit development environments")
    return username.strip() or "admin", password


def _is_hashed_password(value: str) -> bool:
    return value.startswith(("scrypt:", "pbkdf2:", "werkzeug.security:"))


def verify_admin_password(username: str, password: str) -> bool:
    """Verify a login attempt. Env credentials compare in plaintext; DB
    credentials are scrypt-hashed. Legacy plaintext in the DB is upgraded to a
    hash on first successful login (lazy migration, no data migration needed)."""
    exp_u, credential = get_admin_credentials()
    if username != exp_u or not credential:
        return False
    if _env_admin_credentials_configured():
        return password == credential
    if _is_hashed_password(credential):
        return check_password_hash(credential, password)
    if password == credential:
        db.update_settings({"admin_password": generate_password_hash(credential)})
        return True
    return False


def _login_key(ip: str, username: str) -> str:
    """Per (ip, username) bucket so a failing user never locks out other
    accounts sharing the same IP."""
    return f"{ip}|{username}"


def _prune_login_attempts() -> None:
    """Drop buckets whose last attempt is older than the window (caller holds the lock)."""
    now_ts = time.time()
    expired = [
        k for k, v in LOGIN_ATTEMPTS.items()
        if not v or now_ts - v[-1] >= LOGIN_ATTEMPT_WINDOW_SECONDS
    ]
    for k in expired:
        LOGIN_ATTEMPTS.pop(k, None)


def _login_is_rate_limited(client_key: str) -> bool:
    now_ts = time.time()
    with _LOGIN_ATTEMPTS_LOCK:
        attempts = [t for t in LOGIN_ATTEMPTS.get(client_key, []) if now_ts - t < LOGIN_ATTEMPT_WINDOW_SECONDS]
        LOGIN_ATTEMPTS[client_key] = attempts
        if len(LOGIN_ATTEMPTS) > _LOGIN_ATTEMPTS_SWEEP_THRESHOLD:
            _prune_login_attempts()
        return len(attempts) >= LOGIN_ATTEMPT_LIMIT


def _record_failed_login(client_key: str) -> None:
    with _LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.setdefault(client_key, []).append(time.time())


def issue_admin_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    db.create_admin_session(token, username, int(time.time()) + ADMIN_SESSION_TTL_SECONDS)
    return token


def _get_admin_token() -> str:
    token = request.headers.get("X-ADMIN-TOKEN", "").strip()
    if not token:
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return token


def get_admin_session() -> dict | None:
    token = _get_admin_token()
    session = db.get_admin_session(token)
    if not session:
        return None
    db.refresh_admin_session(token, int(time.time()) + ADMIN_SESSION_TTL_SECONDS)
    return session


def require_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not get_admin_session():
            return jsonify({"code": 401, "message": "请先登录管理员账号"}), 401
        return func(*args, **kwargs)
    return wrapper


def _log_op(action: str, resource: str, resource_id: str = "", detail: str = "", result: str = "success", admin_user: str = ""):
    if not admin_user:
        session = get_admin_session()
        admin_user = session.get("username", "未知") if session else "未知"
    ip = request.remote_addr or ""
    try:
        db.add_operation_log(admin_user, action, resource, resource_id, detail, ip, result)
    except Exception:
        pass


# ============= ADMIN AUTH ROUTES =============

@bp.route("/api/v1/admin/auth/login", methods=["POST"])
def admin_login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    client_key = _login_key(request.remote_addr or "unknown", username)
    if _login_is_rate_limited(client_key):
        return jsonify({"code": 429, "message": "登录尝试过于频繁，请稍后再试"}), 429
    if not verify_admin_password(username, password):
        _record_failed_login(client_key)
        _log_op("login", "auth", detail=f"登录失败：{username}", result="failure", admin_user=username or "未知")
        return jsonify({"code": 401, "message": "管理员账号或密码错误"}), 401
    LOGIN_ATTEMPTS.pop(client_key, None)
    token = issue_admin_token(username)
    _log_op("login", "auth", detail=f"管理员登录成功：{username}", admin_user=username)
    return jsonify({"code": 0, "data": {"token": token, "username": username, "expiresIn": ADMIN_SESSION_TTL_SECONDS}})


@bp.route("/api/v1/admin/auth/me", methods=["GET"])
def admin_auth_me():
    session = get_admin_session()
    if not session:
        return jsonify({"code": 401, "message": "未登录"}), 401
    return jsonify({"code": 0, "data": {"username": session.get("username", "admin")}})


@bp.route("/api/v1/admin/auth/logout", methods=["POST"])
def admin_logout():
    token = _get_admin_token()
    session = get_admin_session()
    db.delete_admin_session(token)
    username = session.get("username", "") if session else ""
    if session:
        _log_op("logout", "auth", detail=f"管理员登出：{username}", admin_user=username)
    return jsonify({"code": 0, "message": "已退出登录"})


# ============= ADMIN DASHBOARD =============

@bp.route("/api/v1/admin/dashboard/overview")
@require_admin
def admin_dashboard():
    return jsonify({"code": 0, "data": db.compute_dashboard()})


@bp.route("/api/v1/admin/report", methods=["GET"])
@require_admin
def get_report():
    return jsonify({"code": 0, "data": db.compute_report()})


@bp.route("/api/v1/admin/report/deep", methods=["GET"])
@require_admin
def get_deep_report():
    return jsonify({"code": 0, "data": deep_report.compute_deep_report()})


@bp.route("/api/v1/admin/export/report", methods=["GET"])
@require_admin
def export_report():
    import csv
    import io
    fmt = request.args.get("format", "json")
    if fmt == "csv":
        with db.get_db() as conn:
            rows = conn.execute("SELECT timestamp, message, reply, emotion, satisfaction, interest FROM conversations ORDER BY timestamp DESC LIMIT 1000").fetchall()
        output = io.BytesIO()
        output.write(b'\xef\xbb\xbf')
        wrapper = io.TextIOWrapper(output, encoding='utf-8', newline='')
        writer = csv.writer(wrapper)
        writer.writerow(["时间", "游客问题", "数字人回复", "情绪", "满意度", "兴趣偏好"])
        for r in rows:
            writer.writerow([csv_safe_value(r["timestamp"]), csv_safe_value(r["message"]), csv_safe_value(r["reply"]), csv_safe_value(r["emotion"]), r["satisfaction"] or "", csv_safe_value(r["interest"])])
        wrapper.flush()
        csv_content = output.getvalue()
        return Response(csv_content, mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=conversations_export.csv; charset=utf-8"})
    dashboard = db.compute_dashboard()
    report = db.compute_report()
    deep = deep_report.compute_deep_report()
    return jsonify({"code": 0, "data": {"dashboard": dashboard, "report": report, "deep": deep}})


# ============= ADMIN SETTINGS =============

@bp.route("/api/v1/admin/settings", methods=["GET"])
@require_admin
def get_settings():
    """Return editable credentials plus read-only active runtime status.

    ``responseTargetMs`` is deliberately ``None`` because the application has
    no tunable target-latency setting.  Clients must treat the fixed fields as
    display-only; PUT rejects them instead of persisting stale form values.
    """
    s = db.get_settings()
    # 脱敏: 密码哈希(以及任何历史遗留的明文密码)绝不出现在响应中
    s.pop("admin_password", None)
    # These values describe the active runtime and architecture.  They are not
    # persisted form controls, so stale legacy rows must not be echoed back.
    s["aiModel"] = ai_service.LLM_PROVIDER
    s["knowledgeMode"] = _KNOWLEDGE_MODE_DESCRIPTION
    s["responseTargetMs"] = None
    s["emotionEngine"] = _EMOTION_ENGINE_DESCRIPTION
    s["asrMode"] = f"SiliconFlow /audio/transcriptions（{ai_service.ASR_MODEL}）"
    s["ttsVoice"] = next(
        (name for name, voice in ai_service.VOICE_MAP.items() if voice == ai_service.TTS_VOICE),
        ai_service.TTS_VOICE,
    )
    s["ttsEnabled"] = s.get("ttsEnabled", "True")
    if _env_admin_credentials_configured():
        s["adminUser"] = os.environ.get("ADMIN_USERNAME", "").strip()
        s["admin_username"] = s["adminUser"]
    s["_meta"] = {
        "availableProviders": ["deepseek", "siliconflow", "xunfei"],
        "currentProvider": ai_service.LLM_PROVIDER,
        "availableVoices": list(ai_service.VOICE_MAP.keys()),
    }
    return jsonify({"code": 0, "data": s})


@bp.route("/api/v1/admin/settings", methods=["PUT"])
@require_admin
def update_settings():
    """Update only admin credentials and the active TTS voice.

    This is intentionally stricter than the legacy settings endpoint: fixed
    runtime fields and unknown keys are rejected with a stable 400 response.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"code": 400, "message": "设置请求必须是 JSON 对象"}), 400
    fixed_fields = sorted(_READ_ONLY_SETTINGS.intersection(payload))
    if fixed_fields:
        return jsonify({"code": 400, "message": f"以下系统配置为只读，不可修改: {', '.join(fixed_fields)}"}), 400
    unknown_fields = sorted(set(payload) - _WRITABLE_SETTINGS)
    if unknown_fields:
        return jsonify({"code": 400, "message": f"不支持修改的设置字段: {', '.join(unknown_fields)}"}), 400
    if "ttsVoice" in payload and (
        not isinstance(payload["ttsVoice"], str) or payload["ttsVoice"] not in ai_service.VOICE_MAP
    ):
        return jsonify({"code": 400, "message": "TTS 语音不在可用选项中"}), 400

    env_credentials = _env_admin_credentials_configured()
    if env_credentials:
        expected_username = os.environ.get("ADMIN_USERNAME", "").strip()
        requested_password = str(payload.get("admin_password") or "")
        requested_usernames = [
            str(payload[key]).strip()
            for key in ("adminUser", "admin_username")
            if key in payload and str(payload[key]).strip()
        ]
        if requested_password or any(username != expected_username for username in requested_usernames):
            return jsonify({"code": 400, "message": "环境变量已管理管理员凭据，请修改部署环境变量"}), 400
    merged: dict[str, Any] = {}
    for key in _WRITABLE_SETTINGS - {"admin_password"}:
        if key in payload and not (env_credentials and key in {"adminUser", "admin_username", "admin_password"}):
            merged[key] = payload[key]
    new_password = "" if env_credentials else str(payload.get("admin_password") or "")
    if new_password:
        # 只存 scrypt 哈希, 明文密码不落库
        merged["admin_password"] = generate_password_hash(new_password)
    if merged:
        db.update_settings(merged)

    # A password change must immediately invalidate every previously issued token.
    # 仅在真正改密(非空新密码)时吊销, 避免提交空密码误踢全部会话
    if new_password:
        db.revoke_admin_sessions()

    if "ttsVoice" in payload and payload["ttsVoice"] in ai_service.VOICE_MAP:
        import ai_service as ai_svc
        ai_svc.TTS_VOICE = ai_service.VOICE_MAP.get(payload["ttsVoice"], ai_svc.TTS_VOICE)

    _log_op("update", "settings", detail="更新系统设置")
    return jsonify({"code": 0})


# ============= ADMIN CONVERSATIONS =============

@bp.route("/api/v1/admin/conversations", methods=["GET"])
@require_admin
def get_conversations():
    pagination = parse_pagination(20)
    if not pagination:
        return jsonify({"code": 400, "message": "分页参数无效"}), 400
    page, page_size = pagination
    filters, error = db._parse_conversation_filters(request.args)
    if error:
        return jsonify({"code": 400, "message": error}), 400
    return jsonify({"code": 0, "data": db.get_conversations(page, page_size, **filters)})


@bp.route("/api/v1/admin/conversations/analyze", methods=["POST"])
@require_admin
def analyze_conversations():
    payload = request.get_json(silent=True)
    filters, sample_limit, error = conversation_analysis.parse_analysis_request(payload)
    if error:
        return jsonify({"code": 400, "message": error}), 400
    try:
        report = conversation_analysis.analyze_conversations(filters, sample_limit=sample_limit)
    except ValueError as exc:
        return jsonify({"code": 400, "message": str(exc)}), 400
    total = report.get("scope", {}).get("totalConversations", 0)
    _log_op("create", "conversation-analysis", detail=f"分析筛选结果 {total} 条对话")
    return jsonify({"code": 0, "data": report})
