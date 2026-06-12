"""
benchmarks/perf — Deterministic performance benchmark harness (Perf Phase 1b).

Measures the numbers the perf-upgrade plan optimizes against: tokens per
turn, prompt-cache hit rate, per-turn cost, and retrieval/model-call span
timings — all over checked-in fixtures and fake model clients so two
consecutive runs produce IDENTICAL token/cache/cost numbers (latency may
differ; see ``runner.deterministic_view``).

Modules:
  - ``run_perf``     CLI driver (modeled on backend/tests/agentdojo/run_suites.py)
  - ``runner``       isolated-environment setup + scenario orchestration
  - ``scenarios``    the scenario implementations + fixture loading
  - ``fake_clients`` FakeClaudeClient (simulated Anthropic prefix caching),
                     FakeLocalClient, FakeTaskRouter
  - ``embedder``     deterministic bag-of-words embedder (no model download)

No new runtime dependencies: stdlib + what backend/requirements.txt ships.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Path-hack so the harness works whether it is imported from the repo root
# (``python benchmarks/perf/run_perf.py``), from backend/ (pytest rootdir),
# or as ``python -m benchmarks.perf.run_perf``. Backend modules use bare
# top-level imports (``import db``, ``from services import …``), so the
# backend dir must be on sys.path too. Mirrors tests/agentdojo/run_suites.py.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
for _p in (_REPO_ROOT, _BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
