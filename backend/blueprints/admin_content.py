"""Blueprint for admin content management (knowledge, FAQ, avatar/VRM,
guide presets, operation logs)."""
from __future__ import annotations

import csv
import io
import uuid
from pathlib import Path
from werkzeug.utils import secure_filename

from flask import Blueprint, Response, jsonify, request

import database as db
from blueprints.admin_core import require_admin, _log_op
from blueprints.common import parse_pagination, csv_safe_value
from runtime_paths import MODEL_DIR

bp = Blueprint("admin_content", __name__)

def rebuild_collection(items):
    try:
        from rag_vector import rebuild_collection as _rebuild
        _rebuild(items)
    except Exception:
        pass


# ============= KNOWLEDGE =============

@bp.route("/api/v1/admin/knowledge", methods=["GET"])
@require_admin
def get_knowledge():
    search = request.args.get("search", "").strip()
    pagination = parse_pagination(10)
    if not pagination:
        return jsonify({"code": 400, "message": "分页参数无效"}), 400
    page, page_size = pagination
    return jsonify({"code": 0, "data": db.get_knowledge(search, page, page_size)})


@bp.route("/api/v1/admin/knowledge", methods=["POST"])
@require_admin
def add_knowledge():
    payload = request.get_json(silent=True) or {}
    tags = payload.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    item = db.add_knowledge(
        title=payload.get("title", "未命名知识"),
        category=payload.get("category", "景点讲解"),
        tags=tags,
        content=payload.get("content", ""),
        source=payload.get("source", "后台录入"),
    )
    rebuilt = db.get_all_knowledge()
    rebuild_collection(rebuilt)
    _log_op("create", "knowledge", item.get("id", ""), f"新增知识：{payload.get('title', '')}")
    return jsonify({"code": 0, "data": item})


@bp.route("/api/v1/admin/knowledge/<item_id>", methods=["PUT"])
@require_admin
def update_knowledge(item_id: str):
    payload = request.get_json(silent=True) or {}
    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    db.update_knowledge(
        item_id,
        title=payload.get("title"),
        category=payload.get("category"),
        tags=tags,
        content=payload.get("content"),
        source=payload.get("source"),
    )
    rebuilt = db.get_all_knowledge()
    rebuild_collection(rebuilt)
    _log_op("update", "knowledge", item_id, f"更新知识：{payload.get('title', '')}")
    return jsonify({"code": 0})


@bp.route("/api/v1/admin/knowledge/<item_id>", methods=["DELETE"])
@require_admin
def delete_knowledge(item_id: str):
    db.delete_knowledge(item_id)
    rebuilt = db.get_all_knowledge()
    rebuild_collection(rebuilt)
    _log_op("delete", "knowledge", item_id, f"删除知识：{item_id}")
    return jsonify({"code": 0})


# ============= FAQ =============

@bp.route("/api/v1/admin/faq", methods=["GET"])
@require_admin
def get_faq():
    items = db.get_faq()
    return jsonify({"code": 0, "data": {"list": items, "total": len(items)}})


@bp.route("/api/v1/admin/faq", methods=["POST"])
@require_admin
def add_faq():
    payload = request.get_json(silent=True) or {}
    item = db.add_faq(question=payload.get("question", ""), answer=payload.get("answer", ""), category=payload.get("category", "常见问题"))
    _log_op("create", "faq", item.get("id", ""), f"新增高频问题：{payload.get('question', '')}")
    return jsonify({"code": 0, "data": item})


@bp.route("/api/v1/admin/faq/<item_id>", methods=["PUT"])
@require_admin
def update_faq(item_id: str):
    payload = request.get_json(silent=True) or {}
    db.update_faq(item_id, question=payload.get("question"), answer=payload.get("answer"), category=payload.get("category"))
    _log_op("update", "faq", item_id, f"更新高频问题：{payload.get('question', '')}")
    return jsonify({"code": 0})


@bp.route("/api/v1/admin/faq/<item_id>", methods=["DELETE"])
@require_admin
def delete_faq(item_id: str):
    db.delete_faq(item_id)
    _log_op("delete", "faq", item_id, f"删除高频问题：{item_id}")
    return jsonify({"code": 0})


# ============= AVATAR =============

@bp.route("/api/v1/admin/avatar", methods=["GET"])
@require_admin
def get_avatar():
    return jsonify({"code": 0, "data": db.get_avatar_config()})


@bp.route("/api/v1/admin/avatar", methods=["PUT"])
@require_admin
def update_avatar():
    _log_op("update", "avatar", detail="拒绝修改数字人固定属性", result="failure")
    return jsonify({"code": 400, "message": "数字人固定属性只读，请仅修改模型启用状态"}), 400


# ============= VRM MODELS =============

@bp.route("/api/v1/admin/avatar/models", methods=["GET"])
@require_admin
def list_vrm_models():
    return jsonify({"code": 0, "data": db.get_vrm_models(MODEL_DIR)})


