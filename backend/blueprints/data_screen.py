"""Blueprint for data-screen dashboard API (overview, deep report, feedback)."""
from __future__ import annotations

from flask import Blueprint, jsonify, send_from_directory

import database as db
import deep_report
from blueprints.admin_core import require_admin
from runtime_paths import APP_ROOT

ADMIN_DIR = APP_ROOT / "admin"

bp = Blueprint("data_screen", __name__)


def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@bp.route("/data-screen/assets/<path:filename>")
def serve_asset(filename: str):
    """Serve static assets (like background images) from the app root."""
    return _no_cache(send_from_directory(str(APP_ROOT), filename))


@bp.route("/data-screen")
def data_screen():
    return _no_cache(send_from_directory(ADMIN_DIR, "data-screen.html"))


@bp.route("/api/v1/data-screen/overview")
@require_admin
def data_screen_overview():
    try:
        data = db.compute_dashboard()
        return jsonify({"code": 0, "data": data})
    except Exception as e:
        return jsonify({"code": 500, "message": "数据查询失败，请稍后重试"}), 500


@bp.route("/api/v1/data-screen/deep")
@require_admin
def data_screen_deep():
    try:
        data = deep_report.compute_deep_report()
        return jsonify({"code": 0, "data": data})
    except Exception as e:
        return jsonify({"code": 500, "message": "数据查询失败，请稍后重试"}), 500


@bp.route("/api/v1/data-screen/feedback")
@require_admin
def data_screen_feedback():
    try:
        data = db.get_latest_feedback(20)
        recent = [
            {
                "message": item.get("message", ""),
                "reply": item.get("reply", ""),
                "satisfaction": item.get("satisfaction"),
                "timestamp": item.get("timestamp", "")
            }
            for item in data
        ]
        return jsonify({"code": 0, "data": recent})
    except Exception as e:
        return jsonify({"code": 500, "message": "数据查询失败，请稍后重试"}), 500
