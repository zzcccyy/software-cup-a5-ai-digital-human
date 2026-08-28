from __future__ import annotations

import os
import re
from pathlib import Path
from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from runtime_paths import BACKEND_DIR

load_dotenv(BACKEND_DIR / ".env")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")

_http = requests.Session()
_retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=5, pool_maxsize=5)
_http.mount("https://", _adapter)
_http.mount("http://", _adapter)

# Cache rewritten queries to avoid redundant API calls
_rewrite_cache: dict[str, str] = {}
_CACHE_MAX = 512

# Patterns that indicate the query is already keyword-like (no need to rewrite)
_ALREADY_CLEAN = re.compile(r"^[\w\u4e00-\u9fff\s\d\+\-\.%]{1,30}$")


def _is_essentially_factual(query: str) -> bool:
    fact_words = ["多高", "多少米", "高度", "门票", "票价", "多少钱", "开放时间", "几点",
                  "位于", "在哪", "地址", "历史", "建于", "始建于", "什么时候", "传说",
                  "多远", "多大", "多久", "怎么去", "怎么走", "公交", "地铁", "停车",
                  "电话", "可以", "能", "有", "是", "什么", "哪里", "谁", "哪年"]
    return any(w in query for w in fact_words)


def _strip_punctuation(text: str) -> str:
    return re.sub(r"[\s,，。！？、；：.!?;:\"'（）()【】\[\]{}《》]+", " ", text).strip()


def _normalize_keywords(text: str) -> str:
    """Simple normalization: deduplicate, trim, remove stopwords"""
    tokens = text.split()
    seen = set()
    result = []
    for t in tokens:
        t = t.strip()
        if t and t not in seen and len(t) > 1:
            seen.add(t)
            result.append(t)
    return " ".join(result)


def _call_llm_for_rewrite(raw: str) -> str | None:
    if os.getenv("QUERY_REWRITE_USE_LLM", "0").lower() not in {"1", "true", "yes"}:
        return None
    if not DEEPSEEK_API_KEY:
        return None
    try:
        resp = _http.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "你是一个景区知识库检索助手。请将用户的口语化问题改写为简洁的关键词检索语句。要求：1. 保留所有事实关键词（人名、地名、数字、时间等）2. 移除口语化填充词（啊、呢、吧、那个、我想问一下等）3. 用空格分隔关键词 4. 只输出改写结果，不要任何解释或标点"},
                    {"role": "user", "content": raw},
                ],
                "temperature": 0.1,
                "max_tokens": 100,
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"].strip()
        result = _strip_punctuation(result)
        result = _normalize_keywords(result)
        return result if len(result) > 3 else None
    except Exception as e:
        print(f"[query_rewriter] LLM rewrite failed: {e}")
        return None


def rewrite_query(raw: str) -> str:
    """Rewrite a user query to be more retrieval-friendly.

    For short/factual queries that are already well-formed, returns raw unchanged.
    For verbose/spoken queries, uses DeepSeek to generate keyword-style rewrite.
    Caches results by normalized raw query.
    """
    clean = _strip_punctuation(raw)
    if not clean or len(clean) < 2:
        return raw

    # If the query is already short and keyword-like, skip rewrite
    if len(clean) <= 15 or _ALREADY_CLEAN.match(clean):
        return raw

    # Check cache
    cache_key = clean.lower()
    if cache_key in _rewrite_cache:
        return _rewrite_cache[cache_key]

    rewritten = _call_llm_for_rewrite(raw)

    # If rewrite is too similar to original or failed, use original
    if not rewritten or rewritten == clean.lower() or len(rewritten) < 4:
        result = raw
    else:
        result = rewritten

    # Manage cache size
    if len(_rewrite_cache) >= _CACHE_MAX:
        _rewrite_cache.clear()
    _rewrite_cache[cache_key] = result

    return result
