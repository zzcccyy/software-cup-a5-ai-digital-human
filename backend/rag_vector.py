#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
import os
import hashlib
import threading
import uuid
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from runtime_paths import BACKEND_DIR

CHROMA_DB_DIR = BACKEND_DIR / "chroma_db"
_DEFAULT_COLLECTION_NAME = "scenic_knowledge"
_COLLECTION_NAME_PATTERN = re.compile(r"^scenic_knowledge(?:_tmp_[0-9a-f]{12})?$")
_ACTIVE_COLLECTION_MARKER = CHROMA_DB_DIR / ".active_collection.json"

_http = requests.Session()
_retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=5, pool_maxsize=5)
_http.mount("https://", _adapter)
_http.mount("http://", _adapter)

_embedding_function = None
_chroma_collection = None
_using_vector = False
_CHROMA_SWAP_LOCK = threading.Lock()  # 序列化并发 rebuild


def _read_active_collection_name() -> str:
    try:
        data = json.loads(_ACTIVE_COLLECTION_MARKER.read_text(encoding="utf-8"))
        name = data.get("name", "") if isinstance(data, dict) else ""
        if isinstance(name, str) and _COLLECTION_NAME_PATTERN.fullmatch(name):
            return name
    except (OSError, ValueError, TypeError):
        pass
    return _DEFAULT_COLLECTION_NAME


def _persist_active_collection_name(name: str) -> None:
    if not _COLLECTION_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"invalid Chroma collection name: {name!r}")
    _ACTIVE_COLLECTION_MARKER.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _ACTIVE_COLLECTION_MARKER.with_name(
        f"{_ACTIVE_COLLECTION_MARKER.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp_path.write_text(json.dumps({"name": name}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, _ACTIVE_COLLECTION_MARKER)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _load_collection():
    active_name = _read_active_collection_name()
    try:
        collection = _client.get_collection(active_name, embedding_function=_embedding_function)
        return collection, collection.count()
    except Exception:
        try:
            collection = _client.get_collection(
                _DEFAULT_COLLECTION_NAME, embedding_function=_embedding_function
            )
            count = collection.count()
        except Exception:
            collection = _client.create_collection(
                _DEFAULT_COLLECTION_NAME, embedding_function=_embedding_function
            )
            count = 0
        try:
            _persist_active_collection_name(collection.name)
        except Exception as marker_error:
            print(f"[rag_vector] active collection marker unavailable: {marker_error}")
        return collection, count

try:
    import chromadb
    from chromadb.utils import embedding_functions

    _client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
    _embedding_function = embedding_functions.ONNXMiniLM_L6_V2()
    _embedding_function.DOWNLOAD_PATH = BACKEND_DIR / "onnx_models" / _embedding_function.MODEL_NAME
    _chroma_collection, count = _load_collection()
    _using_vector = True
    print(f"[rag_vector] ONNX MiniLM ready, collection has {count} docs")
except Exception as e:
    print(f"[rag_vector] Chroma init failed, will use fallback: {e}")
    _using_vector = False
    _chroma_collection = None


def rebuild_collection(items: list[dict]):
    """关键修复: 原子重建 — 先建临时集合 add 全部文档, 原子切换全局引用, 再删旧集合.
    重建窗口内查询仍走旧集合 (不返回空, 不抛异常); 并发 rebuild 由锁串行化."""
    global _chroma_collection
    if not _using_vector or _chroma_collection is None:
        return

    with _CHROMA_SWAP_LOCK:
        tmp_name = f"scenic_knowledge_tmp_{uuid.uuid4().hex[:12]}"
        try:
            tmp_collection = _client.create_collection(tmp_name, embedding_function=_embedding_function)
        except Exception as e:
            print(f"[rag_vector] create tmp collection failed: {e}")
            return

        ids_to_add = []
        documents_to_add = []
        metadatas_to_add = []

        for item in items:
            content = item.get("content", "")
            title = item.get("title", "")
            text = f"{title} {content}"
            doc_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, text))

            ids_to_add.append(doc_id)
            documents_to_add.append(text)
            metadatas_to_add.append({
                "title": title,
                "category": item.get("category", ""),
                "tags": ",".join(item.get("tags", [])),
                "source": item.get("source", ""),
                "item_id": item.get("id", ""),
            })

        try:
            if ids_to_add:
                tmp_collection.add(
                    ids=ids_to_add,
                    documents=documents_to_add,
                    metadatas=metadatas_to_add,
                )
        except Exception as e:
            # 中途失败清理临时集合残留, 原集合保持可用
            try:
                _client.delete_collection(tmp_name)
            except Exception:
                pass
            print(f"[rag_vector] rebuild add failed, tmp collection cleaned: {e}")
            return

        try:
            _persist_active_collection_name(tmp_name)
        except Exception as e:
            try:
                _client.delete_collection(tmp_name)
            except Exception:
                pass
            print(f"[rag_vector] active collection marker write failed, old collection kept: {e}")
            return

        # 原子切换全局引用, 之后查询走新集合; 旧集合在切换后才删, 查询永不落空
        old_collection = _chroma_collection
        _chroma_collection = tmp_collection
        print(f"[rag_vector] Rebuilt collection with {len(ids_to_add)} docs")

        # 关键修复: 删除的是切换前的旧集合本身(可能是上一轮遗留的 tmp_* 集合),
        # 不能硬编码初始名 "scenic_knowledge", 否则连续重建时旧临时集合永久泄漏
        try:
            if old_collection is not None:
                _client.delete_collection(old_collection.name)
        except Exception as e:
            # 旧集合删除失败不阻断主流程, 下次重建时会被再次清理
            print(f"[rag_vector] old collection delete failed (ignored): {e}")


