#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import hashlib
import json
import logging
import os
import uuid
import random
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from runtime_paths import BACKEND_DIR, MODEL_DIR

logger = logging.getLogger(__name__)

DB_DIR = BACKEND_DIR / "admin_data"
DB_PATH = DB_DIR / "scenic.db"
DB_DIR.mkdir(parents=True, exist_ok=True)


def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# Per-thread connection pool: every thread reuses a single connection, so the
# total connection count is bounded by the thread count and SQLite's
# thread-affinity rule is respected (no cross-thread sharing).
_thread_local = threading.local()


def _get_thread_conn() -> sqlite3.Connection:
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = _open_conn()
        _thread_local.conn = conn
    return conn


def _reset_thread_conns():
    """Close and drop the cached thread-local connection (test helper)."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _thread_local.conn = None


def get_conn() -> sqlite3.Connection:
    """Return the pooled per-thread connection (thin wrapper kept for
    compatibility with existing callers and the test harness)."""
    return _get_thread_conn()


def get_read_conn() -> sqlite3.Connection:
    return _get_thread_conn()


def _begin_write(conn: sqlite3.Connection):
    for attempt in range(3):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                logger.warning("database locked, retry %d/2: %s", attempt + 1, e)
                time.sleep(0.05 * (2 ** attempt))
                continue
            raise


@contextmanager
def get_db(write: bool = False):
    """Yield a pooled connection; write transactions commit on success and roll
    back on error. Write transactions use BEGIN IMMEDIATE (avoids
    deferred-upgrade deadlocks under WAL) and are retried on lock contention.
    Nested write contexts share the outer transaction.
    """
    conn = get_conn()
    depth = getattr(_thread_local, "tx_depth", 0)
    if write and depth == 0 and not conn.in_transaction:
        _begin_write(conn)
    if write:
        _thread_local.tx_depth = depth + 1
    try:
        yield conn
    except BaseException:
        if write:
            _thread_local.tx_depth = max(0, depth)
            if depth == 0 and conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
        raise
    else:
        if write:
            _thread_local.tx_depth = max(0, depth)
            if depth == 0 and conn.in_transaction:
                conn.execute("COMMIT")


# Simple TTL cache for knowledge & FAQ (avoids repeated full-table scans)
_knowledge_cache: list[dict[str, Any]] | None = None
_knowledge_cache_ts: float = 0
_KNOWLEDGE_CACHE_TTL = 30.0

_faq_cache: list[dict[str, Any]] | None = None
_faq_cache_ts: float = 0
_FAQ_CACHE_TTL = 30.0


def invalidate_caches():
    global _knowledge_cache, _faq_cache, _knowledge_cache_ts, _faq_cache_ts
    _knowledge_cache = None
    _faq_cache = None
    _knowledge_cache_ts = 0
    _faq_cache_ts = 0


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with get_db(write=True) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '景点讲解',
            tags TEXT NOT NULL DEFAULT '[]',
            content TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '后台录入',
            source_hash TEXT DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS faq (
            id TEXT PRIMARY KEY,
            question TEXT NOT NULL DEFAULT '',
            answer TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '常见问题',
            usage_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS routes (
            id TEXT PRIMARY KEY,
            interest TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            duration TEXT NOT NULL DEFAULT '',
            suitable_for TEXT NOT NULL DEFAULT '[]',
            stops TEXT NOT NULL DEFAULT '[]',
            pitch TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT 'guest',
            message TEXT NOT NULL DEFAULT '',
            reply TEXT NOT NULL DEFAULT '',
            emotion TEXT NOT NULL DEFAULT 'warm',
            satisfaction INTEGER DEFAULT NULL,
            interest TEXT NOT NULL DEFAULT 'history',
            topics TEXT NOT NULL DEFAULT '[]',
            timestamp TEXT NOT NULL DEFAULT '',
            latency_ms INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS avatar_config (
            id TEXT PRIMARY KEY DEFAULT 'default',
            theme TEXT NOT NULL DEFAULT '',
            active_profile TEXT NOT NULL DEFAULT 'gentle_guide',
            profiles TEXT NOT NULL DEFAULT '[]',
            vrm_model TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS guide_presets (
            id TEXT PRIMARY KEY,
            model_name TEXT NOT NULL UNIQUE,
            voice TEXT NOT NULL DEFAULT '',
            outfit TEXT NOT NULL DEFAULT '',
            style TEXT NOT NULL DEFAULT '',
            expression_bias TEXT NOT NULL DEFAULT 'warm',
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);
        CREATE INDEX IF NOT EXISTS idx_conversations_timestamp ON conversations(timestamp);
        CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);
        CREATE INDEX IF NOT EXISTS idx_faq_usage ON faq(usage_count DESC);
        CREATE INDEX IF NOT EXISTS idx_knowledge_updated_at ON knowledge(updated_at);
        CREATE INDEX IF NOT EXISTS idx_faq_question ON faq(question);

        CREATE TABLE IF NOT EXISTS admin_operation_logs (
            id TEXT PRIMARY KEY,
            admin_user TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            resource TEXT NOT NULL DEFAULT '',
            resource_id TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            ip_address TEXT DEFAULT '',
            result TEXT NOT NULL DEFAULT 'success',
            timestamp TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_admin_logs_timestamp ON admin_operation_logs(timestamp);
        CREATE INDEX IF NOT EXISTS idx_admin_logs_action ON admin_operation_logs(action);
        CREATE INDEX IF NOT EXISTS idx_admin_logs_resource ON admin_operation_logs(resource);
        CREATE INDEX IF NOT EXISTS idx_admin_logs_action_ts ON admin_operation_logs(action, timestamp);
        CREATE INDEX IF NOT EXISTS idx_admin_logs_resource_ts ON admin_operation_logs(resource, timestamp);

        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires ON admin_sessions(expires_at);
    """)
    conn.commit()

    # ========== Migrations ==========
    try:
        conn.execute("ALTER TABLE avatar_config ADD COLUMN vrm_model TEXT NOT NULL DEFAULT ''")
    except Exception as e:
        logger.debug("migration avatar_config.vrm_model skipped: %s", e)
    try:
        conn.execute("ALTER TABLE conversations ADD COLUMN latency_ms INTEGER DEFAULT 0")
    except Exception as e:
        logger.debug("migration conversations.latency_ms skipped: %s", e)
    try:
        conn.execute("ALTER TABLE faq ADD COLUMN keywords TEXT NOT NULL DEFAULT '[]'")
    except Exception as e:
        logger.debug("migration faq.keywords skipped: %s", e)
    try:
        conn.execute("ALTER TABLE guide_presets ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    except Exception as e:
        logger.debug("migration guide_presets.enabled skipped: %s", e)

    # ========== FTS5 (external-content + triggers) ==========
    # Legacy FTS tables were contentful (each row stored its own copy) and the
    # 'rebuild' command could never re-read the real tables, so the index
    # drifted. Drop them and recreate as external-content tables kept in sync
    # by AFTER INSERT/UPDATE/DELETE triggers.
    def _fts_external(name: str) -> bool:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return bool(row) and "content=" in (row[0] or "")

    if not _fts_external("knowledge_fts"):
        conn.execute("DROP TABLE IF EXISTS knowledge_fts")
    if not _fts_external("faq_fts"):
        conn.execute("DROP TABLE IF EXISTS faq_fts")

    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            title, content, category, source,
            content='knowledge', content_rowid='rowid',
            tokenize='trigram'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS faq_fts USING fts5(
            question, answer, category,
            content='faq', content_rowid='rowid',
            tokenize='trigram'
        );

        CREATE TRIGGER IF NOT EXISTS knowledge_fts_ai AFTER INSERT ON knowledge BEGIN
            INSERT INTO knowledge_fts(rowid, title, content, category, source)
            VALUES (new.rowid, new.title, new.content, new.category, new.source);
        END;

        CREATE TRIGGER IF NOT EXISTS knowledge_fts_ad AFTER DELETE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, category, source)
            VALUES ('delete', old.rowid, old.title, old.content, old.category, old.source);
        END;

        CREATE TRIGGER IF NOT EXISTS knowledge_fts_au AFTER UPDATE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, category, source)
            VALUES ('delete', old.rowid, old.title, old.content, old.category, old.source);
            INSERT INTO knowledge_fts(rowid, title, content, category, source)
            VALUES (new.rowid, new.title, new.content, new.category, new.source);
        END;

        CREATE TRIGGER IF NOT EXISTS faq_fts_ai AFTER INSERT ON faq BEGIN
            INSERT INTO faq_fts(rowid, question, answer, category)
            VALUES (new.rowid, new.question, new.answer, new.category);
        END;

        CREATE TRIGGER IF NOT EXISTS faq_fts_ad AFTER DELETE ON faq BEGIN
            INSERT INTO faq_fts(faq_fts, rowid, question, answer, category)
            VALUES ('delete', old.rowid, old.question, old.answer, old.category);
        END;

        CREATE TRIGGER IF NOT EXISTS faq_fts_au AFTER UPDATE ON faq BEGIN
            INSERT INTO faq_fts(faq_fts, rowid, question, answer, category)
            VALUES ('delete', old.rowid, old.question, old.answer, old.category);
            INSERT INTO faq_fts(rowid, question, answer, category)
            VALUES (new.rowid, new.question, new.answer, new.category);
        END;
    """)

    # Repopulate the FTS index when it is missing rows (fresh migration or a
    # previously interrupted rebuild); the triggers keep it in sync afterwards.
    # COUNT(*)/full scans on an external-content FTS table fall back to the
    # content table when the index is empty, so probe with a MATCH instead:
    # an empty index returns zero hits even though the source row exists.
    def _fts_populated(conn, name: str, columns: tuple[str, ...]) -> bool:
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM {name} WHERE rowid = "
            f"(SELECT MAX(rowid) FROM {name})"
        ).fetchone()
        if not row:
            return True  # 源表为空, 无内容可漂移
        text = " ".join(c for c in row if c)
        probe = _FTS_MATCH_PUNCT_RE.sub("", text)[:6]
        if len(probe) < 3:
            return True  # 不足 3 字符无从探测, 保守跳过
        hits = conn.execute(
            f"SELECT COUNT(*) FROM {name}_fts WHERE {name}_fts MATCH ?",
            (probe,),
        ).fetchone()[0]
        return hits > 0

    if not _fts_populated(conn, "knowledge", ("title", "content")):
        conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('delete-all')")
        conn.execute(
            "INSERT INTO knowledge_fts(rowid, title, content, category, source) "
            "SELECT rowid, title, content, category, source FROM knowledge"
        )
    if not _fts_populated(conn, "faq", ("question", "answer")):
        conn.execute("INSERT INTO faq_fts(faq_fts) VALUES('delete-all')")
        conn.execute(
            "INSERT INTO faq_fts(rowid, question, answer, category) "
            "SELECT rowid, question, answer, category FROM faq"
        )


# ========== Generic CRUD helpers ==========

def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _parse_json_array(val: str, default: list = None) -> list:
    if default is None:
        default = []
    if not val:
        return default
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return default


def _to_json_array(val: list) -> str:
    return json.dumps(val, ensure_ascii=False)


# ========== Knowledge CRUD ==========

def get_knowledge(search: str = "", page: int = 1, page_size: int = 50) -> dict[str, Any]:
    offset = (page - 1) * page_size
    if search:
        like_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{like_search}%"
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge WHERE title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\' ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (like, like, like, like, page_size, offset)
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM knowledge WHERE title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\'",
                (like, like, like, like)
            ).fetchone()[0]
    else:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM knowledge ORDER BY updated_at DESC LIMIT ? OFFSET ?", (page_size, offset)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    items = []
    for r in rows:
        d = dict(r)
        d["tags"] = _parse_json_array(d.get("tags", "[]"))
        items.append(d)
    return {"list": items, "total": total, "page": page, "page_size": page_size}


def add_knowledge(title: str, category: str, tags: list, content: str, source: str = "后台录入", source_hash: str = "") -> dict:
    item_id = str(uuid.uuid4())
    ts = now_str()
    with get_db(write=True) as conn:
        conn.execute(
            "INSERT INTO knowledge (id, title, category, tags, content, source, source_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, title, category, _to_json_array(tags), content, source, source_hash, ts)
        )
    invalidate_caches()
    return {"id": item_id, "title": title, "category": category, "tags": tags, "content": content, "source": source, "updated_at": ts}


def update_knowledge(item_id: str, title: str = None, category: str = None, tags: list = None, content: str = None, source: str = None) -> bool:
    with get_db(write=True) as conn:
        existing = conn.execute("SELECT * FROM knowledge WHERE id=?", (item_id,)).fetchone()
        if not existing:
            return False
        ts = now_str()
        updates = {"title": title, "category": category, "tags": _to_json_array(tags) if tags is not None else None, "content": content, "source": source, "updated_at": ts}
        updates = {k: v for k, v in updates.items() if v is not None}
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE knowledge SET {set_clause} WHERE id=?", (*updates.values(), item_id))
    invalidate_caches()
    return True


def delete_knowledge(item_id: str) -> bool:
    with get_db(write=True) as conn:
        cursor = conn.execute("DELETE FROM knowledge WHERE id=?", (item_id,))
        affected = cursor.rowcount
    invalidate_caches()
    return affected > 0


def get_all_knowledge(use_cache: bool = True) -> list[dict]:
    global _knowledge_cache, _knowledge_cache_ts
    if use_cache and _knowledge_cache is not None and (time.time() - _knowledge_cache_ts) < _KNOWLEDGE_CACHE_TTL:
        return _knowledge_cache
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM knowledge").fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["tags"] = _parse_json_array(d.get("tags", "[]"))
        items.append(d)
    _knowledge_cache = items
    _knowledge_cache_ts = time.time()
    return items


_FTS_MATCH_PUNCT_RE = re.compile(r"[\s，。！？、；：,.!?;:'\"()\[\]{}<>]+")


def _fts_match_knowledge(norm: str, cap: int) -> list[dict]:
    """Candidate retrieval through the real FTS5 index (trigram tokenizer)."""
    match_query = _FTS_MATCH_PUNCT_RE.sub(" ", norm).strip()
    if len(match_query) < 3:
        return []
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT k.* FROM knowledge_fts JOIN knowledge k ON k.rowid = knowledge_fts.rowid "
                "WHERE knowledge_fts MATCH ? ORDER BY bm25(knowledge_fts) LIMIT ?",
                (match_query, cap)
            ).fetchall()
    except sqlite3.Error as e:
        logger.warning("FTS MATCH query failed, falling back to python scorer: %s", e)
        return []
    result = []
    for r in rows:
        d = dict(r)
        d["tags"] = _parse_json_array(d.get("tags", "[]"))
        result.append(d)
    return result


def _knowledge_search_scorer(norm: str, items: list[dict]) -> list[tuple[float, dict]]:
    scored = []
    for item in items:
        haystack = re.sub(r"\s+", "", " ".join([
            item.get("title", ""),
            item.get("category", ""),
            " ".join(item.get("tags", []) or []),
            item.get("content", ""),
        ]).lower())
        if not haystack:
            continue
        # Token match scoring for Chinese text
        q_tokens = set(t for t in re.split(r"[\s,，。！？、；：]+", norm) if len(t) >= 2)
        haystack_tokens = set(t for t in re.split(r"[\s,，。！？、；：]+", haystack) if len(t) >= 2)
        overlap = q_tokens & haystack_tokens
        if overlap:
            score = sum(len(t) for t in overlap) / max(len(norm), 1)
            scored.append((min(score, 1.0), item))
    return scored


def search_knowledge_fts(query: str, limit: int = 10) -> list[dict]:
    norm = re.sub(r"\s+", "", (query or "").lower())
    if not norm:
        return []
    # Retrieve candidates through the real FTS5 MATCH query; fall back to the
    # cached full scan when MATCH is unavailable or misses.
    fts_items = _fts_match_knowledge(norm, limit * 20)
    items = fts_items or get_all_knowledge(use_cache=True)
    scored = _knowledge_search_scorer(norm, items)
    if fts_items:
        # FTS 已用全部 trigram 确认命中, 即使未过旧 chunk 精确打分器也保留,
        # 评分 0.8 以满足调用方 0.5 置信度闸门(main.py fts_quality 路由)。
        scored_ids = {id(item): score for score, item in scored}
        scored.extend((0.8, item) for item in fts_items if id(item) not in scored_ids)
    scored.sort(key=lambda x: x[0], reverse=True)
    # Copy before adding "score": the cache list must not be mutated.
    return [dict(item, score=score) for score, item in scored[:limit]]


def count_knowledge() -> int:
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    return n


# ========== FAQ CRUD ==========

def get_faq(use_cache: bool = True) -> list[dict]:
    global _faq_cache, _faq_cache_ts
    if use_cache and _faq_cache is not None and (time.time() - _faq_cache_ts) < _FAQ_CACHE_TTL:
        return _faq_cache
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM faq ORDER BY usage_count DESC").fetchall()
    result = rows_to_dicts(rows)
    _faq_cache = result
    _faq_cache_ts = time.time()
    return result


def search_faq_fts(query: str, limit: int = 5) -> list[dict]:
    items = get_faq(use_cache=True)
    norm = re.sub(r"\s+", "", (query or "").lower())
    scored = []
    for item in items:
        q = re.sub(r"\s+", "", (item.get("question", "") or "").lower())
        if not q:
            continue
        # Exact substring match (Chinese-friendly)
        if q in norm or norm in q:
            scored.append((len(q), item))
        else:
            # Token overlap scoring
            q_tokens = set(t for t in re.split(r"[\s,，。！？、；：]+", q) if len(t) >= 2)
            norm_tokens = set(t for t in re.split(r"[\s,，。！？、；：]+", norm) if len(t) >= 2)
            overlap = q_tokens & norm_tokens
            if len(overlap) >= max(2, len(q_tokens) // 2):
                scored.append((len(overlap), item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


def search_faq_by_keywords(query: str, limit: int = 5) -> list[dict]:
    items = get_faq(use_cache=True)
    norm = re.sub(r"\s+", "", (query or "").lower())
    qlen = len(norm)
    scored = []
    for item in items:
        try:
            kws = json.loads(item.get("keywords", "[]"))
        except (json.JSONDecodeError, TypeError):
            kws = []
        if not kws:
            continue
        score = 0
        for kw in kws:
            if kw in norm:
                pos = norm.find(kw)
                score += len(kw) + max(0, qlen - pos)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


def add_faq(question: str, answer: str, category: str = "常见问题", keywords: list[str] | None = None) -> dict:
    item_id = str(uuid.uuid4())
    ts = now_str()
    kws = _to_json_array(keywords or [])
    with get_db(write=True) as conn:
        conn.execute(
            "INSERT INTO faq (id, question, answer, category, keywords, usage_count, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (item_id, question, answer, category, kws, ts)
        )
    invalidate_caches()
    return {"id": item_id, "question": question, "answer": answer, "category": category, "keywords": keywords or [], "usage_count": 0, "updated_at": ts}


def update_faq(item_id: str, question: str = None, answer: str = None, category: str = None, keywords: list[str] = None) -> bool:
    with get_db(write=True) as conn:
        existing = conn.execute("SELECT * FROM faq WHERE id=?", (item_id,)).fetchone()
        if not existing:
            return False
        ts = now_str()
        updates = {"question": question, "answer": answer, "category": category, "keywords": _to_json_array(keywords) if keywords is not None else None, "updated_at": ts}
        updates = {k: v for k, v in updates.items() if v is not None}
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE faq SET {set_clause} WHERE id=?", (*updates.values(), item_id))
    invalidate_caches()
    return True


def delete_faq(item_id: str) -> bool:
    with get_db(write=True) as conn:
        cursor = conn.execute("DELETE FROM faq WHERE id=?", (item_id,))
        affected = cursor.rowcount
    invalidate_caches()
    return affected > 0


def increment_faq_usage_by_question(question_text: str):
    with get_db(write=True) as conn:
        rows = conn.execute("SELECT id, question FROM faq").fetchall()
        user_lower = (question_text or "").lower()
        for r in rows:
            faq_tokens = [t for t in r["question"].lower().split() if len(t) > 2]
            matches = sum(1 for t in faq_tokens if t in user_lower)
            if matches >= 2 >= len(faq_tokens) or (matches >= max(2, len(faq_tokens) // 2)):
                conn.execute("UPDATE faq SET usage_count = usage_count + 1 WHERE id=?", (r["id"],))
                break


def increment_faq_usage_by_id(faq_id: str):
    with get_db(write=True) as conn:
        conn.execute("UPDATE faq SET usage_count = usage_count + 1 WHERE id=?", (faq_id,))


# ========== Routes CRUD ==========

def get_routes() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM routes").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["suitableFor"] = _parse_json_array(d.pop("suitable_for", "[]"))
        d["stops"] = _parse_json_array(d.get("stops", "[]"))
        result.append(d)
    return result


def count_routes() -> int:
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
    return n


def seed_routes():
    with get_db(write=True) as conn:
        existing = conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
        if existing > 0:
            return
        routes_data = [
            ("history", "文化深度线", "3.5小时", '["历史文化", "首次到访", "深度讲解"]',
             '[{"name":"灵山广场","highlight":"开场导览与全景认知"},{"name":"灵山大佛","highlight":"地标讲解与文化故事"},{"name":"梵宫","highlight":"建筑艺术与沉浸空间"},{"name":"五印坛城","highlight":"延展性文化讲解"}]',
             "适合喜欢历史、建筑和文化背景的游客，讲解层次更完整。"),
            ("nature", "山水治愈线", "2.5小时", '["自然风光", "轻徒步", "拍照打卡"]',
             '[{"name":"灵山广场","highlight":"景观视角打开"},{"name":"九龙灌浴","highlight":"动态演艺和环境氛围"},{"name":"湖景步道","highlight":"自然风景与慢节奏休憩"},{"name":"观景平台","highlight":"推荐拍照位"}]',
             "更强调风景、演艺与松弛感，适合想轻松逛景区的游客。"),
            ("family", "亲子互动线", "3小时", '["亲子游客", "互动体验", "节奏舒适"]',
             '[{"name":"游客中心","highlight":"行程说明与便利设施提示"},{"name":"九龙灌浴","highlight":"高互动演艺节点"},{"name":"梵宫","highlight":"沉浸式视觉体验"},{"name":"休闲补给区","highlight":"休息与二次分流"}]',
             "减少折返，兼顾演艺、休息和趣味讲解，适合家庭同行。"),
            ("relax", "舒缓漫游线", "2小时", '["轻松游", "银发游客", "低强度行程"]',
             '[{"name":"游客中心","highlight":"低门槛导览启动"},{"name":"灵山大佛","highlight":"核心地标轻讲解"},{"name":"静心休憩区","highlight":"休息与情绪互动"},{"name":"文化商店","highlight":"轻消费与返程建议"}]',
             "适合时间有限或希望舒适游览的游客，路线短、节奏平稳。"),
        ]
        for interest, name, duration, suitable, stops, pitch in routes_data:
            conn.execute(
                "INSERT OR IGNORE INTO routes (id, interest, name, duration, suitable_for, stops, pitch) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (interest, interest, name, duration, suitable, stops, pitch)
            )


# ========== Conversations CRUD ==========

def _parse_conversation_filters(args: Any) -> tuple[dict[str, Any] | None, str | None]:
    period = str(args.get("period", "") or "").strip()
    if period not in {"", "day", "week", "month"}:
        return None, "时间范围无效"

    emotion = str(args.get("emotion", "") or "").strip()
    interest = str(args.get("interest", "") or "").strip()
    satisfaction_raw = str(args.get("satisfaction", "") or "").strip()
    satisfaction = None
    if satisfaction_raw:
        try:
            satisfaction = int(satisfaction_raw)
        except (TypeError, ValueError):
            return None, "评分必须是 1 到 5 的整数"
        if not 1 <= satisfaction <= 5:
            return None, "评分必须是 1 到 5 的整数"

    return {
        "period": period,
        "emotion": emotion,
        "interest": interest,
        "satisfaction": satisfaction,
    }, None


def _conversation_filter_clause(
    period: str = "",
    emotion: str = "",
    interest: str = "",
    satisfaction: int | None = None,
) -> tuple[str, list[Any]]:
    """Build the shared, parameterized conversation WHERE clause."""
    where: list[str] = []
    params: list[Any] = []
    period_days = {"day": 1, "week": 7, "month": 30}
    if period:
        if period not in period_days:
            raise ValueError("invalid period")
        since = (datetime.now() - timedelta(days=period_days[period])).strftime("%Y-%m-%d %H:%M:%S")
        where.append("timestamp >= ?")
        params.append(since)
    if emotion:
        where.append("emotion = ?")
        params.append(emotion)
    if interest:
        where.append("interest = ?")
        params.append(interest)
    if satisfaction is not None:
        where.append("satisfaction = ?")
        params.append(satisfaction)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    return where_sql, params


def add_conversation(session_id: str, user_id: str, message: str, reply: str, emotion: str, interest: str, topics: list[str], latency_ms: int = 0) -> dict:
    item_id = str(uuid.uuid4())
    ts = now_str()
    with get_db(write=True) as conn:
        conn.execute(
            "INSERT INTO conversations (id, session_id, user_id, message, reply, emotion, satisfaction, interest, topics, timestamp, latency_ms) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (item_id, session_id, user_id, message, reply, emotion, interest, _to_json_array(topics), ts, latency_ms)
        )
    return {"id": item_id, "session_id": session_id, "user_id": user_id, "message": message, "reply": reply, "emotion": emotion, "interest": interest, "topics": topics, "timestamp": ts}


def get_conversations(
    page: int = 1,
    page_size: int = 20,
    period: str = "",
    emotion: str = "",
    interest: str = "",
    satisfaction: int | None = None,
) -> dict[str, Any]:
    offset = (page - 1) * page_size
    where_sql, params = _conversation_filter_clause(period, emotion, interest, satisfaction)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM conversations{where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM conversations{where_sql}", params
        ).fetchone()[0]
    items = []
    for r in rows:
        d = dict(r)
        d["topics"] = _parse_json_array(d.get("topics", "[]"))
        items.append(d)
    return {"list": items, "total": total, "page": page, "page_size": page_size}


def get_conversation_analysis_rows(
    period: str = "",
    emotion: str = "",
    interest: str = "",
    satisfaction: int | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    """Return filter-matched analysis data without user/session identifiers."""
    safe_limit = max(1, min(int(limit), 5000))
    where_sql, params = _conversation_filter_clause(period, emotion, interest, satisfaction)
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM conversations{where_sql}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT message, reply, emotion, satisfaction, interest, topics, timestamp "
            f"FROM conversations{where_sql} ORDER BY timestamp DESC LIMIT ?",
            (*params, safe_limit),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["topics"] = _parse_json_array(item.get("topics", "[]"))
        items.append(item)
    return {"list": items, "total": total, "truncated": total > safe_limit}


def get_latest_feedback(limit: int = 20) -> list[dict[str, Any]]:
    """Return the newest rated conversations, filtering before applying the limit."""
    safe_limit = max(1, min(int(limit), 100))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations "
            "WHERE satisfaction IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["topics"] = _parse_json_array(item.get("topics", "[]"))
        items.append(item)
    return items


def get_conversations_by_session(session_id: str, limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE session_id=? ORDER BY timestamp DESC LIMIT ?",
            (session_id, limit)
        ).fetchall()
    return rows_to_dicts(rows)


def get_history_for_llm(session_id: str, limit: int = 6) -> list[dict[str, str]]:
    rows = get_conversations_by_session(session_id, limit)
    result: list[dict[str, str]] = []
    for item in reversed(rows):
        if item.get("message"):
            result.append({"role": "user", "content": item["message"]})
        if item.get("reply"):
            result.append({"role": "assistant", "content": item["reply"]})
    return result[-limit:]


def update_conversation_satisfaction(conv_id: str, satisfaction: int, session_id: str | None = None) -> bool:
    with get_db(write=True) as conn:
        if session_id:
            cursor = conn.execute(
                "UPDATE conversations SET satisfaction=? WHERE id=? AND session_id=?",
                (satisfaction, conv_id, session_id),
            )
        else:
            cursor = conn.execute("UPDATE conversations SET satisfaction=? WHERE id=?", (satisfaction, conv_id))
        affected = cursor.rowcount
    return affected > 0


def count_conversations(since_date: str = None) -> int:
    with get_db() as conn:
        if since_date:
            n = conn.execute("SELECT COUNT(*) FROM conversations WHERE timestamp >= ?", (since_date,)).fetchone()[0]
        else:
            n = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    return n


# ========== Dashboard/Report ==========

def _service_ratio(topic_rows: list[sqlite3.Row]) -> dict[str, int]:
    ticket_keywords = ("门票", "票价", "购票", "售票", "票务", "票", "ticket", "admission")
    guide_keywords = (
        "景点讲解", "景点介绍", "路线推荐", "路线资料", "导览", "导航",
        "讲解", "路线", "guide", "navigation",
    )
    counts = {"consult": 0, "ticket": 0, "guide": 0}

    for row in topic_rows:
        topics = _parse_json_array(row["topics"])
        if not isinstance(topics, list):
            topics = [topics]
        topic_text = " ".join(str(topic).strip().casefold() for topic in topics if topic)
        if any(keyword.casefold() in topic_text for keyword in ticket_keywords):
            category = "ticket"
        elif any(keyword.casefold() in topic_text for keyword in guide_keywords):
            category = "guide"
        else:
            category = "consult"
        counts[category] += 1

    total = sum(counts.values())
    if not total:
        return counts

    ratio = {category: round(count / total * 100) for category, count in counts.items()}
    ratio[max(counts, key=counts.get)] += 100 - sum(ratio.values())
    return ratio


def compute_dashboard() -> dict[str, Any]:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    visitor_key_sql = (
        "CASE WHEN TRIM(COALESCE(user_id, '')) <> '' "
        "AND LOWER(TRIM(user_id)) <> 'guest' THEN TRIM(user_id) "
        "ELSE TRIM(COALESCE(session_id, '')) END"
    )

    with get_db() as conn:
        today_visitors = conn.execute(
            f"SELECT COUNT(DISTINCT {visitor_key_sql}) FROM conversations "
            f"WHERE timestamp >= ? AND timestamp < ? AND ({visitor_key_sql}) <> ''",
            (today, tomorrow),
        ).fetchone()[0]
        week_visitors = conn.execute(
            f"SELECT COUNT(DISTINCT {visitor_key_sql}) FROM conversations "
            f"WHERE timestamp >= ? AND timestamp < ? AND ({visitor_key_sql}) <> ''",
            (week_start, tomorrow),
        ).fetchone()[0]
        today_conversations = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE timestamp >= ? AND timestamp < ?",
            (today, tomorrow),
        ).fetchone()[0]
        week_conversations = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE timestamp >= ? AND timestamp < ?",
            (week_start, tomorrow),
        ).fetchone()[0]
        total_chats = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        route_count = conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
        service_rows = conn.execute("SELECT topics FROM conversations").fetchall()
        service_ratio = _service_ratio(service_rows)

        avg_row = conn.execute("SELECT AVG(satisfaction) FROM conversations WHERE satisfaction IS NOT NULL").fetchone()[0]
        avg_satisfaction = round(avg_row, 1) if avg_row is not None else None

        trend_rows = conn.execute(
            "SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS cnt, AVG(satisfaction) AS avg_sat "
            "FROM conversations WHERE timestamp >= ? AND timestamp < ? GROUP BY day",
            (week_start, tomorrow)
        ).fetchall()
        trend_map = {r["day"]: (r["cnt"], r["avg_sat"]) for r in trend_rows}

        hot_questions_rows = conn.execute("SELECT question, usage_count FROM faq ORDER BY usage_count DESC LIMIT 5").fetchall()
        hot_questions = [{"question": r["question"], "count": r["usage_count"]} for r in hot_questions_rows]

        emotion_rows = conn.execute("SELECT emotion, COUNT(*) as cnt FROM conversations GROUP BY emotion").fetchall()
        emotion_counter = {r["emotion"]: r["cnt"] for r in emotion_rows}

        topic_rows = conn.execute(
            "SELECT j.value AS topic, COUNT(*) AS cnt FROM conversations, json_each(topics) j "
            "WHERE topics IS NOT NULL AND json_valid(topics) AND j.value != '' "
            "GROUP BY j.value ORDER BY cnt DESC, j.value LIMIT 6"
        ).fetchall()
        topic_counter = {r["topic"]: r["cnt"] for r in topic_rows}

        latency_avg = conn.execute("SELECT AVG(latency_ms) FROM conversations WHERE latency_ms > 0").fetchone()[0]
        avg_latency = round(latency_avg, 0) if latency_avg else 0

        sat_count = conn.execute("SELECT COUNT(*) FROM conversations WHERE satisfaction IS NOT NULL").fetchone()[0]
        satisfaction_rate = round(avg_satisfaction / 5 * 100, 0) if sat_count > 0 and avg_satisfaction is not None else 0

    trend = []
    for offset in range(6, -1, -1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        count, avg_sat = trend_map.get(day, (0, None))
        daily_avg = round(avg_sat, 1) if avg_sat else None
        trend.append({"day": day[5:], "count": count, "avgSatisfaction": daily_avg})

    return {
        "todayVisitors": today_visitors,
        "weekVisitors": week_visitors,
        "todayConversations": today_conversations,
        "weekConversations": week_conversations,
        "serviceRatio": service_ratio,
        "avgSatisfaction": avg_satisfaction,
        "totalChats": total_chats,
        "knowledgeCount": knowledge_count,
        "routeCount": route_count,
        "satisfactionRate": satisfaction_rate,
        "avgLatency": avg_latency,
        "trend": trend,
        "hotQuestions": hot_questions,
        "topicFocus": [{"name": name, "value": value} for name, value in sorted(topic_counter.items(), key=lambda x: (x[1], x[0]), reverse=True)[:6]],
        "sentiment": {
            "positive": emotion_counter.get("delighted", 0) + emotion_counter.get("warm", 0) + emotion_counter.get("joy", 0),
            "neutral": emotion_counter.get("focused", 0) + emotion_counter.get("neutral", 0),
            "negative": emotion_counter.get("caring", 0) + emotion_counter.get("sad", 0),
        },
        "lastUpdate": now_str(),
    }


def compute_report() -> dict[str, Any]:
    now = datetime.now()
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        recent_rows = conn.execute(
            "SELECT topics, interest FROM conversations WHERE timestamp >= ?", (week_ago,)
        ).fetchall()
        total = len(recent_rows)

        topic_counter: dict[str, int] = {}
        interest_counter: dict[str, int] = {}
        for r in recent_rows:
            for t in _parse_json_array(r["topics"], []):
                topic_counter[t] = topic_counter.get(t, 0) + 1
            interest_counter[r["interest"] or "history"] = interest_counter.get(r["interest"] or "history", 0) + 1

        avg_row = conn.execute("SELECT AVG(satisfaction) FROM conversations WHERE satisfaction IS NOT NULL AND timestamp >= ?", (week_ago,)).fetchone()[0]
        avg_sat = round(avg_row, 1) if avg_row else None

        peak_row = conn.execute("SELECT strftime('%H', timestamp) as hour, COUNT(*) as cnt FROM conversations WHERE timestamp >= ? GROUP BY hour ORDER BY cnt DESC LIMIT 1", (week_ago,)).fetchone()
        service_peak = f"{peak_row['hour']}:00-{int(peak_row['hour'])+1}:00" if peak_row else None

        latency_avg = conn.execute("SELECT AVG(latency_ms) FROM conversations WHERE latency_ms > 0 AND timestamp >= ?", (week_ago,)).fetchone()[0]
        avg_resp_ms = round(latency_avg, 0) if latency_avg else None
        response_target = f"< {avg_resp_ms} ms" if avg_resp_ms else None

    return {
        "period": f"{week_ago[:10]} 至 {now.strftime('%Y-%m-%d')}",
        "summary": {
            "totalConversations": total,
            "avgSatisfaction": avg_sat,
            "servicePeak": service_peak,
            "responseTarget": response_target,
        },
        "topicFocus": [{"name": name, "value": value} for name, value in sorted(topic_counter.items(), key=lambda x: x[1], reverse=True)[:5]],
        "interestDistribution": [{"name": name, "value": value} for name, value in interest_counter.items()],
        "suggestions": [
            "高峰期可优先推荐亲子轻松线，减少主干道拥堵。",
            "梵宫与灵山大佛关注度高，建议继续扩充深度讲解词。",
            "服务设施咨询占比稳定，适合接入实时位置提示与离线导览方案。",
        ],
    }


# ========== Settings CRUD ==========

def get_settings() -> dict[str, str]:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def update_settings(updates: dict[str, str]):
    with get_db(write=True) as conn:
        for key, value in updates.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


# ========== Admin sessions ==========

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_admin_session(token: str, username: str, expires_at: int):
    with get_db(write=True) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_sessions (token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), username, expires_at, now_str()),
        )
        conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (int(time.time()),))


def get_admin_session(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, expires_at FROM admin_sessions WHERE token_hash=? AND expires_at > ?",
            (_token_hash(token), int(time.time())),
        ).fetchone()
    return row_to_dict(row)


def refresh_admin_session(token: str, expires_at: int):
    with get_db(write=True) as conn:
        conn.execute("UPDATE admin_sessions SET expires_at=? WHERE token_hash=?", (expires_at, _token_hash(token)))


def delete_admin_session(token: str):
    with get_db(write=True) as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token_hash=?", (_token_hash(token),))


def revoke_admin_sessions(username: str | None = None):
    with get_db(write=True) as conn:
        if username:
            conn.execute("DELETE FROM admin_sessions WHERE username=?", (username,))
        else:
            conn.execute("DELETE FROM admin_sessions")


def get_setting(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# ========== Admin Operation Logs ==========

def add_operation_log(admin_user: str, action: str, resource: str, resource_id: str = "", detail: str = "", ip_address: str = "", result: str = "success"):
    item_id = str(uuid.uuid4())
    ts = now_str()
    with get_db(write=True) as conn:
        conn.execute(
            "INSERT INTO admin_operation_logs (id, admin_user, action, resource, resource_id, detail, ip_address, result, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, admin_user, action, resource, resource_id, detail, ip_address, result, ts)
        )


def get_operation_logs(page: int = 1, page_size: int = 20, action: str = "", resource: str = "") -> dict[str, Any]:
    offset = (page - 1) * page_size
    conditions = []
    params = []
    if action:
        conditions.append("action=?")
        params.append(action)
    if resource:
        conditions.append("resource=?")
        params.append(resource)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM admin_operation_logs{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (*params, page_size, offset)
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM admin_operation_logs{where}", params
        ).fetchone()[0]
    return {"list": rows_to_dicts(rows), "total": total, "page": page, "page_size": page_size}


# ========== Avatar Config CRUD ==========

def get_avatar_config() -> dict[str, Any]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM avatar_config WHERE id='default'").fetchone()
    if not row:
        return {
            "theme": "灵山文化",
            "activeProfile": "gentle_guide",
            "vrmModel": "",
            "profiles": [
                {"id": "gentle_guide", "name": "小灵", "style": "亲和讲解员", "voice": "温柔女声", "outfit": "新中式导览服", "expressionBias": "warm"},
                {"id": "scholar_host", "name": "灵山学者", "style": "文化讲述型", "voice": "沉稳男声", "outfit": "文化学者服", "expressionBias": "calm"},
            ],
        }
    d = dict(row)
    d["profiles"] = _parse_json_array(d.get("profiles", "[]"))
    return d


def update_avatar_config(data: dict[str, Any]):
    with get_db(write=True) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO avatar_config (id, theme, active_profile, profiles, vrm_model) VALUES ('default', ?, ?, ?, ?)",
            (data.get("theme", ""), data.get("activeProfile", "") or data.get("active_profile", "gentle_guide"), _to_json_array(data.get("profiles", [])), data.get("vrmModel", "") or data.get("vrm_model", ""))
        )


def get_avatar_public_config(base_dir: Path, model_id: str | None = None) -> dict[str, Any]:
    config = get_avatar_config()
    profiles = config.get("profiles", [])
    active_id = config.get("activeProfile", "") or config.get("active_profile", "")
    active = next((p for p in profiles if p["id"] == active_id), profiles[0] if profiles else {})
    models = get_vrm_models(base_dir)
    if model_id is not None and str(model_id).strip():
        selected = get_vrm_model(str(model_id).strip(), base_dir, enabled_only=True)
    else:
        selected = next((model for model in models if model.get("enabled")), None)
    if not selected:
        raise ValueError("当前没有可用的启用模型")
    return {
        "name": active.get("name", "小灵"),
        "style": selected.get("style", ""),
        "voice": selected.get("voice", ""),
        "outfit": selected.get("outfit", ""),
        "expressionBias": selected.get("expressionBias", "warm"),
        "theme": config.get("theme", "灵山文化"),
        "modelId": selected["name"],
        "vrmModel": selected["name"],
    }


def get_vrm_models(base_dir: Path) -> list[dict[str, Any]]:
    files = sorted(base_dir.glob("*.vrm"), key=lambda p: p.stat().st_mtime, reverse=True)
    presets = {p["model_name"]: p for p in get_guide_presets()}
    result = []
    for f in files:
        info = {"name": f.name, "path": f"/{f.name}", "size": f.stat().st_size}
        preset = presets.get(f.name)
        info["enabled"] = bool(preset.get("enabled", 1)) if preset else True
        if preset:
            info["voice"] = preset.get("voice", "")
            info["outfit"] = preset.get("outfit", "")
            info["style"] = preset.get("style", "")
            info["expressionBias"] = preset.get("expression_bias", "warm")
        else:
            info["voice"] = ""
            info["outfit"] = ""
            info["style"] = ""
            info["expressionBias"] = "warm"
        result.append(info)
    return result


def _ensure_guide_presets_enabled_column() -> None:
    """Make model-status writes safe for databases created before this field."""
    with get_db(write=True) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(guide_presets)").fetchall()}
        if "enabled" not in columns:
            conn.execute("ALTER TABLE guide_presets ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")


def get_vrm_model(model_name: str, base_dir: Path, enabled_only: bool = False) -> dict[str, Any]:
    """Return one safe model record, rejecting missing or disabled models."""
    safe_name = Path(str(model_name or "")).name
    if safe_name != str(model_name or "").strip() or not safe_name.lower().endswith(".vrm"):
        raise ValueError("modelId 无效")
    model = next((item for item in get_vrm_models(base_dir) if item["name"] == safe_name), None)
    if not model:
        raise ValueError("modelId 无效")
    if enabled_only and not model.get("enabled"):
        raise ValueError("modelId 对应的模型已禁用")
    return model


def set_vrm_model_enabled(model_name: str, enabled: bool, base_dir: Path) -> dict[str, Any]:
    """Persist model availability while ensuring one enabled model remains."""
    _ensure_guide_presets_enabled_column()
    model = get_vrm_model(model_name, base_dir)
    enabled = bool(enabled)
    with get_db(write=True) as conn:
        file_names = [path.name for path in base_dir.glob("*.vrm")]
        rows = conn.execute(
            "SELECT model_name, enabled FROM guide_presets WHERE model_name IN ({})".format(",".join("?" for _ in file_names)),
            file_names,
        ).fetchall() if file_names else []
        enabled_by_name = {row["model_name"]: bool(row["enabled"]) for row in rows}
        current_enabled = enabled_by_name.get(model["name"], True)
        enabled_count = sum(1 for name in file_names if enabled_by_name.get(name, True))
        if not enabled and current_enabled and enabled_count <= 1:
            raise ValueError("至少需要保留一个启用模型")

        ts = now_str()
        existing = conn.execute("SELECT id FROM guide_presets WHERE model_name=?", (model["name"],)).fetchone()
        if existing:
            conn.execute("UPDATE guide_presets SET enabled=?, updated_at=? WHERE model_name=?", (int(enabled), ts, model["name"]))
        else:
            conn.execute(
                "INSERT INTO guide_presets (id, model_name, enabled, updated_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), model["name"], int(enabled), ts),
            )
    return get_vrm_model(model["name"], base_dir)


def set_vrm_models_enabled(enabled: bool, base_dir: Path) -> list[dict[str, Any]]:
    """Enable every installed model in one transaction.

    Bulk disabling is intentionally rejected so the public selector can never
    end up without an available model.
    """
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值")
    if not enabled:
        raise ValueError("批量禁用不可用，至少需要保留一个启用模型")

    _ensure_guide_presets_enabled_column()
    file_names = sorted(path.name for path in base_dir.glob("*.vrm"))
    if not file_names:
        raise ValueError("当前没有可管理的 VRM 模型")

    with get_db(write=True) as conn:
        ts = now_str()
        for model_name in file_names:
            existing = conn.execute("SELECT id FROM guide_presets WHERE model_name=?", (model_name,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE guide_presets SET enabled=1, updated_at=? WHERE model_name=?",
                    (ts, model_name),
                )
            else:
                conn.execute(
                    "INSERT INTO guide_presets (id, model_name, enabled, updated_at) VALUES (?, ?, 1, ?)",
                    (str(uuid.uuid4()), model_name, ts),
                )
    return get_vrm_models(base_dir)


# ========== Guide Presets CRUD ==========

def get_guide_presets() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM guide_presets ORDER BY updated_at DESC").fetchall()
    return rows_to_dicts(rows)


def get_guide_preset(model_name: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM guide_presets WHERE model_name=?", (model_name,)).fetchone()
    return row_to_dict(row)


def upsert_guide_preset(model_name: str, voice: str, outfit: str, style: str = "", expression_bias: str = "warm") -> dict:
    item_id = str(uuid.uuid4())
    ts = now_str()
    with get_db(write=True) as conn:
        existing = conn.execute("SELECT id FROM guide_presets WHERE model_name=?", (model_name,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE guide_presets SET voice=?, outfit=?, style=?, expression_bias=?, updated_at=? WHERE model_name=?",
                (voice, outfit, style, expression_bias, ts, model_name)
            )
        else:
            conn.execute(
                "INSERT INTO guide_presets (id, model_name, voice, outfit, style, expression_bias, enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (item_id, model_name, voice, outfit, style, expression_bias, ts)
            )
    return {"model_name": model_name, "voice": voice, "outfit": outfit, "style": style, "expressionBias": expression_bias}


def delete_guide_preset(model_name: str) -> bool:
    with get_db(write=True) as conn:
        cursor = conn.execute("DELETE FROM guide_presets WHERE model_name=?", (model_name,))
        affected = cursor.rowcount
    return affected > 0


def seed_guide_presets():
    with get_db(write=True) as conn:
        presets = [
            ("景.vrm", "温柔女声", "新中式导览服", "亲和讲解员", "warm"),
            ("区.vrm", "温柔女声", "现代休闲装", "活泼互动型", "delighted"),
            ("灵.vrm", "温柔女声", "传统汉服", "知识型讲解", "calm"),
            ("山.vrm", "温柔女声", "户外登山服", "文雅讲解", "warm"),
        ]
        ts = now_str()
        for model, voice, outfit, style, expr in presets:
            existing = conn.execute("SELECT 1 FROM guide_presets WHERE model_name=?", (model,)).fetchone()
            if existing:
                conn.execute("UPDATE guide_presets SET voice=? WHERE model_name=?", (voice, model))
            else:
                conn.execute(
                    "INSERT INTO guide_presets (id, model_name, voice, outfit, style, expression_bias, enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                    (str(uuid.uuid4()), model, voice, outfit, style, expr, ts)
                )


# ========== Seed data ==========

def seed_data():
    init_db()
    seed_routes()
    seed_guide_presets()

    # Seed knowledge from knowledge_expand.py (single source of truth for knowledge table)
    if count_knowledge() < 5:
        try:
            from knowledge_expand import EXTRA_FACTS
            existing_sources = {r["source"] for r in get_all_knowledge()}
            for item in EXTRA_FACTS:
                if item["source"] not in existing_sources:
                    add_knowledge(
                        title=item["title"],
                        category=item["category"],
                        tags=item["tags"],
                        content=item["content"],
                        source=item["source"],
                    )
        except ImportError:
            logger.warning("knowledge_expand.EXTRA_FACTS unavailable, knowledge seeding skipped")

    # Seed FAQ table — includes both natural questions AND keyword-driven rules (replaces old match_strict_fact)
    faq_items = [
            # Original FAQ items with keyword metadata
            ("景区有什么必看景点？", "推荐优先游览灵山大佛、九龙灌浴和梵宫，这三类点位能分别覆盖地标打卡、演艺互动和文化沉浸体验。", "景点讲解",
             ["必看", "推荐景点", "值得看", "看什么", "玩什么"]),
            ("灵山大佛有多高？", "灵山大佛高88米，是目前世界上最高的露天青铜立佛，相当于27层楼高！", "景点讲解",
             ["多高", "高度", "大佛高"]),
            ("九龙灌浴表演时间？", "九龙灌浴是经典表演，每天多场，展示佛祖释迦牟尼诞生的盛况，非常震撼。具体场次时间以景区当日公告为准。", "景点讲解",
             ["九龙灌浴", "表演时间", "演出时间", "九龙灌浴时间"]),
            ("梵宫是什么？", "灵山梵宫是中国最大的仿唐式建筑，集艺术殿堂、会议场所、表演舞台于一体，非常壮观！", "景点讲解",
             ["梵宫", "梵宫是什么", "梵宫介绍", "梵宫什么样"]),
            ("历史文化爱好者怎么逛？", "建议选择文化深度线，从灵山广场到灵山大佛，再到梵宫和五印坛城，整体节奏更适合深度游。", "路线推荐",
             ["历史文化", "深度游", "文化"]),
            ("亲子游客适合怎么玩？", "建议优先选择互动体验更强的亲子互动线，兼顾演艺、打卡和休憩补给点，避免行程过长。", "路线推荐",
             ["亲子", "小孩", "孩子", "带娃", "儿童"]),
            ("游览路线推荐？", "建议游览路线：大佛广场 → 九龙灌浴 → 祥符禅寺 → 灵山大佛 → 梵宫 → 五印坛城。全程约3-4小时。", "游览路线",
             ["路线推荐", "怎么逛", "怎么玩", "游览路线", "安排"]),
            ("游览一遍需要多久？", "建议游玩3-4小时，可以走完主要景点。如果想深度游览可以安排半天时间。", "游览路线",
             ["多久", "时间", "几个小时"]),
            # Consolidated ticket info — replaces 3 duplicate FAQ items
            ("门票多少钱？", "灵山胜境门票价格：成人票210元/人，优惠票（老人、学生）105元/人，1.4米以下儿童免费。梵宫《灵山吉祥颂》演出票需另购。", "服务信息",
             ["门票", "票价", "价格", "收费", "多少钱", "成人票", "门票价格", "优惠票"]),
            ("景区开放时间？", "景区开放时间为每日7:30-17:30，17:00停止入园。梵宫演出时间为10:00、11:30、14:00、16:00，具体以景区当日公告为准。", "服务信息",
             ["开放时间", "几点开门", "几点关门", "营业时间", "开园", "闭园", "入园时间"]),
            ("景区在哪里？", "江苏省无锡市滨湖区马山镇灵山路1号。", "服务信息",
             ["景区地址", "景区在哪", "景区位置", "怎么去景区", "在哪", "在哪里", "地址"]),
            ("停车场在哪里？", "景区设有大型停车场，位于东门和北门。小车10元/次，大车20元/次。节假日建议尽早到达。", "设施服务",
             ["停车", "停车场", "停车费", "车停哪", "怎么停车"]),
            ("卫生间位置？", "景区内设有多个卫生间，主要分布在大佛广场、梵宫、五印坛城附近，按照现场指示牌即可找到。", "设施服务",
             ["卫生间", "厕所", "洗手间"]),
            ("景区有餐厅吗？", "景区内设有素斋餐厅和休闲茶座，梵宫负一层提供精致素斋套餐，人均约50-80元。", "设施服务",
             ["餐饮", "美食", "吃什么", "吃饭", "素斋", "餐厅"]),
            # === Migrated from match_strict_fact() ===
            ("门票包含哪些项目？", "灵山胜境门票为景区大门票，包含入园和各景点游览。梵宫《灵山吉祥颂》演出票需另购，不包含在大门票内。九龙灌浴表演凭大门票免费观看。", "服务信息",
             ["门票包含", "门票包括", "门票含", "包含项目", "含什么"]),
            ("儿童票政策是什么？", "灵山胜境1.4米以下儿童免费入园，无需购票。超过1.4米的儿童需购买成人票210元/人。建议带好儿童身份证明，以景区现场身高测量为准。", "服务信息",
             ["儿童票", "小孩票", "儿童免费", "小孩免费", "小孩收费", "儿童收费", "小朋友票"]),
            ("老人有什么优惠？", "60岁以上老人凭身份证可购买优惠票105元/人，约为成人票半价。学生凭学生证也可享受同价优惠票。购买时需在景区售票窗口出示有效证件。", "服务信息",
             ["老人优惠", "老人票", "老人半价", "老人免费", "老年人优惠", "老年人票", "老人票价"]),
            ("景区有轮椅租赁吗？", "景区游客中心提供轮椅免费租赁服务（需缴纳押金），主要道路无障碍通行，轮椅可以到达大部分景点。", "设施服务",
             ["轮椅", "轮椅租赁", "轮椅服务", "残疾人"]),
            ("景区注意事项有哪些？", "游览灵山胜境建议穿舒适的运动鞋，做好防晒。景区内禁止吸烟和使用明火，进入寺庙请保持安静。建议游玩时间3-4小时，尽量避开节假日高峰期。", "服务信息",
             ["注意事项", "注意什么", "有什么注意", "景区禁忌", "游览注意"]),
            ("景区特色活动有哪些？", "灵山胜境特色活动包括：九龙灌浴大型音乐喷泉表演、梵宫《吉祥颂》演出、抄经体验、禅茶品鉴等。其中九龙灌浴和《吉祥颂》是必看的核心演出项目。", "景点讲解",
             ["特色活动", "有什么活动", "有什么好玩", "体验项目", "互动体验", "好玩的项目"]),
            ("景区适合老人游玩吗？", "灵山胜境比较适合老人游览。景区内有轮椅免费租赁、无障碍通道，核心景点之间距离适中，可以慢慢走。建议走文化路线，经大佛广场、灵山大佛、梵宫等，全程约3小时，比较舒缓。", "路线推荐",
             ["适合老人", "老人游玩", "老人游览", "老人适合"]),
            ("宠物可以带进景区吗？", "景区明确规定禁止携带宠物入园。如果您携带了宠物，建议提前咨询景区游客中心是否有寄养服务，或者将宠物安置好再前来游览，以免影响您的游览计划。", "服务信息",
             ["宠物", "带宠物", "能带宠物", "可以带狗", "可以带猫"]),
            ("梵宫建筑有什么特色？", "灵山梵宫是中国最大的仿唐式建筑群，集艺术殿堂、会议中心、表演舞台于一体。外观气势恢宏，内部汇集了木雕、石雕、铜雕、油画、琉璃等多种艺术形式，非常壮观。", "景点讲解",
             ["梵宫建筑", "梵宫什么样", "梵宫特色"]),
            ("九龙灌浴是什么？", "九龙灌浴是灵山胜境的经典大型音乐喷泉表演，位于景区核心位置。表演展示了佛祖释迦牟尼诞生时的盛况——九龙吐水灌浴太子。表演配合音乐、喷泉和动态雕塑，视觉效果非常震撼，是游客必看的演艺项目之一。", "景点讲解",
             ["九龙灌浴是什么", "九龙灌浴介绍", "讲讲九龙灌浴"]),
            ("祥符禅寺历史背景？", "祥符禅寺是一座千年古寺，始建于唐代，历经多次修缮，是江南地区重要的佛教寺院之一。寺内保存有众多珍贵文物和佛教艺术品，历史悠久，文化底蕴深厚。", "景点讲解",
             ["祥符禅寺", "祥符禅寺历史", "祥符禅寺介绍"]),
            ("五印坛城展示什么文化？", "五印坛城主要展示的是藏传佛教文化，是一座以'五方五佛'和中国四大佛山文化为主题，集佛教发展史、文化展示、艺术鉴赏和互动体验于一体的殿堂，非常适合游客参观。", "景点讲解",
             ["五印坛城", "五印坛城文化", "坛城文化"]),
            ("大佛广场有什么特色？", "大佛广场是拍摄灵山大佛全景的最佳位置，位于大佛正前方，视野开阔，可容纳大量游客驻足观瞻。广场上还可欣赏到九龙灌浴表演，是景区必到的打卡点。", "景点讲解",
             ["大佛广场", "广场特色", "广场有什么"]),
            ("景区有演出吗？", "梵宫《灵山吉祥颂》演出时间为每天10:00、11:30、14:00、16:00共四场，每场约20分钟。九龙灌浴表演每天多场，具体时间以景区当日公告为准。", "景点讲解",
             ["演出时间", "表演时间", "演出几点", "表演几点", "演出场次", "梵宫演出", "吉祥颂"]),
            ("景区交通怎么走？", "灵山胜境位于江苏省无锡市滨湖区马山镇灵山路1号。自驾可走沪宁高速转马山出口；公交可坐K1、88、89路；地铁一号线到终点站转公交。", "服务信息",
             ["交通", "怎么去", "怎么走", "公交", "地铁", "自驾"]),
            ("有无障碍设施吗？", "景区主要道路无障碍通行，轮椅可以到达大部分景点。游客中心提供轮椅免费租赁服务（需押金）。卫生间设有无障碍专用间。如有特殊需求，可以联系游客中心工作人员获得帮助。", "设施服务",
             ["无障碍", "轮椅服务"]),
        ]
    existing_faqs = get_faq()
    existing_qs = {f["question"] for f in existing_faqs}
    for q, a, c, kws in faq_items:
        if q in existing_qs:
            with get_db(write=True) as conn:
                row = conn.execute("SELECT keywords FROM faq WHERE question=?", (q,)).fetchone()
                existing_kw = json.loads(row[0]) if row and row[0] else []
                if not existing_kw:
                    conn.execute("UPDATE faq SET keywords=?, updated_at=? WHERE question=?", (_to_json_array(kws), now_str(), q))
        else:
            add_faq(q, a, c, keywords=kws)
    invalidate_caches()

    settings = get_settings()
    if not settings:
        defaults = {
            "aiModel": "siliconflow",
            "knowledgeMode": "本地景区知识库 + Chroma向量RAG + 资料包自动导入",
            "responseTargetMs": "3200",
            "ttsEnabled": "True",
            "emotionEngine": "LLM细粒度情绪推断 + 多模态标签引擎",
            "asrMode": "SiliconFlow /audio/transcriptions",
            "adminUser": "admin",
        }
        if os.environ.get("APP_ENV", "").strip().lower() in {"development", "dev", "local", "test"}:
            from werkzeug.security import generate_password_hash
            # 默认口令 admin123 只以哈希形式落库, 且仅限显式开发/测试环境
            defaults["admin_password"] = generate_password_hash("admin123")
        update_settings(defaults)

    avatar = get_avatar_config()
    if not avatar.get("vrmModel") and not avatar.get("vrm_model"):
        base_dir = MODEL_DIR
        vrm_files = list(base_dir.glob("*.vrm"))
        if vrm_files:
            first_vrm = vrm_files[0].name
            avatar["vrmModel"] = first_vrm
            update_avatar_config(avatar)
