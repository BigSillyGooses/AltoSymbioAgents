"""
services/workers/builtin.py — built-in background workers.

Each worker does real work against Alto's own data stores — no placeholders.
"""

from __future__ import annotations

import logging
from typing import Callable

import db
from .base import Worker

log = logging.getLogger("alto.workers.builtin")


class ReindexWorker(Worker):
    """Run one embedding-indexer cycle (documents, memories, trajectories, rules)."""

    name = "reindex"
    description = "Embed any pending documents, memories, trajectories and guidance rules into the vector index."

    def run(self, params: dict, progress: Callable[[float, str], None]) -> dict:
        from services import semantic_search
        progress(0.1, "starting indexer cycle")
        if not semantic_search.is_available():
            progress(1.0, "vector store unavailable")
            return {"indexed": 0, "vector_store": "unavailable"}
        total = semantic_search.run_indexer_cycle()
        progress(1.0, f"indexed {total} record(s)")
        return {"indexed": total, "vector_store": "available"}


class MemoryAuditWorker(Worker):
    """Audit the memory tiers: counts, and stale long-term memories."""

    name = "memory_audit"
    description = "Report memory-tier sizes and surface stale long-term memories for review."

    def run(self, params: dict, progress: Callable[[float, str], None]) -> dict:
        days = int(params.get("days", 30))
        progress(0.2, "counting memory tiers")
        memories = db.fetchone("SELECT COUNT(*) AS n FROM memory_entries")
        facts = db.fetchone("SELECT COUNT(*) AS n FROM session_facts")
        docs = db.fetchone("SELECT COUNT(*) AS n FROM documents")

        progress(0.6, "scanning for stale memories")
        stale = []
        try:
            from services import semantic_search
            stale = semantic_search.get_stale_memories(days=days)
        except Exception as exc:
            log.debug("stale scan skipped: %s", exc)

        progress(1.0, "audit complete")
        return {
            "memory_entries": memories["n"] if memories else 0,
            "session_facts": facts["n"] if facts else 0,
            "documents": docs["n"] if docs else 0,
            "stale_days": days,
            "stale_count": len(stale),
            "stale_sample": [
                {"id": s.get("entry_id") or s.get("id"),
                 "content": (s.get("content") or "")[:120]}
                for s in stale[:10]
            ],
        }


class TrajectoryReportWorker(Worker):
    """Aggregate ReasoningBank-lite trajectories into per-agent success rates."""

    name = "trajectory_report"
    description = "Summarise recorded turn trajectories: per-agent success rate and volume."

    def run(self, params: dict, progress: Callable[[float, str], None]) -> dict:
        progress(0.3, "aggregating trajectories")
        rows = db.fetchall(
            """
            SELECT COALESCE(agent_id, '(hub-direct)') AS agent,
                   COUNT(*) AS total,
                   SUM(CASE WHEN quality_verdict = 'success' AND had_error = 0
                            THEN 1 ELSE 0 END) AS successes
            FROM trajectories
            GROUP BY agent
            ORDER BY total DESC
            """
        )
        agents = []
        grand_total = 0
        grand_success = 0
        for r in rows:
            total = r["total"]
            succ = r["successes"] or 0
            grand_total += total
            grand_success += succ
            agents.append({
                "agent": r["agent"],
                "total": total,
                "successes": succ,
                "success_rate": round(succ / total, 3) if total else 0.0,
            })
        progress(1.0, f"{grand_total} trajectories across {len(agents)} agent(s)")
        return {
            "total_trajectories": grand_total,
            "overall_success_rate": round(grand_success / grand_total, 3) if grand_total else 0.0,
            "agents": agents,
        }
