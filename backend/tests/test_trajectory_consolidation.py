"""
tests/test_trajectory_consolidation.py — Perf Phase 6: trajectory learning v2.

Graded verdicts (``quality_score``), consolidation of raw trajectories into
``routing_hints``, the ``hint_table`` recall path, the HubRouter hint bias,
and the TaskRouter fail-open hook. Reuses the deterministic bag-of-words
embedder from test_trajectory_store.py so everything runs against real
sqlite-vec without downloading the fastembed model.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tests.test_trajectory_store import EMBED_DIM, _deterministic_embed, _record


@pytest.fixture
def vector_env(in_memory_db, monkeypatch):
    """Enable the vector store with the deterministic embedder."""
    from services import semantic_search
    monkeypatch.setattr(semantic_search, "_embed_fn", _deterministic_embed)
    monkeypatch.setattr(semantic_search, "_embed_dim", EMBED_DIM)
    monkeypatch.setattr(semantic_search, "_initialized", True)
    return in_memory_db


# ── quality_score mapping ─────────────────────────────────────────────────────


class TestQualityScore:
    def _q(self, **fields):
        from services import trajectory_store
        base = {
            "quality_verdict": "success",
            "had_error": 0,
            "response_empty": 0,
            "route_reasoning": "skill-match",
        }
        base.update(fields)
        return trajectory_store.quality_score(base)

    def test_error_and_empty_are_zero(self):
        assert self._q(had_error=1) == 0.0
        assert self._q(response_empty=1) == 0.0
        # Error wins even over a "success" verdict.
        assert self._q(quality_verdict="success", had_error=1) == 0.0

    def test_mast_classes(self):
        # Specification failures (1.x) → 0.1
        for code in ("1.1", "1.3", "1.5"):
            assert self._q(quality_verdict=code) == 0.1
        # Inter-agent misalignment (2.x) → 0.25
        for code in ("2.1", "2.3", "2.6"):
            assert self._q(quality_verdict=code) == 0.25
        # Verification failures (3.x) → 0.35
        for code in ("3.1", "3.2", "3.3"):
            assert self._q(quality_verdict=code) == 0.35

    def test_success_is_one(self):
        assert self._q(quality_verdict="success") == 1.0
        # Legacy empty verdict counts as success (matches is_success(None)).
        assert self._q(quality_verdict=None) == 1.0

    def test_escalated_success_is_damped(self):
        # The reasoning string TaskRouter writes on a UAR escalation.
        assert self._q(
            route_reasoning="low confidence (45%) — escalated to Claude",
        ) == 0.8
        # Damping only applies to successes — an error stays 0.
        assert self._q(
            route_reasoning="low confidence (45%) — escalated to Claude",
            had_error=1,
        ) == 0.0

    def test_unknown_verdict_is_neutral(self):
        assert self._q(quality_verdict="partial") == 0.5
        assert self._q(quality_verdict="4.9") == 0.5  # not a MAST code

    def test_record_writes_quality_score(self, vector_env):
        import db
        tid = _record(task="summarize the quarterly financial report")
        row = db.fetchone(
            "SELECT quality_score FROM trajectories WHERE id = ?", (tid,))
        assert row["quality_score"] == 1.0

    def test_lazy_backfill_on_read(self, vector_env):
        """Pre-Phase-6 rows (quality_score NULL) get scored on first read."""
        import db
        from services import trajectory_store
        tid = _record(task="audit the security of the auth module",
                      verdict="2.3")
        db.execute("UPDATE trajectories SET quality_score = NULL WHERE id = ?",
                   (tid,))
        db.commit()

        hits = trajectory_store.find_similar(
            "audit the security of the auth module", top_k=3, min_sim=0.0)
        assert hits and hits[0]["quality"] == 0.25
        row = db.fetchone(
            "SELECT quality_score FROM trajectories WHERE id = ?", (tid,))
        assert row["quality_score"] == 0.25  # persisted, not just computed


# ── bias_for / bias_table binary-outcome equivalence ──────────────────────────


class TestBinaryBiasUnchanged:
    def _seed_binary(self):
        for _ in range(3):
            _record(task="audit the security of the auth module",
                    agent="agent-good", verdict="success")
        for _ in range(3):
            _record(task="audit the security of the auth module",
                    agent="agent-bad", verdict="2.3", had_error=True)

    def test_bias_for_matches_pre_change_success_rate(self, vector_env):
        """For binary rows the quality-weighted mean must equal the old
        similarity-weighted success rate exactly."""
        from services import trajectory_store
        self._seed_binary()
        query = "audit the security of the login module"

        for agent in ("agent-good", "agent-bad"):
            similar = trajectory_store.find_similar(
                query, agent_id=agent, top_k=5, min_sim=0.0)
            # Pre-change semantics: numerator counts only success rows.
            legacy = round(
                sum(t["score"] for t in similar if t["success"])
                / sum(t["score"] for t in similar), 3)
            assert trajectory_store.bias_for(
                query, agent, min_sim=0.0) == legacy

        assert trajectory_store.bias_for(query, "agent-good", min_sim=0.0) >= 0.9
        assert trajectory_store.bias_for(query, "agent-bad", min_sim=0.0) <= 0.1

    def test_bias_table_matches_pre_change_success_rate(self, vector_env):
        from services import trajectory_store
        self._seed_binary()
        query = "audit the security of the login module"
        table = trajectory_store.bias_table(query, top_k=5, min_sim=0.0)

        similar = trajectory_store.find_similar(
            query, top_k=20, min_sim=0.0)
        for agent in ("agent-good", "agent-bad"):
            rows = [t for t in similar if t["agent_id"] == agent]
            legacy = round(
                sum(t["score"] for t in rows if t["success"])
                / sum(t["score"] for t in rows), 3)
            assert table[agent] == legacy

    def test_graded_rows_carry_partial_credit(self, vector_env):
        """A MAST verdict WITHOUT an error is graded, not zeroed."""
        from services import trajectory_store
        for _ in range(3):
            _record(task="verify the report appendix tables",
                    agent="agent-v", verdict="3.2", had_error=False)
        bias = trajectory_store.bias_for(
            "verify the report appendix tables", "agent-v", min_sim=0.0)
        assert bias == 0.35  # all members are the 3.x class


# ── Consolidation: cluster / merge / prune ────────────────────────────────────


def _hint_rows():
    import db
    return db.fetchall(
        "SELECT id, exemplar_text, agent_id, backend, quality, support_count "
        "FROM routing_hints ORDER BY created_at, id")


class TestConsolidate:
    def test_cluster_forms_at_min_cluster(self, vector_env):
        import db
        from services import trajectory_store
        for _ in range(3):
            _record(task="summarize the quarterly financial report",
                    agent="agent-a", verdict="success")

        n = trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})
        assert n == 3
        hints = _hint_rows()
        assert len(hints) == 1
        assert hints[0]["agent_id"] == "agent-a"
        assert hints[0]["backend"] == "claude"
        assert hints[0]["support_count"] == 3
        assert hints[0]["quality"] == 1.0
        assert hints[0]["exemplar_text"] == (
            "summarize the quarterly financial report")
        # The exemplar is embedded into the hint vec0 table.
        assert db.fetchone(
            "SELECT 1 FROM vec_routing_hints_map WHERE hint_id = ?",
            (hints[0]["id"],)) is not None

    def test_below_min_cluster_no_hint(self, vector_env):
        import db
        from services import trajectory_store
        for _ in range(2):
            _record(task="summarize the quarterly financial report",
                    agent="agent-a", verdict="success")

        n = trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})
        assert n == 0
        assert _hint_rows() == []
        # Rows stay unconsolidated AND keep their vectors for a later pass.
        assert db.fetchone(
            "SELECT COUNT(*) AS c FROM trajectories "
            "WHERE COALESCE(consolidated, 0) = 0")["c"] == 2
        assert db.fetchone(
            "SELECT COUNT(*) AS c FROM vec_trajectories_map")["c"] == 2

    def test_merge_updates_running_mean_and_support(self, vector_env):
        from services import trajectory_store
        for _ in range(3):
            _record(task="summarize the quarterly financial report",
                    agent="agent-a", verdict="success")
        trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})
        assert _hint_rows()[0]["quality"] == 1.0

        # A later failed turn on the same task family merges in.
        _record(task="summarize the quarterly financial report",
                agent="agent-a", verdict="2.3", had_error=True)  # quality 0.0
        n = trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})
        assert n == 1
        hints = _hint_rows()
        assert len(hints) == 1
        assert hints[0]["support_count"] == 4
        # Running mean: (1.0 * 3 + 0.0) / 4
        assert hints[0]["quality"] == pytest.approx(0.75)

    def test_same_task_different_agent_does_not_merge(self, vector_env):
        from services import trajectory_store
        for _ in range(3):
            _record(task="summarize the quarterly financial report",
                    agent="agent-a", verdict="success")
        trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})

        # Same task text, DIFFERENT agent: must not fold into agent-a's hint
        # (and 2 rows are below the cluster minimum, so no new hint either).
        for _ in range(2):
            _record(task="summarize the quarterly financial report",
                    agent="agent-b", verdict="2.3", had_error=True)
        n = trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})
        assert n == 0
        hints = _hint_rows()
        assert len(hints) == 1
        assert hints[0]["agent_id"] == "agent-a"
        assert hints[0]["support_count"] == 3
        assert hints[0]["quality"] == 1.0

    def test_same_task_different_backend_does_not_merge(self, vector_env):
        from services import trajectory_store
        for _ in range(3):
            _record(task="summarize the quarterly financial report",
                    agent="agent-a", verdict="success")
        trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})

        # _record always seeds backend="claude"; record a local-backend row.
        trajectory_store.record(
            conversation_id="c1", turn_id="t-local",
            task_text="summarize the quarterly financial report",
            agent_id="agent-a", skill_matched="research", backend="local",
            model_name="local-model", routing_score=0.7,
            route_reasoning="test", quality_verdict="success",
            had_error=False, response_empty=False, tokens_in=10, tokens_out=20,
        )
        n = trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})
        assert n == 0
        assert _hint_rows()[0]["support_count"] == 3

    def test_vec_rows_dropped_but_audit_rows_remain(self, vector_env):
        import db
        from services import trajectory_store
        ids = [
            _record(task="summarize the quarterly financial report",
                    agent="agent-a", verdict="success")
            for _ in range(3)
        ]
        trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})

        for tid in ids:
            # trajectories row survives for audit, marked consolidated…
            row = db.fetchone(
                "SELECT consolidated FROM trajectories WHERE id = ?", (tid,))
            assert row is not None and row["consolidated"] == 1
            # …but its vector + map entry are gone (bounds the KNN).
            assert db.fetchone(
                "SELECT 1 FROM vec_trajectories_map WHERE trajectory_id = ?",
                (tid,)) is None
        assert db.fetchone("SELECT COUNT(*) AS c FROM vec_trajectories")["c"] == 0
        # Consolidated rows no longer feed the raw bias path.
        assert trajectory_store.bias_table(
            "summarize the quarterly financial report", min_sim=0.0) == {}

    def test_prune_stale_low_support_hint(self, vector_env):
        import db
        from services import trajectory_store
        db.execute(
            "INSERT INTO routing_hints (id, exemplar_text, agent_id, backend, "
            "skill, quality, support_count, created_at, last_seen) "
            "VALUES ('stale-1', 'old task family text', 'agent-a', 'claude', "
            "'research', 0.9, 2, '2020-01-01T00:00:00+00:00', "
            "'2020-01-01T00:00:00+00:00')")
        db.commit()
        trajectory_store._embed_hint("stale-1", "old task family text")

        trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3,
                      "trajectory_hint_max_age_days": 90})
        assert _hint_rows() == []
        assert db.fetchone(
            "SELECT 1 FROM vec_routing_hints_map WHERE hint_id = 'stale-1'"
        ) is None

    def test_prune_no_signal_hint(self, vector_env):
        import db
        from services import trajectory_store
        now = "2099-01-01T00:00:00+00:00"  # fresh — age prune can't fire
        for hid, quality, support in (
            ("noise-1", 0.5, 3),    # no-signal, low support → pruned
            ("keep-1", 0.5, 5),     # no-signal but well-supported → kept
            ("keep-2", 0.9, 3),     # clear signal → kept
        ):
            db.execute(
                "INSERT INTO routing_hints (id, exemplar_text, agent_id, "
                "backend, skill, quality, support_count, created_at, last_seen) "
                "VALUES (?, ?, 'agent-a', 'claude', 'research', ?, ?, ?, ?)",
                (hid, f"task family {hid}", quality, support, now, now))
        db.commit()

        trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})
        remaining = {h["id"] for h in _hint_rows()}
        assert remaining == {"keep-1", "keep-2"}

    def test_inline_trigger_from_record(self, vector_env, monkeypatch):
        """record() runs consolidate() best-effort every interval turns."""
        from services import trajectory_store

        class _S(dict):
            def get(self, k, d=None):
                return super().get(k, d)

        monkeypatch.setattr(trajectory_store, "_settings_obj", _S({
            "trajectory_consolidation_enabled": True,
            "trajectory_consolidation_interval_turns": 3,
            "trajectory_consolidation_min_cluster": 3,
        }))
        monkeypatch.setattr(trajectory_store, "_records_since_consolidate", 0)

        for _ in range(3):
            _record(task="summarize the quarterly financial report",
                    agent="agent-a", verdict="success")
        assert len(_hint_rows()) == 1  # third record() hit the interval

    def test_inline_trigger_failure_never_breaks_record(
        self, vector_env, monkeypatch,
    ):
        from services import trajectory_store

        class _S(dict):
            def get(self, k, d=None):
                return super().get(k, d)

        monkeypatch.setattr(trajectory_store, "_settings_obj", _S({
            "trajectory_consolidation_enabled": True,
            "trajectory_consolidation_interval_turns": 1,
        }))
        monkeypatch.setattr(trajectory_store, "_records_since_consolidate", 0)
        monkeypatch.setattr(
            trajectory_store, "consolidate",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

        tid = _record(task="summarize the quarterly financial report")
        assert tid is not None  # recording survived the consolidation failure


# ── hint_table ────────────────────────────────────────────────────────────────


class TestHintTable:
    def _seed_hints(self):
        from services import trajectory_store
        for _ in range(3):
            _record(task="summarize the quarterly financial report",
                    agent="agent-good", verdict="success")
        for _ in range(3):
            _record(task="summarize the quarterly financial report",
                    agent="agent-bad", verdict="2.3", had_error=True)
        trajectory_store.consolidate(
            settings={"trajectory_consolidation_min_cluster": 3})

    def test_returns_expected_agents_and_qualities(self, vector_env):
        from services import trajectory_store
        self._seed_hints()
        table = trajectory_store.hint_table(
            "summarize the quarterly financial report", top_k=3, min_sim=0.0)
        assert table == {
            "agent-good": (1.0, 3),
            "agent-bad": (0.0, 3),
        }

    def test_min_sim_filters(self, vector_env):
        from services import trajectory_store
        self._seed_hints()
        assert trajectory_store.hint_table(
            "a totally unrelated xyzzy prompt", top_k=3, min_sim=0.9) == {}

    def test_backend_filter(self, vector_env):
        from services import trajectory_store
        self._seed_hints()  # backend="claude" for every seeded hint
        assert trajectory_store.hint_table(
            "summarize the quarterly financial report",
            top_k=3, min_sim=0.0, backend="local") == {}
        local_free = trajectory_store.hint_table(
            "summarize the quarterly financial report",
            top_k=3, min_sim=0.0, backend="claude")
        assert set(local_free) == {"agent-good", "agent-bad"}

    def test_unavailable_vector_store_returns_empty(self, in_memory_db):
        from services import trajectory_store
        assert trajectory_store.hint_table("anything") == {}


# ── HubRouter: hint bias ──────────────────────────────────────────────────────


def _seed_agent(in_memory_db, name: str, skills: list[dict]) -> str:
    import uuid as _uuid
    aid = str(_uuid.uuid4())
    in_memory_db.execute(
        "INSERT INTO agents (id, name, description, system_prompt, "
        "model_preference, role, is_builtin, skills, created_at, updated_at) "
        "VALUES (?, ?, '', 'sys', 'auto', 'researcher', 0, ?, "
        "'2024-01-01', '2024-01-01')",
        (aid, name, json.dumps(skills)),
    )
    in_memory_db.commit()
    return aid


class TestHubHintBias:
    def _hub(self, settings):
        from services.hub_router import HubRouter
        return HubRouter(MagicMock(), MagicMock(), settings)

    def _task(self):
        from models import TaskDescriptor
        return TaskDescriptor(text="research the topic",
                              required_skills=("researcher",),
                              required_scopes=("read",))

    def test_hint_bias_in_reasoning_and_support_damped(
        self, in_memory_db, settings, monkeypatch,
    ):
        from services import trajectory_store
        a = _seed_agent(in_memory_db, "A",
                        [{"name": "researcher", "scopes": ["read"]}])
        b = _seed_agent(in_memory_db, "B",
                        [{"name": "researcher", "scopes": ["read"]}])
        settings.set("trajectory_consolidation_enabled", True)

        monkeypatch.setattr(
            trajectory_store, "hint_table",
            lambda *args, **kw: {b: (1.0, 7), a: (0.0, 7)})
        raw_calls = {"n": 0}
        monkeypatch.setattr(
            trajectory_store, "bias_table",
            lambda *args, **kw: raw_calls.__setitem__("n", raw_calls["n"] + 1) or {})

        hub = self._hub(settings)
        decision = hub.route(self._task())

        assert decision.agent_id == b
        # weight 0.4 × (1.0 − 0.5) × min(1, 7/5) = +0.20 (full support).
        assert "hint bias +0.20 (support 7)" in decision.reasoning
        assert "trajectory bias" not in decision.reasoning

    def test_hint_delta_damped_below_full_support(
        self, in_memory_db, settings, monkeypatch,
    ):
        from services import trajectory_store
        a = _seed_agent(in_memory_db, "A",
                        [{"name": "researcher", "scopes": ["read"]}])
        b = _seed_agent(in_memory_db, "B",
                        [{"name": "researcher", "scopes": ["read"]}])
        settings.set("trajectory_consolidation_enabled", True)
        monkeypatch.setattr(
            trajectory_store, "hint_table",
            lambda *args, **kw: {b: (1.0, 2), a: (0.0, 2)})  # support 2 → damp 0.4

        hub = self._hub(settings)
        decision = hub.route(self._task())
        # 0.4 × 0.5 × (2/5) = +0.08 — visibly smaller than the full +0.20.
        assert "hint bias +0.08 (support 2)" in decision.reasoning

    def test_agents_without_hint_fall_back_to_raw_bias(
        self, in_memory_db, settings, monkeypatch,
    ):
        from services import trajectory_store
        a = _seed_agent(in_memory_db, "A",
                        [{"name": "researcher", "scopes": ["read"]}])
        b = _seed_agent(in_memory_db, "B",
                        [{"name": "researcher", "scopes": ["read"]}])
        settings.set("trajectory_consolidation_enabled", True)
        settings.set("trajectory_guidance_enabled", True)

        monkeypatch.setattr(
            trajectory_store, "hint_table", lambda *args, **kw: {})
        monkeypatch.setattr(
            trajectory_store, "bias_table",
            lambda *args, **kw: {b: 1.0, a: 0.0})

        hub = self._hub(settings)
        decision = hub.route(self._task())
        assert decision.agent_id == b
        assert "trajectory bias" in decision.reasoning

    def test_flag_off_route_is_byte_identical(
        self, in_memory_db, settings, monkeypatch,
    ):
        """With consolidation off, hints in the DB must not change route():
        decision fields equal the pre-hint decision and hint_table is never
        consulted."""
        from services import trajectory_store
        _seed_agent(in_memory_db, "A",
                    [{"name": "researcher", "scopes": ["read"]}])
        hub = self._hub(settings)

        before = hub.route(self._task())

        def _boom(*args, **kw):
            raise AssertionError("hint_table consulted while flag off")
        monkeypatch.setattr(trajectory_store, "hint_table", _boom)
        # Even a populated hint store must be invisible while the flag is off.
        in_memory_db.execute(
            "INSERT INTO routing_hints (id, exemplar_text, agent_id, backend, "
            "skill, quality, support_count, created_at, last_seen) "
            "VALUES ('h1', 'research the topic', 'someone-else', 'claude', "
            "'researcher', 1.0, 9, '2024-01-01', '2024-01-01')")
        in_memory_db.commit()

        after = hub.route(self._task())
        assert (after.agent_id, after.backend, after.score, after.reasoning,
                after.skill_matched, after.used_fallback) == (
            before.agent_id, before.backend, before.score, before.reasoning,
            before.skill_matched, before.used_fallback)
        assert "hint bias" not in after.reasoning


# ── TaskRouter hook ───────────────────────────────────────────────────────────


def _local_classifier(reply: dict) -> MagicMock:
    local = MagicMock()
    local.is_available.return_value = True
    local.chat.return_value = json.dumps(reply)
    return local


class TestTaskRouterHook:
    def test_hook_fail_open_on_exception(self, in_memory_db, settings, monkeypatch):
        """hint_table raising must leave classification unaffected."""
        from services import trajectory_store
        from services.router import TaskRouter
        settings.set("trajectory_consolidation_enabled", True)

        def _boom(*args, **kw):
            raise RuntimeError("hint store unavailable")
        monkeypatch.setattr(trajectory_store, "hint_table", _boom)

        router = TaskRouter(_local_classifier({
            "complexity": "simple", "model": "local",
            "confidence": 0.9, "needs_context": False, "reasoning": "easy",
        }), settings)
        route = router.classify("what time is it")
        assert route.model == "local"
        assert route.confidence == 0.9

    def test_strong_hint_keeps_low_confidence_local(
        self, in_memory_db, settings, monkeypatch,
    ):
        from services import trajectory_store
        from services.router import TaskRouter
        settings.set("trajectory_consolidation_enabled", True)
        monkeypatch.setattr(
            trajectory_store, "hint_table",
            lambda *args, **kw: {"agent-a": (0.9, 7)})

        router = TaskRouter(_local_classifier({
            "complexity": "simple", "model": "local",
            "confidence": 0.45, "needs_context": False, "reasoning": "hmm",
        }), settings)
        route = router.classify("what time is it")
        # 0.45 < ESCALATION_THRESHOLD would normally escalate to Claude.
        assert route.model == "local"

    def test_weak_hint_biases_toward_escalation(
        self, in_memory_db, settings, monkeypatch,
    ):
        from services import trajectory_store
        from services.router import TaskRouter
        settings.set("trajectory_consolidation_enabled", True)
        monkeypatch.setattr(
            trajectory_store, "hint_table",
            lambda *args, **kw: {"agent-a": (0.1, 6)})

        router = TaskRouter(_local_classifier({
            "complexity": "simple", "model": "local",
            "confidence": 0.9, "needs_context": False, "reasoning": "easy",
        }), settings)
        route = router.classify("what time is it")
        assert route.model == "claude"
        assert "negative routing hint" in route.reasoning

    def test_flag_off_never_consults_hints(self, in_memory_db, settings, monkeypatch):
        from services import trajectory_store
        from services.router import TaskRouter

        def _boom(*args, **kw):
            raise AssertionError("hint_table consulted while flag off")
        monkeypatch.setattr(trajectory_store, "hint_table", _boom)

        router = TaskRouter(_local_classifier({
            "complexity": "simple", "model": "local",
            "confidence": 0.45, "needs_context": False, "reasoning": "hmm",
        }), settings)
        route = router.classify("what time is it")
        # Flag off: the UAR escalation fires exactly as before.
        assert route.model == "claude"
        assert "escalated to Claude" in route.reasoning

    def test_low_support_hint_does_not_override_escalation(
        self, in_memory_db, settings, monkeypatch,
    ):
        from services import trajectory_store
        from services.router import TaskRouter
        settings.set("trajectory_consolidation_enabled", True)
        monkeypatch.setattr(
            trajectory_store, "hint_table",
            lambda *args, **kw: {"agent-a": (0.9, 2)})  # support < 5

        router = TaskRouter(_local_classifier({
            "complexity": "simple", "model": "local",
            "confidence": 0.45, "needs_context": False, "reasoning": "hmm",
        }), settings)
        route = router.classify("what time is it")
        assert route.model == "claude"  # escalation unchanged
