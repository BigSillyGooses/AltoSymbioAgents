"""
tests/test_pipeline_parallel.py — Perf Phase 5: parallel pipeline execution.

Covers the Phase 5 contract:
  1. Flag off is byte-identical: DECOMPOSITION_PROMPT untouched, the
     parallel prompt is only selected when the flag is on, and specialist
     invocations happen strictly in step order.
  2. depends_on parsing: valid refs normalize to 0-based, forward/self refs
     are dropped, references to dropped steps are dropped, malformed/missing
     depends_on degrades to depending on all earlier steps.
  3. Wave scheduling: independent steps overlap; a dependent step does not
     start until its dependencies' checkpoints reach a terminal state.
  4. Per-backend admission: local steps never overlap (semaphore of 1) while
     claude steps do.
  5. Parallel upstream context contains ONLY transitive dependencies'
     packets — sibling output is withheld.
  6. One raising step neither deadlocks the wave nor starves synthesis.
  7. Saga checkpoint states match sequential execution for the same outcomes.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

from models import RoutingDecision, WorkerResult
from services.pipeline import (
    DECOMPOSITION_PROMPT,
    DECOMPOSITION_PROMPT_PARALLEL,
    PipelineExecutor,
)


# ── Helpers (mirroring tests/test_pipeline.py) ───────────────────────────────


def _seed_agent(in_memory_db, name: str, role: str,
                system_prompt: str = "You are a specialist.",
                model_pref: str = "auto") -> str:
    aid = str(uuid.uuid4())
    in_memory_db.execute(
        "INSERT INTO agents (id, name, description, system_prompt, "
        "model_preference, role, is_builtin, skills, created_at, updated_at) "
        "VALUES (?, ?, '', ?, ?, ?, 0, '[]', '2024-01-01', '2024-01-01')",
        (aid, name, system_prompt, model_pref, role),
    )
    in_memory_db.commit()
    return aid


def _seed_team(in_memory_db, coordinator_id: str, member_ids: list,
               name: str = "T") -> str:
    tid = str(uuid.uuid4())
    in_memory_db.execute(
        "INSERT INTO agent_teams (id, name, description, coordinator_id, "
        "created_at, updated_at) VALUES (?, ?, '', ?, '2024-01-01', "
        "'2024-01-01')",
        (tid, name, coordinator_id),
    )
    for i, mid in enumerate(member_ids):
        in_memory_db.execute(
            "INSERT INTO agent_team_members (team_id, agent_id, role, "
            "sort_order) VALUES (?, ?, 'worker', ?)",
            (tid, mid, i),
        )
    in_memory_db.commit()
    return tid


class ScriptedHub:
    """Recording HubRouter stand-in usable from both execution paths.

    Classifies each invoke as decomposition / specialist / synthesis from
    the message shape, scripts replies per step description, and exposes
    threading.Event gates plus per-backend in-flight counters so tests can
    observe (and control) concurrency.
    """

    SPEC_MARKER = "Your specific task:\n\n"
    SYNTH_MARKER = "Your specialists have completed their sub-tasks"

    def __init__(self, decomp_json: str, backends: dict | None = None,
                 step_replies: dict | None = None,
                 step_sleep_s: float = 0.0,
                 raise_for: set | None = None):
        self.decomp_json = decomp_json
        self.backends = dict(backends or {})       # agent_id → backend
        self.step_replies = dict(step_replies or {})  # description → reply
        self.step_sleep_s = step_sleep_s
        self.raise_for = set(raise_for or ())      # descriptions that raise
        self.entered: dict[str, threading.Event] = {}
        self.release: dict[str, threading.Event] = {}
        self.invocations: list[dict] = []
        self.systems: dict[str, str] = {}          # description → system seen
        self.in_flight: dict[str, int] = {}
        self.max_in_flight: dict[str, int] = {}
        self._lock = threading.Lock()

    def gate(self, description: str) -> None:
        """Make the step with this description block until released."""
        self.entered[description] = threading.Event()
        self.release[description] = threading.Event()

    def release_all(self) -> None:
        for ev in self.release.values():
            ev.set()

    # ── HubRouter surface the pipeline uses ──────────────────────────────

    def route_for_agent(self, agent_id, task):
        return RoutingDecision(
            agent_id=agent_id,
            backend=self.backends.get(agent_id, "claude"),
            score=1.0, reasoning="test", used_fallback=False,
            skill_matched="",
        )

    def _classify(self, messages) -> tuple[str, str]:
        content = messages[-1]["content"] if messages else ""
        if self.SPEC_MARKER in content:
            desc = content.split(self.SPEC_MARKER, 1)[1]
            desc = desc.split("\n\nThe user's original request", 1)[0]
            return "specialist", desc
        if self.SYNTH_MARKER in content:
            return "synthesis", ""
        return "decomposition", ""

    def invoke(self, decision, system, messages, max_tokens=4096,
               on_token=None, agent_role="monolithic"):
        kind, desc = self._classify(messages)
        backend = decision.backend
        with self._lock:
            self.invocations.append({
                "kind": kind, "desc": desc, "system": system,
                "backend": backend,
            })
            if kind == "specialist":
                self.systems[desc] = system
                self.in_flight[backend] = self.in_flight.get(backend, 0) + 1
                self.max_in_flight[backend] = max(
                    self.max_in_flight.get(backend, 0),
                    self.in_flight[backend],
                )
        try:
            if kind == "decomposition":
                return WorkerResult(text=self.decomp_json, backend=backend,
                                    model_name="t")
            if kind == "synthesis":
                return WorkerResult(text="synthesised", backend=backend,
                                    model_name="t")
            if desc in self.entered:
                self.entered[desc].set()
            if desc in self.release:
                assert self.release[desc].wait(timeout=10), \
                    f"release event for {desc!r} timed out"
            if self.step_sleep_s:
                time.sleep(self.step_sleep_s)
            if desc in self.raise_for:
                raise RuntimeError(f"boom: {desc}")
            reply = self.step_replies.get(desc, f"deliverable for {desc}")
            return WorkerResult(text=reply, backend=backend, model_name="t",
                                input_tokens=3, output_tokens=7)
        finally:
            if kind == "specialist":
                with self._lock:
                    self.in_flight[backend] = self.in_flight.get(backend, 1) - 1


def _decomp_json(specs: list[tuple[str, str]], depends: dict | None = None) -> str:
    """Build a decomposition reply. ``specs`` is [(agent_id, description)];
    ``depends`` maps 1-based step position → depends_on list."""
    depends = depends or {}
    return json.dumps([
        {
            "agent_id": aid,
            "agent_name": f"S{i}",
            "description": desc,
            "depends_on": depends.get(i, []),
        }
        for i, (aid, desc) in enumerate(specs, start=1)
    ])


def _run_pipeline_in_thread(executor, team_id, events=None, timeout=20):
    """Start executor.run on a worker thread; returns (thread, result_box)."""
    box: dict = {}

    def _target():
        try:
            box["result"] = executor.run(
                team_id=team_id, user_message="do the thing",
                conversation_id="cid", history=[],
                on_event=(lambda et, d: events.append((et, d)))
                if events is not None else None,
            )
        except Exception as exc:  # surfaced by the test's join assertion
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, box


def _join_or_fail(thread, hub, box, timeout=20):
    thread.join(timeout=timeout)
    if thread.is_alive():
        hub.release_all()  # unwedge so the suite doesn't hang
        pytest.fail("pipeline run did not complete (possible deadlock)")
    if "error" in box:
        raise box["error"]
    return box["result"]


# ── 1. Flag-off byte-identity ────────────────────────────────────────────────


# Verbatim snapshot of the pre-Phase-5 decomposition prompt. The flag-off
# path must keep using EXACTLY this string — any drift is a regression.
_DECOMPOSITION_PROMPT_SNAPSHOT = """You are a team coordinator. Break the user's request into sub-tasks for your specialists.