def search_knowledge_vector(query: str, top_k: int = 10) -> list[dict[str, Any]]:
    if not _using_vector or _chroma_collection is None:
        return _fallback_keyword_search(query, top_k)

    try:
        results = _chroma_collection.query(
            query_texts=[query],
            n_results=min(top_k, 20),
            include=["documents", "metadatas", "distances"],
        )

        items = []
        if results and results["ids"] and results["ids"][0]:
            for i in range(len(results["ids"][0])):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                items.append({
                    "id": meta.get("item_id", ""),
                    "title": meta.get("title", ""),
                    "category": meta.get("category", ""),
                    "tags": meta.get("tags", "").split(",") if meta.get("tags") else [],
                    "content": results["documents"][0][i] if results["documents"] else "",
                    "source": meta.get("source", ""),
                    "score": 1.0 - results["distances"][0][i] if results.get("distances") else 0.5,
                })
        return items or _fallback_keyword_search(query, top_k)
    except Exception as e:
        print(f"[rag_vector] Chroma query failed: {e}")
        return _fallback_keyword_search(query, top_k)


def _fallback_keyword_search(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    try:
        from database import get_all_knowledge
    except ImportError:
        return []

    items = get_all_knowledge()
    query_lower = query.lower()
    query_tokens = [t for t in _tokenize(query) if len(t) >= 2]
    if not query_tokens:
        return []

    scored = []
    for item in items:
        haystack = " ".join([
            item.get("title", ""),
            item.get("category", ""),
            " ".join(item.get("tags", [])),
            item.get("content", ""),
        ]).lower()

        score = 0
        matched_tokens = set()
        for token in query_tokens:
            if token in haystack:
                score += 2
                matched_tokens.add(token)

        for tag in item.get("tags", []):
            if tag.lower() in query_lower:
                score += 3
                matched_tokens.add(tag.lower())

        if score > 0 and matched_tokens:
            coverage = len(matched_tokens) / max(len(query_tokens), 1)
            scored.append((score, coverage, item))

    scored.sort(key=lambda x: (x[1], x[0]), reverse=True)
    result = []
    for score, coverage, item in scored[:top_k]:
        item["score"] = min(coverage * 0.65 + min(score / 50.0, 0.2), 0.65)
        item["retriever"] = "keyword-fallback"
        result.append(item)
    return result


def _tokenize(text: str) -> list[str]:
    raw = re.split(r"[，。！？、,.!?\s:/：；;\-]+", text or "")
    tokens = [t.strip().lower() for t in raw if t.strip()]
    compact = re.sub(r"\s+", "", text.lower())
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
