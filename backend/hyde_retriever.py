from __future__ import annotations

import re
from typing import Any


def _normalize_query(q: str) -> str:
    return re.sub(r'\s+', '', q.strip().lower())


def generate_hyde_document(query: str) -> str:
    """Return the query itself as the HyDE document.
    
    Previously this made an expensive LLM call to generate a hypothetical document,
    but with ONNX MiniLM (384-dim) the benefit was marginal. The rewritten query
    (from query_rewriter.py) already provides good keyword coverage for vector search.
    """
    return query


_HYDE_CACHE: dict[str, list[dict[str, Any]]] = {}
_HYDE_CACHE_MAX = 512


FACT_INDICATORS = ["多高", "高度", "门票", "票价", "多少钱", "开放时间", "几点",
                    "位于", "在哪", "地址", "历史", "建于", "始建于", "传说", "故事"]


def search_with_hyde(query: str, search_fn, top_k: int = 5, rewritten_query: str | None = None,
                     precomputed_hyde: str | None = None) -> list[dict[str, Any]]:
    search_query = rewritten_query or query
    is_fact_query = any(kw in search_query for kw in FACT_INDICATORS)
    if not is_fact_query:
        return search_fn(search_query, top_k=top_k)

    cache_key = _normalize_query(search_query)
    if cache_key in _HYDE_CACHE:
        return _HYDE_CACHE[cache_key]

    hyde_doc = precomputed_hyde if precomputed_hyde is not None else generate_hyde_document(query)
    hyde_query = f"{search_query} {hyde_doc[:300]}"
    results = search_fn(hyde_query, top_k=top_k)

    if len(_HYDE_CACHE) >= _HYDE_CACHE_MAX:
        oldest = next(iter(_HYDE_CACHE))
        del _HYDE_CACHE[oldest]
    _HYDE_CACHE[cache_key] = results

    return results
