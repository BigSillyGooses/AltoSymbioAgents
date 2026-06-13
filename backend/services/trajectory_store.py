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

Perf Phase 6 — trajectory learning v2
-------------------------------------
* **Graded verdicts**: ``quality_score(row)`` maps a row's outcome onto
  [0, 1] instead of the binary success flag — MAST failure classes carry
  partial credit (a verification failure is less damning than a
  specification failure), escalated-but-successful turns are damped, and
  errors/empties stay 0. Written on ``record()`` (the new
  ``trajectories.quality_score`` column) and lazily backfilled on read for
  legacy rows. ``bias_for``/``bias_table`` become similarity-weighted MEAN
  quality — numerically identical to the old success rate whenever every
  involved row's quality is exactly 1.0 or 0.0 (i.e. binary outcomes).
* **Consolidation** (``consolidate()``, flag ``trajectory_consolidation_enabled``):
  greedy single pass over unconsolidated trajectories. Each either merges
  into an existing ``routing_hints`` row (same agent/backend, similarity ≥
  ``trajectory_hint_merge_sim``: running quality mean, ``support_count += 1``)
  or seeds a new hint when ≥ ``trajectory_consolidation_min_cluster``
  similar same-agent/backend rows exist (the cluster medoid becomes the
  exemplar). Consolidated rows are dropped from ``vec_trajectories`` (the
  ``trajectories`` row is kept for audit) so the brute-force KNN stays
  bounded; stale and no-signal hints are pruned in the same pass.
* ``hint_table()`` is the hint counterpart of ``bias_table`` — one embed +
  one KNN over ``vec_routing_hints`` per routing decision.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import db
from services import perf_metrics, semantic_search

log = logging.getLogger("alto.trajectory_store")

# A MAST failure code looks like "1.1" .. "3.3"; "success" is everything else.
_MAST_RE = re.compile(r"^[123]\.[1-6]$")

# ── Graded verdict constants (Perf Phase 6) ───────────────────────────────────
# MAST (Cemri et al. 2025) codes are "N.M" strings — see the verbatim list in
# hub_router._MAST_CATEGORIES, which is where classify_failure() produces the
# values stored in trajectories.quality_verdict. The category digit N orders
# severity for routing purposes: specification failures (1.x) say the agent
# was fundamentally miscast, inter-agent misalignment (2.x) is partially
# situational, verification failures (3.x) mean the work happened but wasn't
# checked. Prefix matching on "N." is safe because the codes are exactly
# one digit, a dot, and one digit (enforced by _MAST_RE).
MAST_CLASS_SCORES: dict[str, float] = {
    "1.": 0.1,   # Specification failures — strongest negative signal.
    "2.": 0.25,  # Inter-agent misalignment.
    "3.": 0.35,  # Verification failures — weakest negative signal.
}
QUALITY_SUCCESS: float = 1.0
# A turn that only succeeded after the router escalated local → Claude is a
# weaker endorsement of the original decision. The marker below matches the
# reasoning strings TaskRouter writes into route_reasoning ("low confidence
# (45%) — escalated to Claude") — matched case-insensitively on the stem so
# future "escalation"/"escalated" phrasings still register.
QUALITY_ESCALATED_SUCCESS: float = 0.8
_ESCALATION_MARKER = "escalat"
QUALITY_NEUTRAL: float = 0.5

# Hints with support below this and quality in the no-signal band are pruned;
# also the support level at which a hint's bias delta stops being damped
# (hub_router scales deltas by min(1, support / HINT_FULL_SUPPORT)).
HINT_FULL_SUPPORT: int = 5
_NO_SIGNAL_LOW: float = 0.4
_NO_SIGNAL_HIGH: float = 0.6

# Live core.settings.Settings handle (or any object with .get), attached at
# API init via ``attach_settings`` — same idiom as semantic_search /
# embedding_cache. None (unit tests, early startup) means the consolidation
# feature reads as disabled and every knob falls back to its default.
_settings_obj = None

# Inline-consolidation trigger: record() counts rows since the last attempt
# and runs consolidate() best-effort once the counter reaches
# ``trajectory_consolidation_interval_turns`` (the background indexer hook
# in semantic_search.run_indexer_cycle covers the rest).
_records_since_consolidate = 0


def attach_settings(settings) -> None:
    """Give this module the live Settings store (consolidation flags)."""
    global _settings_obj
    _settings_obj = settings


