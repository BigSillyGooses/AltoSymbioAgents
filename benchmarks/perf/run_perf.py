"""
benchmarks/perf/run_perf.py — CLI driver for the performance bench.

Usage:
    python benchmarks/perf/run_perf.py \\
        --scenario all \\
        --output benchmarks/perf_results.json

Runs the chosen deterministic scenario(s) (see ``scenarios.SCENARIOS``)
through the real backend services with fake model clients and writes
``results.json`` with the headline metrics: tokens per turn, prompt-cache
read/creation accounting, hit rate, per-turn cost, retrieval/model-call
span timings.

The script is import-clean (sys.exit codes only). It honours the gates in
``benchmarks/perf_thresholds.json``: the ``deterministic`` class holds exact
ceilings/floors on token/cost/recall numbers (identical on every run by
construction), the ``latency`` class holds generous absolute ms ceilings.
Any breach exits 1 so the GitHub workflow fails the build;
``--ignore-threshold`` is provided for local exploration.

No API key and no network access are required — that is the point.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

# Path-hack so ``python benchmarks/perf/run_perf.py`` works whether CWD is
# the repo root or backend/. Mirrors tests/agentdojo/run_suites.py (the
# package __init__ repeats it for the import-from-pytest path).
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
for p in (_REPO_ROOT, _BACKEND_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from benchmarks.perf import runner, scenarios  # noqa: E402

log = logging.getLogger("altosybioagents.perfbench")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_perf",
        description="Run the deterministic perf bench against the altosybioagents stack.",
    )
    parser.add_argument(
        "--scenario", required=True,
        choices=sorted(scenarios.SCENARIOS) + ["all"],
        help="Scenario to run, or 'all'.",
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to results.json (parent dir is created if missing).",
    )
    parser.add_argument(
        "--ignore-threshold", action="store_true",
        help="Don't fail the process when a configured gate is breached.",
    )
    parser.add_argument(
        "--thresholds",
        default=str(_REPO_ROOT / "benchmarks" / "perf_thresholds.json"),
        help="Path to the perf gate config JSON.",
    )
    args = parser.parse_args(argv)

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    started_at = time.time()
    if args.scenario == "all":
        scenario_results = runner.run_all()
    else:
        scenario_results = {args.scenario: runner.run_scenario(args.scenario)}

    results: dict[str, Any] = {
        "started_at": started_at,
        "duration_seconds": round(time.time() - started_at, 3),
        "scenarios": scenario_results,
    }
    output_path.write_text(json.dumps(results, indent=2, sort_keys=True))
    log.info("Wrote %s", output_path)

    breaches = _check_thresholds(
        results=results, thresholds_path=Path(args.thresholds),
    )
    if breaches:
        for line in breaches:
            sys.stderr.write(line + "\n")
        if not args.ignore_threshold:
            return 1
    return 0


def _resolve_metric(metrics: dict, dotted_path: str):
    """Walk a dotted path ('spans.vec_search.avg_ms') into a metrics dict."""
    node: Any = metrics
    for part in dotted_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _check_thresholds(*, results: dict, thresholds_path: Path) -> list[str]:
    """Return human-readable breach strings (empty list = all gates green).

    Gate config shape (benchmarks/perf_thresholds.json):

        {"deterministic": {"<scenario>": {"<metric.path>": {"max": N, "min": N}}},
         "latency":       {... same shape, ms ceilings ...}}

    A gate over a scenario that was not part of this run is skipped (so a
    single-scenario invocation doesn't fail every other scenario's gates).
    A gate whose metric path doesn't resolve IS a breach — a silently
    renamed metric must not turn its gate into a no-op.
    """
    if not thresholds_path.exists():
        return []
    try:
        thresholds = json.loads(thresholds_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"[threshold] {thresholds_path} could not be parsed: {exc}"]

    breaches: list[str] = []
    for gate_class in ("deterministic", "latency"):
        for scenario_name, gates in (thresholds.get(gate_class) or {}).items():
            if scenario_name.startswith("_"):
                continue
            metrics = (results.get("scenarios") or {}).get(scenario_name)
            if metrics is None:
                continue  # scenario not part of this run
            for metric_path, gate in (gates or {}).items():
                if metric_path.startswith("_") or not isinstance(gate, dict):
                    continue
                value = _resolve_metric(metrics, metric_path)
                label = f"[threshold:{gate_class}] {scenario_name}.{metric_path}"
                if value is None:
                    breaches.append(f"{label} did not resolve to a metric")
                    continue
                if "max" in gate and float(value) > float(gate["max"]):
                    breaches.append(
                        f"{label} = {value} exceeds ceiling {gate['max']}"
                    )
                if "min" in gate and float(value) < float(gate["min"]):
                    breaches.append(
                        f"{label} = {value} below floor {gate['min']}"
                    )
    return breaches


if __name__ == "__main__":  # pragma: no cover — CLI entry
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(main())
