"""
services/trajectory_store.py — ReasoningBank-lite trajectory store.

Records one row per completed turn capturing the routing decision and the
outcome verdict, embeds the task text into the ``vec_trajectories`` virtual
table (sqlite-vec), and exposes similarity recall so the router can bias
toward agents/skills that historically succeeded on semantically-similar
tasks.

Design notes
------------
* Reuses the existing embedding pipeline in ``services.semantic_search``
  (fastembed BAAI/bge-small-en-v1.5, 384 dims) and the same
  ``map-table + vec0`` upsert/search idiom used for documents and memories.
* ``record()`` embeds inline so the trajectory is queryable on the very next
  turn without depending on the background indexer. Every step is wrapped so
  a failure can never break the turn that produced it. Rows whose inline
  embed fails are left ``embedding_status='dirty'`` and picked up later by
  ``index_pending()`` (wired into the background indexer cycle).
* A "success" verdict is anything that is not an error / empty response and
  is not a MAST failure code (``N.M``). Bias is the similarity-weighted
  success rate of recalled trajectories for a candidate agent.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import db
from services import semantic_search

log = logging.getLogger("alto.trajectory_store")

# A MAST failure code looks like "1.1" .. "3.3"; "success" is everything else.
_MAST_RE = re.compile(r"^[123]\.[1-6]$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_success(verdict: Optional[str]) -> bool:
    """A verdict counts as success unless it is a MAST failure code."""
    if not verdict:
        return True
    return not bool(_MAST_RE.match(verdict.strip()))


# ── Embedding helpers (mirror semantic_search's document/memory upsert) ───────

def _embed_trajectory(trajectory_id: str, task_text: str) -> bool:
    """Embed one trajectory into vec_trajectories. Returns True on success."""
    if not semantic_search.is_available():
        return False
    text = (task_text or "").strip()
    if not text:
        return False
    try:
        vec_blob = semantic_search._serialize(semantic_search._embed([text])[0])
        existing = db.fetchone(
            "SELECT vec_rowid FROM vec_trajectories_map WHERE trajectory_id = ?",
            (trajectory_id,),
        )
        if existing:
            db.execute(
                "UPDATE vec_trajectories SET embedding = ? WHERE rowid = ?",
                (vec_blob, existing["vec_rowid"]),
            )
        else:
            db.execute(
                "INSERT INTO vec_trajectories_map (trajectory_id) VALUES (?)",
                (trajectory_id,),
            )
            db.commit()
            row2 = db.fetchone(
                "SELECT vec_rowid FROM vec_trajectories_map WHERE trajectory_id = ?",
                (trajectory_id,),
            )
            db.execute(
                "INSERT INTO vec_trajectories (rowid, embedding) VALUES (?, ?)",
                (row2["vec_rowid"], vec_blob),
            )
        db.execute(
            "UPDATE trajectories SET embedding_status = 'clean' WHERE id = ?",
            (trajectory_id,),
        )
        db.commit()
        return True
    except Exception as exc:  # never let embedding break a turn
        log.warning("trajectory embed failed for %s: %s", trajectory_id, exc)
        return False


def index_pending(batch_size: int = 50) -> int:
    """Embed any trajectories whose inline embed was skipped/failed.

    Wired into ``semantic_search.run_indexer_cycle`` so the background
    indexer keeps the vector table consistent even when the vector store
    was unavailable at record time.
    """
    rows = db.fetchall(
        "SELECT id, task_text FROM trajectories "
        "WHERE embedding_status = 'dirty' LIMIT ?",
        (batch_size,),
    )
    done = 0
    for r in rows:
        if _embed_trajectory(r["id"], r["task_text"]):
            done += 1
    return done


# ── Write path ────────────────────────────────────────────────────────────────

def record(
    *,
    conversation_id: str,
    turn_id: str,
    task_text: str,
    agent_id: Optional[str],
    skill_matched: Optional[str],
    backend: Optional[str],
    model_name: Optional[str],
    routing_score: Optional[float],
    route_reasoning: Optional[str],
    quality_verdict: Optional[str],
    had_error: bool,
    response_empty: bool,
    tokens_in: int,
    tokens_out: int,
) -> Optional[str]:
    """Persist a trajectory row and embed it. Returns the new id (or None)."""
    text = (task_text or "").strip()
    if not text:
        return None
    traj_id = str(uuid.uuid4())
    try:
        db.execute(
            """INSERT INTO trajectories
               (id, conversation_id, turn_id, task_text, agent_id, skill_matched,
                backend, model_name, routing_score, route_reasoning,
                quality_verdict, had_error, response_empty, tokens_in, tokens_out,
                embedding_status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'dirty', ?)""",
            (
                traj_id, conversation_id, turn_id, text, agent_id, skill_matched,
                backend, model_name, routing_score, route_reasoning,
                quality_verdict, int(bool(had_error)), int(bool(response_empty)),
                int(tokens_in or 0), int(tokens_out or 0), _now(),
            ),
        )
        db.commit()
    except Exception as exc:
        log.warning("trajectory record failed: %s", exc)
        return None

    _embed_trajectory(traj_id, text)  # best-effort inline; index_pending retries
    return traj_id


# ── Read path ─────────────────────────────────────────────────────────────────

def find_similar(
    task_text: str,
    *,
    agent_id: Optional[str] = None,
    top_k: int = 3,
    min_sim: float = 0.6,
) -> list[dict]:
    """Return trajectories whose task_text is most similar to ``task_text``.

    Results include a ``score`` in [0,1] and the recorded outcome fields.
    Only rows at/above ``min_sim`` are returned. If ``agent_id`` is given,
    results are restricted to that agent.
    """
    text = (task_text or "").strip()
    if not text or not semantic_search.is_available():
        return []
    try:
        query_blob = semantic_search._serialize(semantic_search._embed([text])[0])
        # Over-fetch so post-filtering by agent/threshold still yields top_k.
        vec_rows = db.fetchall(
            """
            SELECT v.distance, m.trajectory_id
            FROM vec_trajectories v
            INNER JOIN vec_trajectories_map m ON m.vec_rowid = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (query_blob, top_k * 4),
        )
    except Exception as exc:
        log.warning("trajectory search failed: %s", exc)
        return []

    out: list[dict] = []
    for vr in vec_rows:
        distance = vr["distance"]
        score = round(1.0 - distance, 3) if distance <= 1.0 else round(1.0 / (1.0 + distance), 3)
        if score < min_sim:
            continue
        row = db.fetchone(
            """SELECT id, conversation_id, task_text, agent_id, skill_matched,
                      backend, model_name, routing_score, quality_verdict,
                      had_error, response_empty, created_at
               FROM trajectories WHERE id = ?""",
            (vr["trajectory_id"],),
        )
        if not row:
            continue
        if agent_id is not None and row["agent_id"] != agent_id:
            continue
        out.append({
            "id": row["id"],
            "task_text": row["task_text"],
            "agent_id": row["agent_id"],
            "skill_matched": row["skill_matched"],
            "backend": row["backend"],
            "model_name": row["model_name"],
            "quality_verdict": row["quality_verdict"],
            "success": is_success(row["quality_verdict"]) and not row["had_error"],
            "score": score,
            "created_at": row["created_at"],
        })
        if len(out) >= top_k:
            break
    return out


