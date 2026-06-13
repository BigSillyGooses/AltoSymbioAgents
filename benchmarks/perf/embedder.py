"""
benchmarks/perf/embedder.py — Deterministic bag-of-words embedder.

Copy of ``_deterministic_embed`` from backend/tests/test_trajectory_store.py
(kept in sync by hand — it is 15 lines). The harness wires it into
``services.semantic_search`` exactly where the real fastembed function would
sit, so ingest/search/trajectory-recall run the production SQL unchanged
without downloading the fastembed model. Texts that share tokens land close
together under L2 distance, which is all the retrieval scenarios need.
"""

from __future__ import annotations

import hashlib
import math

EMBED_DIM = 384  # matches the vec0 tables in db.py (float[384])


def deterministic_embed(texts: list[str]) -> list[list[float]]:
    """Hash tokens into a normalized 384-dim bag-of-words vector."""
    out = []
    for t in texts:
        vec = [0.0] * EMBED_DIM
        for tok in (t or "").lower().split():
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % EMBED_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out