@bp.route("/api/v1/admin/avatar/models/status", methods=["PUT"])
@require_admin
def update_vrm_model_status():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"code": 400, "message": "模型状态请求必须是 JSON 对象"}), 400
    unknown = sorted(set(payload) - {"modelId", "modelName", "enabled"})
    if unknown:
        return jsonify({"code": 400, "message": "模型管理只允许修改 enabled"}), 400
    model_name = str(payload.get("modelId") or payload.get("modelName") or "").strip()
    if not model_name or not isinstance(payload.get("enabled", True), bool):
        return jsonify({"code": 400, "message": "请提供模型 modelId 和布尔 enabled"}), 400
    try:
        model = db.set_vrm_model_enabled(model_name, payload.get("enabled", True), MODEL_DIR)
    except ValueError as exc:
        _log_op("update", "vrm-model", model_name, str(exc), result="failure")
        return jsonify({"code": 400, "message": str(exc)}), 400
    _log_op("update", "vrm-model", model_name, f"模型 enabled={model['enabled']}")
    return jsonify({"code": 0, "data": model})


@bp.route("/api/v1/admin/avatar/models/status/batch", methods=["PUT"])
@require_admin
def enable_all_vrm_models():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or set(payload) != {"enabled"} or payload.get("enabled") is not True:
        return jsonify({"code": 400, "message": "批量操作仅支持启用全部模型"}), 400
    try:
        models = db.set_vrm_models_enabled(True, MODEL_DIR)
    except ValueError as exc:
        _log_op("update", "vrm-model", detail=str(exc), result="failure")
        return jsonify({"code": 400, "message": str(exc)}), 400
    _log_op("update", "vrm-model", detail=f"批量启用 {len(models)} 个模型")
    return jsonify({"code": 0, "data": models})


@bp.route("/api/v1/admin/avatar/upload", methods=["POST"])
@require_admin
def upload_vrm_model():
    _log_op("create", "vrm-model", detail="拒绝上传 VRM 模型", result="failure")
    return jsonify({"code": 400, "message": "后台仅支持管理已有模型的启用状态，不允许上传模型"}), 400


# ============= GUIDE PRESETS =============

@bp.route("/api/v1/admin/guide-presets", methods=["GET"])
@require_admin
def list_guide_presets():
    return jsonify({"code": 0, "data": db.get_guide_presets()})


@bp.route("/api/v1/admin/guide-presets", methods=["POST"])
@require_admin
def save_guide_preset():
    _log_op("update", "guide-preset", detail="拒绝修改模型固定属性", result="failure")
    return jsonify({"code": 400, "message": "声音、服装、语气和表情固定属性不可修改"}), 400


@bp.route("/api/v1/admin/guide-presets/<model_name>", methods=["DELETE"])
@require_admin
def delete_guide_preset(model_name: str):
    _log_op("delete", "guide-preset", model_name, "拒绝删除模型固定属性", result="failure")
    return jsonify({"code": 400, "message": "模型固定属性不可删除"}), 400


@bp.route("/api/v1/admin/guide-presets/batch", methods=["PUT"])
@require_admin
def batch_save_guide_presets():
    _log_op("update", "guide-preset", detail="拒绝批量修改模型固定属性", result="failure")
    return jsonify({"code": 400, "message": "模型固定属性不可批量修改"}), 400


# ============= OPERATION LOGS =============

@bp.route("/api/v1/admin/operation-logs", methods=["GET"])
@require_admin
def get_operation_logs():
    pagination = parse_pagination(10)
    if not pagination:
        return jsonify({"code": 400, "message": "分页参数无效"}), 400
    page, page_size = pagination
    action = request.args.get("action", "").strip()
    resource = request.args.get("resource", "").strip()
    return jsonify({"code": 0, "data": db.get_operation_logs(page, page_size, action, resource)})


@bp.route("/api/v1/admin/operation-logs/export", methods=["GET"])
@require_admin
def export_operation_logs():
    action = request.args.get("action", "").strip()
    resource = request.args.get("resource", "").strip()
    data = db.get_operation_logs(1, 100000, action, resource)
    output = io.BytesIO()
    output.write(b'\xef\xbb\xbf')
    wrapper = io.TextIOWrapper(output, encoding='utf-8', newline='')
    writer = csv.writer(wrapper)
    writer.writerow(["时间", "管理员", "操作", "模块", "资源ID", "描述", "IP", "状态"])
    for r in data.get("list", []):
        writer.writerow([csv_safe_value(r["timestamp"]), csv_safe_value(r["admin_user"]), csv_safe_value(r["action"]), csv_safe_value(r["resource"]), csv_safe_value(r.get("resource_id", "")), csv_safe_value(r.get("detail", "")), csv_safe_value(r.get("ip_address", "")), csv_safe_value(r["result"])])
    wrapper.flush()
    csv_content = output.getvalue()
    _log_op("export", "operation-logs", detail="导出操作日志")
    return Response(csv_content, mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=operation_logs.csv; charset=utf-8"})
