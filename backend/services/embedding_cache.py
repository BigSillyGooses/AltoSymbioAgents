"""
services/embedding_cache.py — Content-hash keyed cache in front of the embedder.

Perf Phase 2. Re-embedding the same text is the single most expensive
retrieval operation per turn (the ``embed`` span in
``semantic_search.search_documents_hybrid``), and the inputs — queries,
document chunks, trajectory task texts — repeat heavily within a session.
This module memoizes ``semantic_search._embed`` behind a two-tier cache:

  1. in-memory LRU (``collections.OrderedDict``, capped at the
     ``embedding_cache_memory_items`` setting, default 512), and
  2. the ``embedding_cache`` SQLite table (vector stored as a packed
     float32 BLOB via ``array('f')`` — the same float32 layout
     ``sqlite_vec.serialize_float32`` writes into the vec0 tables, but
     symmetric: unlike the sqlite-vec helper we must also DEserialize, so
     the stdlib ``array`` round-trip is used instead).

Keys are ``sha256(model_key() + "\\x00" + text)`` so vectors from different
embedding models can never collide; ``model_key()`` mirrors the default
fastembed model id in ``semantic_search.init_vector_store``.

Behavioral contract:
  - ``get_or_embed`` preserves input order and calls the real embedder for
    ONLY the texts missing from both tiers (one batch call).
  - Every DB operation is wrapped in try/except: a cache failure degrades
    to calling the real embedder, never breaks an embed.
  - Import-safe: nothing here touches the database (or semantic_search —
    imported lazily to avoid a module-load cycle) at import time.
  - Gated behind the ``embedding_cache_enabled`` setting (default off).
    Callers check ``is_enabled()`` and route through ``get_or_embed`` only
    when true, so flag-off turns are byte-identical to the legacy path.

Precision note: the DB tier stores float32 (like the vec0 tables), so a
vector served from SQLite may differ from a fresh embed below ~1e-7 per
component when the embedder emits float64. The in-memory tier returns the
exact original vector. This is the same precision the vector index itself
searches at.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from array import array
from collections import OrderedDict
from datetime import datetime, timezone

import db

log = logging.getLogger("alto.embedding_cache")

# Default fastembed model id — mirrors semantic_search.init_vector_store.
# Tests monkeypatch ``model_key`` (or this constant) to isolate entries.
_MODEL_KEY = "BAAI/bge-small-en-v1.5"

_DEFAULT_MEMORY_ITEMS = 512
_DEFAULT_MAX_ROWS = 50_000

# In-memory LRU tier: content_hash -> list[float]. Guarded by _mem_lock
# because embeds happen both on chat threads and the background indexer.
_mem_cache: OrderedDict[str, list[float]] = OrderedDict()
_mem_lock = threading.Lock()

# Live core.settings.Settings handle (or any object with .get), attached at
# API init via attach_settings — same idiom as input_sanitizer. None means
# "not wired" (unit tests, early startup): the cache stays disabled.
_settings_obj = None


def attach_settings(settings) -> None:
    """Give this module the Settings object so the cache flags are live."""
    global _settings_obj
    _settings_obj = settings


def is_enabled() -> bool:
    """True only when settings are attached and the flag is on (default off)."""
    if _settings_obj is None:
        return False
    try:
        return bool(_settings_obj.get("embedding_cache_enabled", False))
    except Exception:
        return False


def model_key() -> str:
    """Identifier of the embedding model the cached vectors belong to."""
    return _MODEL_KEY


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(text: str) -> str:
    payload = (model_key() + "\x00" + (text or "")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _memory_capacity() -> int:
    if _settings_obj is None:
        return _DEFAULT_MEMORY_ITEMS
    try:
        return max(1, int(_settings_obj.get(
            "embedding_cache_memory_items", _DEFAULT_MEMORY_ITEMS,
        ) or _DEFAULT_MEMORY_ITEMS))
    except Exception:
        return _DEFAULT_MEMORY_ITEMS


def _max_rows() -> int:
    if _settings_obj is None:
        return _DEFAULT_MAX_ROWS
    try:
        return max(1, int(_settings_obj.get(
            "embedding_cache_max_rows", _DEFAULT_MAX_ROWS,
        ) or _DEFAULT_MAX_ROWS))
    except Exception:
        return _DEFAULT_MAX_ROWS


def clear_memory_tier() -> None:
    """Drop the in-memory LRU (tests use this to exercise the DB tier)."""
    with _mem_lock:
        _mem_cache.clear()


# ── Vector (de)serialization — symmetric float32 packing ─────────────────────

def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes, dim: int) -> list[float] | None:
    try:
        a = array("f")
        a.frombytes(blob)
    except (ValueError, TypeError):
        return None
    if len(a) != dim:
        return None
    return list(a)


# ── Tier helpers ──────────────────────────────────────────────────────────────

def _mem_get(key: str) -> list[float] | None:
    with _mem_lock:
        vec = _mem_cache.get(key)
        if vec is not None:
            _mem_cache.move_to_end(key)
        return vec


def _mem_put(key: str, vec: list[float]) -> None:
    capacity = _memory_capacity()
    with _mem_lock:
        _mem_cache[key] = vec
        _mem_cache.move_to_end(key)
        while len(_mem_cache) > capacity:
            _mem_cache.popitem(last=False)


def _db_get(key: str) -> list[float] | None:
    """DB-tier lookup. Touches last_used best-effort; failure == miss."""
    try:
        row = db.fetchone(
            "SELECT vector, dim FROM embedding_cache "
            "WHERE content_hash = ? AND model = ?",
            (key, model_key()),
        )
    except Exception as exc:
        log.debug("embedding_cache read failed: %s", exc)
        return None
    if not row:
        return None
    vec = _unpack(row["vector"], row["dim"])
    if vec is None:
        return None
    try:
        db.execute(
            "UPDATE embedding_cache SET last_used = ? WHERE content_hash = ?",
            (_now(), key),
        )
        db.commit()
    except Exception:
        pass  # best-effort recency bump
    return vec


def _db_put(key: str, vec: list[float]) -> None:
    try:
        now = _now()
        db.execute(
            """INSERT INTO embedding_cache
               (content_hash, model, vector, dim, created_at, last_used)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash) DO UPDATE SET
                   model=excluded.model, vector=excluded.vector,
                   dim=excluded.dim, last_used=excluded.last_used""",
            (key, model_key(), _pack(vec), len(vec), now, now),
        )
        db.commit()
    except Exception as exc:
        log.debug("embedding_cache write failed: %s", exc)


def _prune() -> None:
    """Delete oldest rows by last_used once the table exceeds the cap."""
    try:
        row = db.fetchone("SELECT COUNT(*) AS cnt FROM embedding_cache")
        count = row["cnt"] if row else 0
        excess = count - _max_rows()
        if excess <= 0:
            return
        db.execute(
            """DELETE FROM embedding_cache WHERE content_hash IN (
                   SELECT content_hash FROM embedding_cache
                   ORDER BY COALESCE(last_used, created_at) ASC LIMIT ?
               )""",
            (excess,),
        )
        db.commit()
    except Exception as exc:
        log.debug("embedding_cache prune failed: %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def get_or_embed(texts: list[str]) -> list[list[float]]:
    """Return one vector per input text, in input order.

    Lookup order per text: memory LRU → embedding_cache table → real
    embedder (``semantic_search._embed``) for ONLY the missing texts, in one
    batch. Fresh vectors are written back to both tiers; duplicate texts
    within a batch are embedded once.
    """
    if not texts:
        return []

    results: list[list[float] | None] = [None] * len(texts)
    missing: dict[str, list[int]] = {}   # content_hash -> positions
    missing_texts: dict[str, str] = {}   # content_hash -> text

    for i, text in enumerate(texts):
        key = _content_hash(text)
        vec = _mem_get(key)
        if vec is None:
            vec = _db_get(key)
            if vec is not None:
                _mem_put(key, vec)
        if vec is not None:
            results[i] = vec
        else:
            missing.setdefault(key, []).append(i)
            missing_texts[key] = text

    if missing:
        # Lazy import: semantic_search imports this module at its call
        # sites, so a top-level import here would be a load cycle.
        from services import semantic_search
        keys = list(missing.keys())
        fresh = semantic_search._embed([missing_texts[k] for k in keys])
        for key, vec in zip(keys, fresh):
            vec = list(vec)
            _mem_put(key, vec)
            _db_put(key, vec)
            for pos in missing[key]:
                results[pos] = vec
        _prune()

    return results  # type: ignore[return-value]  # every slot filled above
