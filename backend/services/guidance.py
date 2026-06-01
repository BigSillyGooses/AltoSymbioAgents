"""
services/guidance.py — Guidance / Constitution compiler.

Stores project/role rules as discrete, individually-embedded "shards" so that
only the rules *relevant to the current message* are injected into the system
prompt — instead of pasting the whole rulebook on every turn. This cuts tokens
and improves adherence (the model sees a short, on-topic constitution).

Reuses the same embedding pipeline and ``map-table + vec0`` idiom as
``services.semantic_search`` / ``services.trajectory_store``.

A "shard" is one atomic rule (a bullet, a sentence, or a short paragraph).
``compile_from_prompt`` splits a CLAUDE.md-style document into shards;
``retrieve`` returns the shards most similar to the current message, always
including any always-on high-priority rules so critical guardrails are never
dropped by the similarity filter.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import db
from services import semantic_search

log = logging.getLogger("alto.guidance")

# Rules at/above this priority are ALWAYS injected regardless of similarity,
# so safety-critical guardrails can't be filtered out.
ALWAYS_ON_PRIORITY = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Shard splitting ───────────────────────────────────────────────────────────

_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")


def split_into_shards(text: str) -> list[str]:
    """Split a rules document into atomic rule shards.

    Headings become a prefix context for the bullets/lines beneath them.
    Bullet lines and standalone non-empty lines each become one shard. Long
    paragraphs are split on sentence boundaries.
    """
    shards: list[str] = []
    current_heading = ""
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _HEADING_RE.match(line):
            current_heading = _HEADING_RE.sub("", line).strip()
            continue
        body = _BULLET_RE.sub("", line).strip()
        if not body:
            continue
        prefix = f"[{current_heading}] " if current_heading else ""
        # Split overly-long lines into sentences so each shard stays atomic.
        if len(body) > 240:
            for sent in re.split(r"(?<=[.!?])\s+", body):
                sent = sent.strip()
                if sent:
                    shards.append(prefix + sent)
        else:
            shards.append(prefix + body)
    return shards


# ── Embedding (mirror trajectory_store / semantic_search upsert) ──────────────

def _embed_rule(rule_id: str, rule_text: str) -> bool:
    if not semantic_search.is_available():
        return False
    text = (rule_text or "").strip()
    if not text:
        return False
    try:
        vec_blob = semantic_search._serialize(semantic_search._embed([text])[0])
        existing = db.fetchone(
            "SELECT vec_rowid FROM vec_guidance_map WHERE rule_id = ?", (rule_id,)
        )
        if existing:
            db.execute(
                "UPDATE vec_guidance SET embedding = ? WHERE rowid = ?",
                (vec_blob, existing["vec_rowid"]),
            )
        else:
            db.execute(
                "INSERT INTO vec_guidance_map (rule_id) VALUES (?)", (rule_id,)
            )
            db.commit()
            row2 = db.fetchone(
                "SELECT vec_rowid FROM vec_guidance_map WHERE rule_id = ?", (rule_id,)
            )
            db.execute(
                "INSERT INTO vec_guidance (rowid, embedding) VALUES (?, ?)",
                (row2["vec_rowid"], vec_blob),
            )
        db.execute(
            "UPDATE guidance_rules SET embedding_status = 'clean' WHERE id = ?",
            (rule_id,),
        )
        db.commit()
        return True
    except Exception as exc:
        log.warning("guidance embed failed for %s: %s", rule_id, exc)
        return False


def index_pending(batch_size: int = 100) -> int:
    rows = db.fetchall(
        "SELECT id, rule_text FROM guidance_rules "
        "WHERE embedding_status = 'dirty' AND enabled = 1 LIMIT ?",
        (batch_size,),
    )
    done = 0
    for r in rows:
        if _embed_rule(r["id"], r["rule_text"]):
            done += 1
    return done


# ── CRUD ──────────────────────────────────────────────────────────────────────

def add_rule(
    rule_text: str,
    *,
    scope: str = "global",
    priority: int = 0,
    source: str = "",
) -> Optional[str]:
    text = (rule_text or "").strip()
    if not text:
        return None
    # De-dupe identical shards within the same scope.
    existing = db.fetchone(
        "SELECT id FROM guidance_rules WHERE scope = ? AND rule_text = ?",
        (scope, text),
    )
    if existing:
        return existing["id"]
    rule_id = str(uuid.uuid4())
    now = _now()
    db.execute(
        """INSERT INTO guidance_rules
           (id, scope, rule_text, priority, source, enabled, embedding_status,
            created_at, updated_at)
           VALUES (?,?,?,?,?,1,'dirty',?,?)""",
        (rule_id, scope, text, int(priority), source, now, now),
    )
    db.commit()
    _embed_rule(rule_id, text)
    return rule_id


def compile_from_prompt(
    text: str, *, scope: str = "global", source: str = "", priority: int = 0
) -> list[str]:
    """Split a rules document into shards and persist each. Returns rule ids."""
    ids: list[str] = []
    for shard in split_into_shards(text):
        rid = add_rule(shard, scope=scope, priority=priority, source=source)
        if rid:
            ids.append(rid)
    return ids


def list_rules(scope: Optional[str] = None) -> list[dict]:
    if scope:
        rows = db.fetchall(
            "SELECT * FROM guidance_rules WHERE scope = ? ORDER BY priority DESC, created_at",
            (scope,),
        )
    else:
        rows = db.fetchall(
            "SELECT * FROM guidance_rules ORDER BY priority DESC, created_at"
        )
    return [dict(r) for r in rows]


def delete_rule(rule_id: str) -> bool:
    db.execute("DELETE FROM guidance_rules WHERE id = ?", (rule_id,))
    db.execute("DELETE FROM vec_guidance_map WHERE rule_id = ?", (rule_id,))
    db.commit()
    return True


def set_enabled(rule_id: str, enabled: bool) -> bool:
    db.execute(
        "UPDATE guidance_rules SET enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, _now(), rule_id),
    )
    db.commit()
    return True


# ── Retrieval ─────────────────────────────────────────────────────────────────

def retrieve(
    message: str,
    *,
    scope: str = "global",
    top_k: int = 5,
    min_sim: float = 0.45,
) -> list[dict]:
    """Return the rule shards most relevant to ``message``.

    Always-on high-priority rules (priority >= ALWAYS_ON_PRIORITY) are included
    unconditionally. The remaining slots are filled by vector similarity. Falls
    back to top-priority rules when the vector store is unavailable.
    """
    out: list[dict] = []
    seen: set[str] = set()

    always = db.fetchall(
        "SELECT id, rule_text, priority FROM guidance_rules "
        "WHERE enabled = 1 AND scope IN (?, 'global') AND priority >= ? "
        "ORDER BY priority DESC",
        (scope, ALWAYS_ON_PRIORITY),
    )
    for r in always:
        if r["id"] not in seen:
            out.append({"id": r["id"], "rule_text": r["rule_text"],
                        "priority": r["priority"], "score": 1.0})
            seen.add(r["id"])

    text = (message or "").strip()
    if text and semantic_search.is_available():
        try:
            query_blob = semantic_search._serialize(semantic_search._embed([text])[0])
            vec_rows = db.fetchall(
                """
                SELECT v.distance, m.rule_id
                FROM vec_guidance v
                INNER JOIN vec_guidance_map m ON m.vec_rowid = v.rowid
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance
                """,
                (query_blob, top_k * 4),
            )
        except Exception as exc:
            log.warning("guidance search failed: %s", exc)
            vec_rows = []
        for vr in vec_rows:
            if len(out) >= top_k + len(always):
                break
            rid = vr["rule_id"]
            if rid in seen:
                continue
            distance = vr["distance"]
            score = round(1.0 - distance, 3) if distance <= 1.0 else round(1.0 / (1.0 + distance), 3)
            if score < min_sim:
                continue
            row = db.fetchone(
                "SELECT id, rule_text, priority, scope, enabled FROM guidance_rules WHERE id = ?",
                (rid,),
            )
            if not row or not row["enabled"]:
                continue
            if row["scope"] not in (scope, "global"):
                continue
            out.append({"id": row["id"], "rule_text": row["rule_text"],
                        "priority": row["priority"], "score": score})
            seen.add(rid)
    elif not out:
        # No vector store and no always-on rules: surface the top-priority ones.
        rows = db.fetchall(
            "SELECT id, rule_text, priority FROM guidance_rules "
            "WHERE enabled = 1 AND scope IN (?, 'global') "
            "ORDER BY priority DESC, created_at LIMIT ?",
            (scope, top_k),
        )
        for r in rows:
            out.append({"id": r["id"], "rule_text": r["rule_text"],
                        "priority": r["priority"], "score": 0.0})

    return out


def format_block(rules: list[dict]) -> str:
    """Render retrieved rules as a system-prompt section."""
    if not rules:
        return ""
    lines = ["\n\n## Relevant Rules",
             "(The following project/role rules apply to this request.)"]
    for r in rules:
        lines.append(f"- {r['rule_text']}")
    return "\n".join(lines)


def rule_count() -> int:
    row = db.fetchone("SELECT COUNT(*) AS n FROM guidance_rules WHERE enabled = 1")
    return int(row["n"]) if row else 0