Available specialists:
{agent_list}

Return ONLY a JSON array. Each element:
{{
  "agent_id": "<id of the specialist>",
  "agent_name": "<name for display>",
  "description": "<what this specialist should do — be specific>"
}}

Rules:
- Order matters: earlier steps execute first, later steps can reference earlier results.
- Use 1 step if the task is simple enough for one specialist.
- Maximum {max_steps} steps.
- Every step must map to one of the listed specialists.
- If the task doesn't need specialisation, return a single step with yourself as the agent.
- Do NOT include a "synthesis" step — that happens automatically after all specialists finish.
"""


class TestFlagOffByteIdentity:
    def test_decomposition_prompt_constant_unchanged(self):
        assert DECOMPOSITION_PROMPT == _DECOMPOSITION_PROMPT_SNAPSHOT

    def test_parallel_prompt_differs_only_by_depends_on(self):
        assert "depends_on" in DECOMPOSITION_PROMPT_PARALLEL
        assert "depends_on" not in DECOMPOSITION_PROMPT

    def test_flag_off_uses_legacy_prompt_and_sequential_order(
        self, in_memory_db, settings,
    ):
        coord = _seed_agent(in_memory_db, "C", "coordinator")
        s1 = _seed_agent(in_memory_db, "S1", "researcher")
        s2 = _seed_agent(in_memory_db, "S2", "writer")
        s3 = _seed_agent(in_memory_db, "S3", "auditor")
        team = _seed_team(in_memory_db, coord, [s1, s2, s3])

        # depends_on present in the coordinator output but the flag is off:
        # it must be ignored and execution must stay strictly sequential.
        hub = ScriptedHub(_decomp_json(
            [(s1, "task A"), (s2, "task B"), (s3, "task C")],
            depends={3: [1, 2]},
        ))
        executor = PipelineExecutor(hub, settings)
        result = executor.run(
            team_id=team, user_message="do the thing",
            conversation_id="cid", history=[],
        )

        kinds = [(i["kind"], i["desc"]) for i in hub.invocations]
        assert kinds == [
            ("decomposition", ""),
            ("specialist", "task A"),
            ("specialist", "task B"),
            ("specialist", "task C"),
            ("synthesis", ""),
        ]
        # The decomposition system prompt is the legacy one (no depends_on).
        assert hub.invocations[0]["system"] == DECOMPOSITION_PROMPT.format(
            agent_list=hub.invocations[0]["system"]
            .split("Available specialists:\n", 1)[1]
            .split("\n\nReturn ONLY", 1)[0],
            max_steps=6,
        )
        assert "depends_on" not in hub.invocations[0]["system"]
        assert len(result.steps) == 3

    def test_flag_on_selects_parallel_prompt(self, in_memory_db, settings):
        settings.set("pipeline_parallel_enabled", "1")
        coord = _seed_agent(in_memory_db, "C", "coordinator")
        s1 = _seed_agent(in_memory_db, "S1", "researcher")
        team = _seed_team(in_memory_db, coord, [s1])

        hub = ScriptedHub(_decomp_json([(s1, "task A")]))
        executor = PipelineExecutor(hub, settings)
        executor.run(
            team_id=team, user_message="do the thing",
            conversation_id="cid", history=[],
        )
        assert '"depends_on"' in hub.invocations[0]["system"]


# ── 2. depends_on parsing ────────────────────────────────────────────────────


class TestDependsOnParsing:
    @pytest.fixture
    def executor(self, settings):
        return PipelineExecutor(MagicMock(), settings)

    MEMBERS = [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}]
    COORD = {"id": "c1"}

    def _raw(self, *items):
        return json.dumps(list(items))

    def _item(self, n: int, **extra):
        item = {"agent_id": f"a{n}", "agent_name": f"A{n}",
                "description": f"task {n}"}
        item.update(extra)
        return item

    def test_valid_refs_normalize_to_zero_based(self, executor):
        subs = executor._parse_subtasks(
            self._raw(
                self._item(1, depends_on=[]),
                self._item(2, depends_on=[]),
                self._item(3, depends_on=[1, 2]),
            ),
            self.MEMBERS, self.COORD, parallel=True,
        )
        assert [s.depends_on for s in subs] == [[], [], [0, 1]]

    def test_forward_ref_dropped(self, executor):
        subs = executor._parse_subtasks(
            self._raw(
                self._item(1, depends_on=[2]),
                self._item(2, depends_on=[]),
            ),
            self.MEMBERS, self.COORD, parallel=True,
        )
        assert subs[0].depends_on == []

    def test_self_ref_dropped(self, executor):
        subs = executor._parse_subtasks(
            self._raw(
                self._item(1, depends_on=[]),
                self._item(2, depends_on=[2, 1]),
            ),
            self.MEMBERS, self.COORD, parallel=True,
        )
        assert subs[1].depends_on == [0]

    def test_malformed_depends_on_means_all_earlier(self, executor):
        subs = executor._parse_subtasks(
            self._raw(
                self._item(1, depends_on=[]),
                self._item(2, depends_on=[]),
                self._item(3, depends_on="after the budget step"),
            ),
            self.MEMBERS, self.COORD, parallel=True,
        )
        assert subs[2].depends_on == [0, 1]

    def test_missing_depends_on_means_all_earlier(self, executor):
        subs = executor._parse_subtasks(
            self._raw(
                self._item(1, depends_on=[]),
                self._item(2),  # no depends_on key at all
            ),
            self.MEMBERS, self.COORD, parallel=True,
        )
        assert subs[1].depends_on == [0]

    def test_non_int_entries_dropped(self, executor):
        subs = executor._parse_subtasks(
            self._raw(
                self._item(1, depends_on=[]),
                self._item(2, depends_on=[]),
                self._item(3, depends_on=[1, "2", 2.5, True]),
            ),
            self.MEMBERS, self.COORD, parallel=True,
        )
        assert subs[2].depends_on == [0]

    def test_ref_to_dropped_step_is_dropped(self, executor):
        subs = executor._parse_subtasks(
            self._raw(
                {"agent_id": "ghost", "agent_name": "G",
                 "description": "dropped", "depends_on": []},
                self._item(2, depends_on=[1]),
            ),
            self.MEMBERS, self.COORD, parallel=True,
        )
        assert len(subs) == 1
        assert subs[0].depends_on == []

    def test_flag_off_ignores_depends_on(self, executor):
        subs = executor._parse_subtasks(
            self._raw(
                self._item(1, depends_on=[]),
                self._item(2, depends_on=[1]),
            ),
            self.MEMBERS, self.COORD, parallel=False,
        )
        assert [s.depends_on for s in subs] == [[], []]


# ── 3. Wave scheduling ───────────────────────────────────────────────────────


class TestWaveScheduling:
    def test_independent_steps_overlap_dependent_waits_for_commit(
        self, in_memory_db, settings,
    ):
        settings.set("pipeline_parallel_enabled", "1")
        coord = _seed_agent(in_memory_db, "C", "coordinator")
        s1 = _seed_agent(in_memory_db, "S1", "researcher")
        s2 = _seed_agent(in_memory_db, "S2", "writer")
        s3 = _seed_agent(in_memory_db, "S3", "auditor")
        team = _seed_team(in_memory_db, coord, [s1, s2, s3])

        hub = ScriptedHub(_decomp_json(
            [(s1, "task A"), (s2, "task B"), (s3, "task C")],
            depends={3: [1, 2]},
        ))
        hub.gate("task A")
        hub.gate("task B")
        hub.gate("task C")
        executor = PipelineExecutor(hub, settings)

        events: list = []
        thread, box = _run_pipeline_in_thread(executor, team, events=events)
        try:
            # Both independent steps must be in flight CONCURRENTLY:
            # each entered the hub before either was released.
            assert hub.entered["task A"].wait(timeout=10)
            assert hub.entered["task B"].wait(timeout=10)
            assert not hub.entered["task C"].is_set()

            # Releasing only A is not enough — C needs B's commit too.
            hub.release["task A"].set()
            assert not hub.entered["task C"].wait(timeout=0.5)

            hub.release["task B"].set()
            assert hub.entered["task C"].wait(timeout=10)
            hub.release["task C"].set()
        finally:
            result = _join_or_fail(thread, hub, box)

        assert len(result.steps) == 3
        assert [s["step"] for s in result.steps] == [1, 2, 3]
        # Every step event payload carries its step index.
        started = [d for et, d in events if et == "pipeline_step_started"]
        assert sorted(d["step"] for d in started) == [1, 2, 3]
        # When C entered the hub, its dependencies' checkpoints had already
        # committed (C only became runnable on their terminal state).
        rows = in_memory_db.fetchall(
            "SELECT step_index, state FROM workflow_checkpoints "
            "WHERE workflow_id = ?", (result.pipeline_id,),
        )
        assert {r["state"] for r in rows} == {"committed"}
        assert len(rows) == 3


# ── 4. Per-backend admission control ─────────────────────────────────────────


class TestBackendAdmission:
    def test_local_serialized_while_claude_overlaps(
        self, in_memory_db, settings,
    ):
        settings.set("pipeline_parallel_enabled", "1")
        settings.set("pipeline_max_concurrency", "4")
        settings.set("pipeline_local_concurrency", "1")
        coord = _seed_agent(in_memory_db, "C", "coordinator")
        l1 = _seed_agent(in_memory_db, "L1", "researcher", model_pref="local")
        l2 = _seed_agent(in_memory_db, "L2", "writer", model_pref="local")
        c1 = _seed_agent(in_memory_db, "C1", "auditor", model_pref="claude")
        c2 = _seed_agent(in_memory_db, "C2", "planner", model_pref="claude")
        team = _seed_team(in_memory_db, coord, [l1, l2, c1, c2])

        hub = ScriptedHub(
            _decomp_json([
                (l1, "local one"), (l2, "local two"),
                (c1, "claude one"), (c2, "claude two"),
            ]),
            backends={l1: "local", l2: "local", c1: "claude", c2: "claude"},
            step_sleep_s=0.15,
        )
        hub.gate("claude one")
        hub.gate("claude two")
        executor = PipelineExecutor(hub, settings)

        thread, box = _run_pipeline_in_thread(executor, team)
        try:
            # Both claude steps in flight at once (remote semaphore allows it)
            assert hub.entered["claude one"].wait(timeout=10)
            assert hub.entered["claude two"].wait(timeout=10)
            hub.release["claude one"].set()
            hub.release["claude two"].set()
        finally:
            result = _join_or_fail(thread, hub, box)

        assert len(result.steps) == 4
        # The local semaphore (1 permit) never let local calls overlap.
        assert hub.max_in_flight.get("local", 0) == 1
        assert hub.max_in_flight.get("claude", 0) == 2


# ── 5. Upstream context = transitive dependencies only ──────────────────────


class TestParallelUpstreamContext:
    def test_dependent_step_sees_only_its_dependency(
        self, in_memory_db, settings,
    ):
        settings.set("pipeline_parallel_enabled", "1")
        coord = _seed_agent(in_memory_db, "C", "coordinator")
        s1 = _seed_agent(in_memory_db, "S1", "researcher")
        s2 = _seed_agent(in_memory_db, "S2", "writer")
        s3 = _seed_agent(in_memory_db, "S3", "auditor")
        team = _seed_team(in_memory_db, coord, [s1, s2, s3])

        # C depends ONLY on A; B is an independent sibling.
        hub = ScriptedHub(
            _decomp_json(
                [(s1, "task A"), (s2, "task B"), (s3, "task C")],
                depends={3: [1]},
            ),
            step_replies={
                "task A": "ARTIFACT-ALPHA findings",
                "task B": "ARTIFACT-BRAVO findings",
            },
        )
        hub.gate("task A")
        executor = PipelineExecutor(hub, settings)

        events: list = []
        thread, box = _run_pipeline_in_thread(executor, team, events=events)
        try:
            assert hub.entered["task A"].wait(timeout=10)
            # Let B FINISH before A so the sibling packet exists by the time
            # C builds its context — its absence is then a real exclusion,
            # not an accident of timing.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                done_steps = {d["step"] for et, d in events
                              if et == "pipeline_step_complete"}
                if 2 in done_steps:
                    break
                time.sleep(0.02)
            else:
                pytest.fail("step B never completed")
            hub.release["task A"].set()
        finally:
            result = _join_or_fail(thread, hub, box)

        assert len(result.steps) == 3
        system_c = hub.systems["task C"]
        assert "## Results from earlier pipeline steps" in system_c
        assert "ARTIFACT-ALPHA" in system_c        # its dependency
        assert "ARTIFACT-BRAVO" not in system_c    # the sibling — withheld
        # Independent steps get no upstream context at all.
        assert "Results from earlier pipeline steps" not in hub.systems["task A"]
        assert "Results from earlier pipeline steps" not in hub.systems["task B"]


# ── 6. Failure isolation ─────────────────────────────────────────────────────


class TestFailureDoesNotDeadlock:
    def test_raising_step_completes_wave_and_synthesis(
        self, in_memory_db, settings,
    ):
        settings.set("pipeline_parallel_enabled", "1")
        coord = _seed_agent(in_memory_db, "C", "coordinator")
        s1 = _seed_agent(in_memory_db, "S1", "researcher")
        s2 = _seed_agent(in_memory_db, "S2", "writer")
        s3 = _seed_agent(in_memory_db, "S3", "auditor")
        team = _seed_team(in_memory_db, coord, [s1, s2, s3])

        # A raises on every attempt; C depends on A and must STILL run.
        hub = ScriptedHub(
            _decomp_json(
                [(s1, "task A"), (s2, "task B"), (s3, "task C")],
                depends={3: [1]},
            ),
            raise_for={"task A"},
        )
        executor = PipelineExecutor(hub, settings)

        thread, box = _run_pipeline_in_thread(executor, team)
        result = _join_or_fail(thread, hub, box, timeout=20)

        assert len(result.steps) == 3
        by_step = {s["step"]: s for s in result.steps}
        assert by_step[1]["validation_passed"] is False
        assert by_step[2]["validation_passed"] is True
        assert by_step[3]["validation_passed"] is True
        # Synthesis ran and received all three packets (incl. the error one).
        assert result.synthesis == "synthesised"
        assert len(result.handoffs) == 3
        assert result.handoffs[0].artifact.startswith("[Error:")
        # The dependent step DID execute despite its dependency's failure.
        specialist_descs = [i["desc"] for i in hub.invocations
                            if i["kind"] == "specialist"]
        assert "task C" in specialist_descs


# ── 7. Saga state parity with sequential ─────────────────────────────────────


class TestSagaStateParity:
    def _run(self, in_memory_db, settings, parallel: bool):
        # Agent names are unique per mode — agents.name carries a UNIQUE
        # constraint and both modes seed into the same in-memory DB.
        tag = "par" if parallel else "seq"
        coord = _seed_agent(in_memory_db, f"C-{tag}", "coordinator")
        s1 = _seed_agent(in_memory_db, f"S1-{tag}", "researcher")
        s2 = _seed_agent(in_memory_db, f"S2-{tag}", "writer")
        team = _seed_team(in_memory_db, coord, [s1, s2], name=f"T-{tag}")

        settings.set("pipeline_parallel_enabled", "1" if parallel else "0")
        # "task bad" returns an empty artifact on every attempt → structural
        # validation fails, retries exhaust, the checkpoint's final state is
        # rolled_back. "task good" commits first try.
        hub = ScriptedHub(
            _decomp_json([(s1, "task bad"), (s2, "task good")]),
            step_replies={"task bad": "", "task good": "solid output"},
        )
        executor = PipelineExecutor(hub, settings)
        result = executor.run(
            team_id=team, user_message="do the thing",
            conversation_id="cid", history=[],
        )
        rows = in_memory_db.fetchall(
            "SELECT step_index, state, retry_count FROM workflow_checkpoints "
            "WHERE workflow_id = ? ORDER BY step_index",
            (result.pipeline_id,),
        )
        return [(r["step_index"], r["state"], r["retry_count"]) for r in rows]

    def test_parallel_leaves_same_checkpoint_states_as_sequential(
        self, in_memory_db, settings,
    ):
        sequential = self._run(in_memory_db, settings, parallel=False)
        parallel = self._run(in_memory_db, settings, parallel=True)
        assert sequential == parallel
        assert sequential == [
            (0, "rolled_back", 3),  # MAX_RETRIES_PER_STEP exhausted
            (1, "committed", 0),
        ]