def bias_for(
    task_text: str,
    candidate_agent_id: str,
    candidate_skill: Optional[str] = None,
    *,
    top_k: int = 5,
    min_sim: float = 0.6,
) -> Optional[float]:
    """Similarity-weighted success rate in [0,1] for an agent on this task.

    Returns None when there is no relevant history (caller should then leave
    routing unchanged). A value > 0.5 means the agent historically did well on
    similar tasks; < 0.5 means it struggled.
    """
    similar = find_similar(
        task_text, agent_id=candidate_agent_id, top_k=top_k, min_sim=min_sim
    )
    if candidate_skill:
        scoped = [t for t in similar if t["skill_matched"] == candidate_skill]
        if scoped:
            similar = scoped
    if not similar:
        return None
    weight_sum = sum(t["score"] for t in similar)
    if weight_sum <= 0:
        return None
    success_sum = sum(t["score"] for t in similar if t["success"])
    return round(success_sum / weight_sum, 3)


def bias_table(
    task_text: str,
    *,
    top_k: int = 5,
    min_sim: float = 0.6,
) -> dict[str, float]:
    """Per-agent similarity-weighted success rates from a SINGLE recall.

    Returns ``{agent_id: success_rate}`` for every agent that appears in the
    trajectories most similar to ``task_text``. This lets the router bias an
    arbitrary number of candidate agents while embedding/querying the task
    exactly once per turn (instead of once per candidate via ``bias_for``).
    """
    # Over-fetch across all agents in one query, then group locally.
    similar = find_similar(
        task_text, agent_id=None, top_k=max(top_k * 4, top_k), min_sim=min_sim
    )
    groups: dict[str, list[dict]] = {}
    for t in similar:
        aid = t["agent_id"]
        if not aid:
            continue
        groups.setdefault(aid, []).append(t)
    out: dict[str, float] = {}
    for aid, rows in groups.items():
        weight_sum = sum(r["score"] for r in rows)
        if weight_sum <= 0:
            continue
        success_sum = sum(r["score"] for r in rows if r["success"])
        out[aid] = round(success_sum / weight_sum, 3)
    return out
