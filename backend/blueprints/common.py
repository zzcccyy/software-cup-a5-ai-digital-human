# -*- coding: utf-8 -*-
"""跨蓝图共享的小工具函数(无业务状态)."""

from __future__ import annotations

from typing import Any

from flask import request


def parse_pagination(default_page_size: int, max_page_size: int = 100) -> tuple[int, int] | None:
    """Parse bounded paging parameters without turning malformed URLs into 500s."""
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", default_page_size))
    except (TypeError, ValueError):
        return None
    if page < 1 or not 1 <= page_size <= max_page_size:
        return None
    return page, page_size


def csv_safe_value(value: Any) -> str:
    value = str(value or "").replace("\n", " ").replace("\r", "")
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value