def _setting(key: str, default, settings=None):
    """Best-effort settings read; explicit ``settings`` wins, then the
    attached store, then the default."""
    obj = settings if settings is not None else _settings_obj
    if obj is None:
        return default
    try:
        value = obj.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_success(verdict: Optional[str]) -> bool:
    """A verdict counts as success unless it is a MAST failure code."""
    if not verdict:
        return True
    return not bool(_MAST_RE.match(verdict.strip()))


# ── Graded verdicts (Perf Phase 6) ────────────────────────────────────────────

def _field(row, key, default=None):
    """Tolerant field access for sqlite3.Row / dict alike."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def quality_score(row) -> float:
    """Graded outcome quality in [0, 1] for a trajectory row (or any mapping
    with the same field names).

    Mapping:
      - ``had_error`` or ``response_empty``      → 0.0
      - MAST code "1.x"                          → 0.1
      - MAST code "2.x"                          → 0.25
      - MAST code "3.x"                          → 0.35
      - "success" (or the legacy empty verdict,
        which ``is_success`` already treats as
        success)                                 → 1.0,
        damped to 0.8 when route_reasoning shows the turn only succeeded
        after an escalation ("… — escalated to Claude")
      - anything else                            → 0.5 (neutral)
    """
    if bool(_field(row, "had_error", 0)) or bool(_field(row, "response_empty", 0)):
        return 0.0
    verdict = str(_field(row, "quality_verdict", "") or "").strip()
    if _MAST_RE.match(verdict):
        for prefix, score in MAST_CLASS_SCORES.items():
            if verdict.startswith(prefix):
                return score
        return QUALITY_NEUTRAL  # unreachable for codes accepted by _MAST_RE
    if not verdict or verdict.lower() == "success":
        reasoning = str(_field(row, "route_reasoning", "") or "")
        if _ESCALATION_MARKER in reasoning.lower():
            return QUALITY_ESCALATED_SUCCESS
        return QUALITY_SUCCESS
    return QUALITY_NEUTRAL


def _row_quality(row) -> float:
    """Quality for a trajectories row, lazily backfilling pre-Phase-6 rows.

    Rows written before the ``quality_score`` column existed carry NULL;
    the first read computes the graded score and persists it (best-effort)
    so the backfill happens at most once per row.
    """
    stored = _field(row, "quality_score")
    if stored is not None:
        return float(stored)
    computed = quality_score(row)
    row_id = _field(row, "id")
    if row_id is not None:
        try:
            db.execute(
                "UPDATE trajectories SET quality_score = ? WHERE id = ?",
                (computed, row_id),
            )
            db.commit()
        except Exception:
            pass  # backfill is best-effort; recompute next read
    return computed


# ── Embedding helpers (mirror semantic_search's document/memory upsert) ───────

def _embed_trajectory(trajectory_id: str, task_text: str) -> bool:
    """Embed one trajectory into vec_trajectories. Returns True on success."""
    if not semantic_search.is_available():
        return False
    text = (task_text or "").strip()
    if not text:
        return False
    try:
        # _embed_cached routes through the embedding cache when the
        # embedding_cache_enabled flag is on; otherwise it is _embed.
        vec_blob = semantic_search._serialize(semantic_search._embed_cached([text])[0])
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
    was unavailable at record time. Consolidated rows are skipped — their
    vectors were deliberately dropped from vec_trajectories.
    """
    rows = db.fetchall(
        "SELECT id, task_text FROM trajectories "
        "WHERE embedding_status = 'dirty' "
        "AND COALESCE(consolidated, 0) = 0 LIMIT ?",
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
    global _records_since_consolidate
    text = (task_text or "").strip()
    if not text:
        return None
    traj_id = str(uuid.uuid4())
    quality = quality_score({
        "quality_verdict": quality_verdict,
        "had_error": had_error,
        "response_empty": response_empty,
        "route_reasoning": route_reasoning,
    })
    try:
        db.execute(
            """INSERT INTO trajectories
               (id, conversation_id, turn_id, task_text, agent_id, skill_matched,
                backend, model_name, routing_score, route_reasoning,
                quality_verdict, had_error, response_empty, tokens_in, tokens_out,
                quality_score, embedding_status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'dirty', ?)""",
            (
                traj_id, conversation_id, turn_id, text, agent_id, skill_matched,
                backend, model_name, routing_score, route_reasoning,
                quality_verdict, int(bool(had_error)), int(bool(response_empty)),
                int(tokens_in or 0), int(tokens_out or 0), quality, _now(),
            ),
        )
        db.commit()
    except Exception as exc:
        log.warning("trajectory record failed: %s", exc)
        return None

    _embed_trajectory(traj_id, text)  # best-effort inline; index_pending retries

    # Perf Phase 6: inline consolidation trigger. Flag-gated and best-effort
    # — consolidation trouble must never break the turn that recorded.
    if bool(_setting("trajectory_consolidation_enabled", False)):
        try:
            _records_since_consolidate += 1
            interval = int(_setting("trajectory_consolidation_interval_turns", 25))
            if _records_since_consolidate >= max(1, interval):
                _records_since_consolidate = 0
                consolidate()
        except Exception as exc:
            log.debug("inline trajectory consolidation skipped: %s", exc)

    return traj_id


# ── Read path ─────────────────────────────────────────────────────────────────

def _distance_to_score(distance: float) -> float:
    """Legacy distance→similarity mapping (find_similar / bias paths).

    NOTE: non-monotonic across the d=1 boundary (1−d falls to 0, then
    1/(1+d) jumps back to ~0.5). Kept verbatim for the raw recall paths so
    their scores/thresholds stay byte-compatible; the Phase-6 hint paths use
    the monotonic ``_monotonic_score`` below instead.
    """
    return (
        round(1.0 - distance, 3) if distance <= 1.0
        else round(1.0 / (1.0 + distance), 3)
    )


def _monotonic_score(distance: float) -> float:
    """Monotonic distance→similarity for the Phase-6 hint paths.

    ``max(0, 1 − d)`` — strictly order-preserving, so "closer is more
    similar" actually holds when ranking hints and forming clusters. The
    legacy mapping would rank an unrelated pair (L2 distance ≳ 1 between
    normalized vectors) ABOVE a related one at d ≈ 0.6, which poisons any
    table small enough for the KNN to return everything.
    """
    return max(0.0, round(1.0 - distance, 3))


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
        query_blob = semantic_search._serialize(semantic_search._embed_cached([text])[0])
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
        score = _distance_to_score(vr["distance"])
        if score < min_sim:
            continue
        row = db.fetchone(
            """SELECT id, conversation_id, task_text, agent_id, skill_matched,
                      backend, model_name, routing_score, quality_verdict,
                      had_error, response_empty, quality_score,
                      route_reasoning, created_at
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
            # Perf Phase 6: graded outcome (lazily backfilled for old rows).
            "quality": _row_quality(row),
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
    """Similarity-weighted mean quality in [0,1] for an agent on this task.

    Returns None when there is no relevant history (caller should then leave
    routing unchanged). A value > 0.5 means the agent historically did well on
    similar tasks; < 0.5 means it struggled.

    Perf Phase 6: the numerator weights each recalled row by its graded
    ``quality_score`` instead of the binary success flag. When every involved
    row is binary (quality exactly 1.0 or 0.0 — all pre-Phase-6 behavior),
    the result is numerically identical to the old success rate.
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
    quality_sum = sum(t["score"] * t["quality"] for t in similar)
    return round(quality_sum / weight_sum, 3)


def bias_table(
    task_text: str,
    *,
    top_k: int = 5,
    min_sim: float = 0.6,
) -> dict[str, float]:
    """Per-agent similarity-weighted mean quality from a SINGLE recall.

    Returns ``{agent_id: mean_quality}`` for every agent that appears in the
    trajectories most similar to ``task_text``. This lets the router bias an
    arbitrary number of candidate agents while embedding/querying the task
    exactly once per turn (instead of once per candidate via ``bias_for``).
    Same binary-outcome equivalence as ``bias_for``.
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
        quality_sum = sum(r["score"] * r["quality"] for r in rows)
        out[aid] = round(quality_sum / weight_sum, 3)
    return out


# ── Perf Phase 6: consolidation into routing hints ────────────────────────────

def _embed_hint(hint_id: str, exemplar_text: str) -> bool:
    """Embed one hint exemplar into vec_routing_hints (map + vec0 idiom)."""
    if not semantic_search.is_available():
        return False
    text = (exemplar_text or "").strip()
    if not text:
        return False
    try:
        vec_blob = semantic_search._serialize(semantic_search._embed_cached([text])[0])
        existing = db.fetchone(
            "SELECT vec_rowid FROM vec_routing_hints_map WHERE hint_id = ?",
            (hint_id,),
        )
        if existing:
            db.execute(
                "UPDATE vec_routing_hints SET embedding = ? WHERE rowid = ?",
                (vec_blob, existing["vec_rowid"]),
            )
        else:
            db.execute(
                "INSERT INTO vec_routing_hints_map (hint_id) VALUES (?)",
                (hint_id,),
            )
            db.commit()
            row2 = db.fetchone(
                "SELECT vec_rowid FROM vec_routing_hints_map WHERE hint_id = ?",
                (hint_id,),
            )
            db.execute(
                "INSERT INTO vec_routing_hints (rowid, embedding) VALUES (?, ?)",
                (row2["vec_rowid"], vec_blob),
            )
        db.commit()
        return True
    except Exception as exc:
        log.warning("routing hint embed failed for %s: %s", hint_id, exc)
        return False


def _delete_hint(hint_id: str) -> None:
    """Remove a hint row plus its vec0 vector + map entry (best-effort)."""
    try:
        mapping = db.fetchone(
            "SELECT vec_rowid FROM vec_routing_hints_map WHERE hint_id = ?",
            (hint_id,),
        )
        if mapping:
            db.execute(
                "DELETE FROM vec_routing_hints WHERE rowid = ?",
                (mapping["vec_rowid"],),
            )
            db.execute(
                "DELETE FROM vec_routing_hints_map WHERE hint_id = ?",
                (hint_id,),
            )
        db.execute("DELETE FROM routing_hints WHERE id = ?", (hint_id,))
        db.commit()
    except Exception as exc:
        log.debug("routing hint delete failed for %s: %s", hint_id, exc)


def _search_hints(query_blob: bytes, k: int) -> list[dict]:
    """KNN over vec_routing_hints. Returns hint rows with a ``sim`` score."""
    vec_rows = db.fetchall(
        """
        SELECT v.distance, m.hint_id
        FROM vec_routing_hints v
        INNER JOIN vec_routing_hints_map m ON m.vec_rowid = v.rowid
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
        """,
        (query_blob, k),
    )
    out: list[dict] = []
    for vr in vec_rows:
        row = db.fetchone(
            "SELECT id, exemplar_text, agent_id, backend, skill, quality, "
            "support_count, created_at, last_seen FROM routing_hints WHERE id = ?",
            (vr["hint_id"],),
        )
        if not row:
            continue
        hint = dict(row)
        hint["sim"] = _monotonic_score(vr["distance"])
        out.append(hint)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    """Plain-python cosine similarity (clusters are small; no numpy needed)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def consolidate(settings=None) -> int:
    """Distil unconsolidated trajectories into ``routing_hints``.

    Greedy single pass in recording order. For each unconsolidated row:

    1. **Merge** — if the most similar existing hint is at/above
       ``trajectory_hint_merge_sim`` AND shares (agent_id, backend), fold the
       row in: hint quality becomes the support-weighted running mean, the
       support count increments, ``last_seen`` refreshes.
    2. **Cluster** — otherwise look for similar *unconsolidated* raw
       trajectories with the same (agent_id, backend); once
       ``trajectory_consolidation_min_cluster`` members exist, create a hint
       from the cluster medoid (member with the highest mean similarity to
       the others), quality = mean member quality, support = cluster size.
    3. Rows in neither bucket stay unconsolidated for a later pass.

    The same pass prunes stale hints (``last_seen`` older than
    ``trajectory_hint_max_age_days`` with support below the cluster minimum)
    and no-signal hints (quality ≈ 0.5 with low support), then drops every
    consolidated trajectory's vector from ``vec_trajectories`` — the
    ``trajectories`` rows are kept for audit, but the brute-force KNN no
    longer pays for them.

    Returns the number of trajectories consolidated (merged + clustered) in
    this pass. Callers gate on ``trajectory_consolidation_enabled``; calling
    directly (tests, the perf harness) always runs.
    """
    if not semantic_search.is_available():
        return 0
    merge_sim = float(_setting("trajectory_hint_merge_sim", 0.75, settings))
    min_cluster = max(2, int(_setting("trajectory_consolidation_min_cluster", 3, settings)))
    max_age_days = int(_setting("trajectory_hint_max_age_days", 90, settings))

    try:
        rows = db.fetchall(
            "SELECT id, task_text, agent_id, skill_matched, backend, "
            "quality_verdict, had_error, response_empty, route_reasoning, "
            "quality_score, created_at "
            "FROM trajectories WHERE COALESCE(consolidated, 0) = 0 "
            "AND embedding_status = 'clean' "
            "ORDER BY created_at, id"
        )
    except Exception as exc:
        log.debug("consolidate: trajectory read failed: %s", exc)
        return 0

    consolidated_ids: set[str] = set()
    consolidated_count = 0

    for row in rows:
        if row["id"] in consolidated_ids:
            continue
        text = (row["task_text"] or "").strip()
        if not text:
            continue
        try:
            query_vec = semantic_search._embed_cached([text])[0]
            query_blob = semantic_search._serialize(query_vec)
        except Exception as exc:
            log.debug("consolidate: embed failed for %s: %s", row["id"], exc)
            continue
        quality = _row_quality(row)

        # 1) Merge into the closest matching existing hint.
        merged = False
        try:
            for hint in _search_hints(query_blob, k=4):
                if hint["sim"] < merge_sim:
                    break  # monotonic score + distance order: nothing closer follows
                if hint["agent_id"] != row["agent_id"] or hint["backend"] != row["backend"]:
                    continue
                support = int(hint["support_count"] or 1)
                new_quality = (float(hint["quality"]) * support + quality) / (support + 1)
                db.execute(
                    "UPDATE routing_hints SET quality = ?, support_count = ?, "
                    "last_seen = ? WHERE id = ?",
                    (round(new_quality, 6), support + 1, _now(), hint["id"]),
                )
                db.execute(
                    "UPDATE trajectories SET consolidated = 1 WHERE id = ?",
                    (row["id"],),
                )
                db.commit()
                consolidated_ids.add(row["id"])
                consolidated_count += 1
                merged = True
                break
        except Exception as exc:
            log.debug("consolidate: hint merge failed for %s: %s", row["id"], exc)
        if merged:
            continue

        # 2) Try to form a new cluster among unconsolidated raw trajectories.
        try:
            vec_rows = db.fetchall(
                """
                SELECT v.distance, m.trajectory_id
                FROM vec_trajectories v
                INNER JOIN vec_trajectories_map m ON m.vec_rowid = v.rowid
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance
                """,
                (query_blob, max(16, min_cluster * 4)),
            )
        except Exception as exc:
            log.debug("consolidate: cluster search failed for %s: %s", row["id"], exc)
            continue

        members: list[dict] = []
        seen_member_ids: set[str] = set()
        for vr in vec_rows:
            sim = _monotonic_score(vr["distance"])
            if sim < merge_sim:
                break  # monotonic score + distance order: nothing closer follows
            tid = vr["trajectory_id"]
            if tid in consolidated_ids or tid in seen_member_ids:
                continue
            member = db.fetchone(
                "SELECT id, task_text, agent_id, skill_matched, backend, "
                "quality_verdict, had_error, response_empty, route_reasoning, "
                "quality_score FROM trajectories "
                "WHERE id = ? AND COALESCE(consolidated, 0) = 0",
                (tid,),
            )
            if not member:
                continue
            if member["agent_id"] != row["agent_id"] or member["backend"] != row["backend"]:
                continue
            members.append(dict(member))
            seen_member_ids.add(tid)
        if row["id"] not in seen_member_ids:
            members.insert(0, dict(row))
            seen_member_ids.add(row["id"])

        if len(members) < min_cluster:
            continue  # leave for a later pass — more members may arrive

        try:
            member_texts = [(m["task_text"] or "").strip() for m in members]
            member_vecs = semantic_search._embed_cached(member_texts)
            # Medoid: highest mean similarity to the other members. With one
            # member per text the bag-of-words embedder makes this exact; ties
            # break toward the earliest (recording-order) member.
            best_idx, best_mean = 0, -2.0
            for i, vi in enumerate(member_vecs):
                sims = [
                    _cosine(vi, vj)
                    for j, vj in enumerate(member_vecs) if j != i
                ]
                mean_sim = sum(sims) / len(sims) if sims else 0.0
                if mean_sim > best_mean:
                    best_idx, best_mean = i, mean_sim
            medoid = members[best_idx]
            cluster_quality = sum(_row_quality(m) for m in members) / len(members)

            hint_id = str(uuid.uuid4())
            now = _now()
            db.execute(
                """INSERT INTO routing_hints
                   (id, exemplar_text, agent_id, backend, skill, quality,
                    support_count, created_at, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    hint_id, (medoid["task_text"] or "").strip(),
                    medoid["agent_id"], medoid["backend"],
                    medoid["skill_matched"], round(cluster_quality, 6),
                    len(members), now, now,
                ),
            )
            for m in members:
                db.execute(
                    "UPDATE trajectories SET consolidated = 1 WHERE id = ?",
                    (m["id"],),
                )
            db.commit()
            _embed_hint(hint_id, medoid["task_text"])
            consolidated_ids.update(m["id"] for m in members)
            consolidated_count += len(members)
        except Exception as exc:
            log.debug("consolidate: cluster creation failed for %s: %s", row["id"], exc)

    _prune_hints(min_cluster=min_cluster, max_age_days=max_age_days)
    _drop_consolidated_vectors()
    return consolidated_count


def _prune_hints(*, min_cluster: int, max_age_days: int) -> None:
    """Delete stale and no-signal hints (vec rows included). Best-effort."""
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))
        ).isoformat()
        stale = db.fetchall(
            "SELECT id FROM routing_hints "
            "WHERE COALESCE(last_seen, created_at) < ? AND support_count < ?",
            (cutoff, min_cluster),
        )
        no_signal = db.fetchall(
            "SELECT id FROM routing_hints "
            "WHERE quality BETWEEN ? AND ? AND support_count < ?",
            (_NO_SIGNAL_LOW, _NO_SIGNAL_HIGH, HINT_FULL_SUPPORT),
        )
        for r in {row["id"] for row in stale} | {row["id"] for row in no_signal}:
            _delete_hint(r)
    except Exception as exc:
        log.debug("routing hint prune failed: %s", exc)


def _drop_consolidated_vectors() -> None:
    """Remove consolidated trajectories from vec_trajectories (+ map).

    The trajectories rows stay for audit; dropping the vectors is what keeps
    the brute-force KNN bounded as history grows (the consolidation answer to
    retrieval scale, in lieu of an ANN index).
    """
    try:
        mappings = db.fetchall(
            "SELECT m.vec_rowid, m.trajectory_id FROM vec_trajectories_map m "
            "INNER JOIN trajectories t ON t.id = m.trajectory_id "
            "WHERE COALESCE(t.consolidated, 0) = 1"
        )
        for m in mappings:
            db.execute(
                "DELETE FROM vec_trajectories WHERE rowid = ?", (m["vec_rowid"],)
            )
            db.execute(
                "DELETE FROM vec_trajectories_map WHERE trajectory_id = ?",
                (m["trajectory_id"],),
            )
        if mappings:
            db.commit()
    except Exception as exc:
        log.debug("consolidated vector drop failed: %s", exc)


def hint_table(
    task_text: str,
    top_k: int = 3,
    min_sim: Optional[float] = None,
    *,
    backend: Optional[str] = None,
) -> dict[str, tuple[float, int]]:
    """Per-agent routing hints from ONE embed + ONE KNN over the hint table.

    Returns ``{agent_id: (quality, support_count)}`` for the hints most
    similar to ``task_text`` (the closest hint wins when an agent has
    several). ``min_sim`` defaults to the find_similar default (0.6);
    ``backend`` optionally restricts hints to one backend — the TaskRouter
    uses that to ask "how do LOCAL turns historically go on this task?".
    """
    text = (task_text or "").strip()
    if not text or not semantic_search.is_available():
        return {}
    if min_sim is None:
        min_sim = 0.6
    with perf_metrics.span("traj_hint_table"):
        try:
            query_blob = semantic_search._serialize(
                semantic_search._embed_cached([text])[0]
            )
            hints = _search_hints(query_blob, k=max(top_k * 4, top_k))
        except Exception as exc:
            log.warning("routing hint search failed: %s", exc)
            return {}
        out: dict[str, tuple[float, int]] = {}
        for hint in hints:
            if hint["sim"] < min_sim:
                break  # monotonic score + distance order: nothing closer follows
            if backend is not None and hint["backend"] != backend:
                continue
            aid = hint["agent_id"]
            if not aid or aid in out:
                continue  # closest hint per agent wins
            out[aid] = (float(hint["quality"]), int(hint["support_count"] or 0))
            if len(out) >= top_k:
                break
        return out
