"""
benchmarks/ — Benchmark assets for the altosybioagents stack.

Holds the AgentDojo ASR thresholds (``thresholds.json`` — consumed by
``backend/tests/agentdojo/run_suites.py``) and the deterministic
performance harness (``perf/`` — Perf Phase 1b). This ``__init__.py``
exists so the perf harness is importable as ``benchmarks.perf`` from
the backend test suite as well as from the CLI driver.
"""
