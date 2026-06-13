"""
services/pipeline.py — Team Pipeline Executor.

When an agent team is active, decomposes a user message into sub-tasks,
dispatches each to the appropriate specialist via HubRouter, chains
HandoffPackets between steps, and synthesises a final response.

Single-agent chat is unaffected — the pipeline only activates when the
orchestrator detects an active team (i.e. the selected agent is the
coordinator of an agent_teams row).

Uses existing infrastructure:
  - HubRouter.invoke() for all model calls (single boundary preserved)
  - HandoffPacket + HandoffValidation from models.py
  - handoff_log table from db.py
  - SSE events via on_event callback

Layer 2 wiring (Priority 4 + Priority 6):
  - workflow_checkpoints — every specialist step is bracketed by a
    provisional → committed/rolled_back transition with retry-on-failure
    and a startup pass that marks orphaned provisional rows abandoned.
  - debate_log — opt-in adversarial challenger fires after each committed
    step and the synthesizer sees the critique alongside the artifact.
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import db as _db
from models import (
    ChallengePacket,
    HandoffPacket,
    RoutingDecision,
    TaskDescriptor,
    semantic_validate_handoff,
    validate_handoff_packet,
)
from services.hub_router import HubRouter
from services.redact import redact

log = logging.getLogger("altosybioagents.pipeline")

# Maximum sub-tasks the coordinator can decompose into. Prevents runaway
# decomposition on adversarial or ambiguous inputs.
MAX_SUBTASKS = 6

# Maximum retries per specialist when HandoffPacket validation fails. The
# saga commits the row on the first pass and only retries on validation
# rollback, so a value > 1 here ratchets reliability without changing the
# happy path's latency.
MAX_RETRIES_PER_STEP = 3

# Maximum HandoffPacket context injected into downstream agents (chars).
# Prevents context rot when many specialists contribute.
MAX_UPSTREAM_CONTEXT_CHARS = 12_000

# Perf Phase 5: clamp range for the parallel scheduler's worker pool
# (``pipeline_max_concurrency`` setting). The floor keeps a misconfigured 0
# from deadlocking; the ceiling keeps a runaway value from spawning a thread
# per subtask times retries.
MIN_PARALLEL_WORKERS = 1
MAX_PARALLEL_WORKERS = 8

# Workflow-checkpoint state vocabulary. Kept at module scope so tests and
# downstream consumers can import the strings instead of re-typing them.
CHECKPOINT_PROVISIONAL = "provisional"
CHECKPOINT_COMMITTED   = "committed"
CHECKPOINT_ROLLED_BACK = "rolled_back"
CHECKPOINT_ABANDONED   = "abandoned"


def _setting_truthy(settings, key: str, default: bool) -> bool:
    """Coerce a settings value (which may be '1'/'0'/'true'/etc.) to bool."""
    try:
        raw = settings.get(key, default)
    except Exception:
        return default
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "on")


def _setting_int(settings, key: str, default: int) -> int:
    """Coerce a settings value to int, falling back to ``default``."""
    try:
        raw = settings.get(key, default)
    except Exception:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ── Per-backend admission control (Perf Phase 5) ─────────────────────────────
#
# Module-level so every parallel pipeline run in the process shares ONE
# admission gate per backend class. Local inference (Ollama / LM Studio /
# the bundled llama.cpp server) is effectively single-stream on one GPU, so
# its semaphore defaults to 1 permit — parallel wall-clock wins come from
# Claude/litellm-routed or mixed-backend subtask sets, which share the
# pipeline-wide limit. A semaphore is lazily (re)created when its configured
# limit changes; a change while permits are held only affects subsequent
# acquisitions, which is acceptable for an advisory gate.

_admission_lock = threading.Lock()
_admission_semaphores: dict[str, tuple[int, threading.Semaphore]] = {}


def _backend_semaphore(key: str, limit: int) -> threading.Semaphore:
    """Return the shared admission semaphore for ``key`` at ``limit`` permits."""
    limit = max(1, int(limit))
    with _admission_lock:
        entry = _admission_semaphores.get(key)
        if entry is None or entry[0] != limit:
            entry = (limit, threading.Semaphore(limit))
            _admission_semaphores[key] = entry
        return entry[1]


class _AdmissionGatedHub:
    """Wrap a HubRouter, gating ``invoke()`` behind per-backend semaphores.

    Used ONLY by the parallel scheduler — the sequential path calls the hub
    directly and stays byte-identical. HubRouter remains the single worker
    invocation boundary; this proxy adds admission control around that
    boundary and nothing else. The backend is read off the RoutingDecision
    the pipeline routed for the step: ``local`` competes for the local
    semaphore (``pipeline_local_concurrency``, default 1 — single GPU),
    everything else (claude/litellm) shares the remote semaphore
    (``pipeline_max_concurrency``). All other attributes pass through.
    """

    def __init__(self, hub: HubRouter, local_limit: int, remote_limit: int):
        self._gated_hub = hub
        self._local_limit = local_limit
        self._remote_limit = remote_limit

    def invoke(self, decision, *args, **kwargs):
        if getattr(decision, "backend", "") == "local":
            sem = _backend_semaphore("local", self._local_limit)
        else:
            sem = _backend_semaphore("remote", self._remote_limit)
        with sem:
            return self._gated_hub.invoke(decision, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._gated_hub, name)


def _str_list(raw) -> list:
    """Coerce a JSON value into a list of trimmed non-empty strings."""
    if not isinstance(raw, list):
        return []
    out: list = []
    for item in raw:
        s = str(item or "").strip()
        if s:
            out.append(s[:500])
    return out


def _parse_allowed_tools(raw) -> list[str]:
    """Decode an agent/team ``allowed_tools`` JSON column into a list.

    Returns [] for NULL, empty strings, "[]", or anything that doesn't
    decode into a list of strings. The "[]" sentinel means "no per-row
    restriction" everywhere in the schema, so it must round-trip to an
    empty list here.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text or text == "[]":
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x).strip() for x in parsed if str(x).strip()]


def _union_allowed_tools(agent_tools: list[str], team_tools: list[str]) -> list[str]:
    """Union an agent's allowed_tools with the team's.

    Both sides are whitelists; an empty list on either side means "no
    constraint contributed". Returns the merged, deduplicated, sorted list,
    or [] when neither side restricts (the caller treats [] as "skip the
    restriction notice").
    """
    if not agent_tools and not team_tools:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for src in (agent_tools, team_tools):
        for tool in src:
            if tool and tool not in seen:
                seen.add(tool)
                out.append(tool)
    return sorted(out)


def mark_abandoned_provisional_checkpoints() -> int:
    """Mark every still-provisional row as 'abandoned' on sidecar startup.

    A provisional row from a previous process means the sidecar died after
    opening a checkpoint but before validating it — the only honest answer
    is to declare the workflow lost. Returns the row count for logging.
    Best-effort: a DB error is swallowed and reported by the caller.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _db.transaction() as conn:
            cur = conn.execute(
                "UPDATE workflow_checkpoints "
                "SET state = ?, rolled_back_at = ?, "
                "    failure_reason = COALESCE(failure_reason, 'sidecar restart abandoned in-flight checkpoint') "
                "WHERE state = ?",
                (CHECKPOINT_ABANDONED, now, CHECKPOINT_PROVISIONAL),
            )
            return cur.rowcount or 0
    except Exception as exc:
        log.warning("mark_abandoned_provisional_checkpoints failed: %s", exc)
        return 0


@dataclass
class SubTask:
    """A single specialist assignment from the coordinator's decomposition."""
    agent_id: str
    agent_name: str
    description: str
    # 0-BASED indexes into the parsed subtask list of the steps whose output
    # this step needs (the coordinator emits 1-based indexes; _parse_subtasks
    # normalizes). Only populated when pipeline_parallel_enabled is on, and
    # guaranteed to reference strictly earlier steps — a DAG by construction.
    # Flag off: always [] (sequential semantics, exactly as before Phase 5).
    depends_on: list = field(default_factory=list)


@dataclass
class PipelineResult:
    """Outcome of a full pipeline run."""
    synthesis: str
    steps: list = field(default_factory=list)
    handoffs: list = field(default_factory=list)
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    # Backend model name used for the synthesis step, e.g. "claude-sonnet-..."
    # or the configured local model name. The orchestrator uses this to
    # estimate cost; per-step cost attribution is a future iteration.
    synthesis_model: str = "pipeline"
    pipeline_id: str = field(default_factory=lambda: str(uuid.uuid4()))


DECOMPOSITION_PROMPT = """You are a team coordinator. Break the user's request into sub-tasks for your specialists.

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

# Perf Phase 5: dependency-aware clone of DECOMPOSITION_PROMPT, used ONLY
# when ``pipeline_parallel_enabled`` is on. The flag-off prompt above must
# stay byte-identical (tests assert it), so the depends_on field and its
# rule are added here instead of edited in.
DECOMPOSITION_PROMPT_PARALLEL = """You are a team coordinator. Break the user's request into sub-tasks for your specialists.

Available specialists:
{agent_list}

Return ONLY a JSON array. Each element:
{{
  "agent_id": "<id of the specialist>",
  "agent_name": "<name for display>",
  "description": "<what this specialist should do — be specific>",
  "depends_on": [<1-based indexes of EARLIER steps>]
}}

Rules:
- Order matters: earlier steps execute first, later steps can reference earlier results.
- Mark a dependency ONLY if the step needs another step's output to do its work; steps that can proceed from the user request alone must have an empty list.
- Use 1 step if the task is simple enough for one specialist.
- Maximum {max_steps} steps.
- Every step must map to one of the listed specialists.
- If the task doesn't need specialisation, return a single step with yourself as the agent.
- Do NOT include a "synthesis" step — that happens automatically after all specialists finish.
"""

SYNTHESIS_PROMPT = """You are a team coordinator. Your specialists have completed their sub-tasks.
Synthesise their outputs into a single, coherent response for the user.

The user's original request: {user_message}

Specialist outputs:
{handoff_blocks}
{challenge_blocks}
Instructions:
- Combine the specialists' work into one clear response.
- Resolve any contradictions by noting them.
- If a specialist flagged low confidence or uncertainties, mention them briefly.
- If a challenger raised disputes, fact conflicts, or missing analysis above,
  weigh them and reflect the strongest critiques in your answer.
- Write as if YOU did the work — don't say "the researcher found..." unless attribution adds value.
- Keep the response focused on what the user asked for.
"""

# The challenger prompt is intentionally small and JSON-only. It runs after
# every committed step, so its latency tax shows up on every team turn — the
# shorter we keep it, the less it costs.
CHALLENGER_PROMPT = """You are an adversarial reviewer. Critique the work below.

Original task: {task}
Specialist's deliverable: {artifact}

Return ONLY a JSON object with these keys (each list may be empty):
{{
  "assumption_diffs":   ["...assumptions you'd dispute..."],
  "fact_conflicts":     ["...claims that look wrong or contradict known facts..."],
  "missing_analysis":   ["...important gaps the deliverable left out..."],
  "changed_position":   true | false,
  "revised_conclusion": "if changed_position is true, your preferred answer; otherwise empty string",
  "overall_assessment": "one sentence"
}}

Be specific. If you find nothing wrong, return all-empty lists, changed_position=false,
and overall_assessment='No material issues found.' Never invent objections to look thorough.
"""


class PipelineExecutor:
    """Executes a multi-agent pipeline for a team.

    ``claude_client`` and ``local_client`` are optional and default to None.
    When supplied they enable semantic_validate_handoff (which calls the
    local model to score whether a deliverable satisfies its task) and the
    debate-log challenger. When omitted, the executor falls back to the
    structural-only validator and skips debate entirely. Existing tests that
    construct ``PipelineExecutor(hub, settings)`` keep working unchanged.
    """

    def __init__(self, hub_router: HubRouter, settings,
                 claude_client=None, local_client=None):
        self._hub = hub_router
        self._settings = settings
        self._claude = claude_client
        self._local = local_client

    def run(
        self,
        team_id: str,
        user_message: str,
        conversation_id: str,
        history: list,
        on_event: Optional[Callable] = None,
        on_token: Optional[Callable] = None,
    ) -> PipelineResult:
        """Execute the full pipeline: decompose -> specialists -> synthesise."""
        from services import perf_metrics
        with perf_metrics.span("pipeline_total"):
            return self._run_inner(
                team_id, user_message, conversation_id, history,
                on_event=on_event, on_token=on_token,
            )

    def _run_inner(
        self,
        team_id: str,
        user_message: str,
        conversation_id: str,
        history: list,
        on_event: Optional[Callable] = None,
        on_token: Optional[Callable] = None,
    ) -> PipelineResult:

        def emit(event_type: str, data: dict):
            if on_event:
                try:
                    on_event(event_type, data)
                except Exception:
                    pass

        pipeline_id = str(uuid.uuid4())
        emit("pipeline_started", {"pipeline_id": pipeline_id, "team_id": team_id})

        team = _db.fetchone("SELECT * FROM agent_teams WHERE id = ?", (team_id,))
        if not team:
            raise ValueError(f"Team not found: {team_id}")

        # Phase 4: per-team tool restrictions. Parsed once per pipeline run
        # and unioned with each specialist's own allowed_tools list at
        # dispatch time inside _invoke_specialist. NULL or [] means "no
        # team-level constraint; trust the per-agent lists".
        team_allowed_tools = _parse_allowed_tools(team["allowed_tools"]) \
            if "allowed_tools" in team.keys() else []

        coordinator_id = team["coordinator_id"]
        coordinator_row = _db.fetchone(
            "SELECT * FROM agents WHERE id = ?", (coordinator_id,)
        )
        if not coordinator_row:
            raise ValueError(f"Coordinator not found: {coordinator_id}")
        coordinator = dict(coordinator_row)

        member_rows = _db.fetchall(
            "SELECT a.* FROM agents a "
            "JOIN agent_team_members atm ON atm.agent_id = a.id "
            "WHERE atm.team_id = ? AND a.id != ?",
            (team_id, coordinator_id),
        )
        members = [dict(m) for m in member_rows]

        if not members:
            log.info(
                "Team %s has no specialists; falling back to coordinator-only",
                team_id,
            )
            return self._single_agent_fallback(
                coordinator, user_message, history, pipeline_id, emit, on_token,
            )

        # ── Step 1: Coordinator decomposes ──────────────────────────────────
        emit("pipeline_decomposing", {"agent": coordinator["name"]})

        agent_list = "\n".join(
            f"- {m['name']} (id: {m['id']}, role: {m.get('role') or 'worker'}): "
            f"{(m.get('system_prompt') or '')[:150]}"
            for m in members
        )

        # Perf Phase 5: dependency-aware decomposition + wave scheduling,
        # flag-gated. Flag off keeps DECOMPOSITION_PROMPT, an empty
        # depends_on on every SubTask, and the sequential loop below —
        # byte-identical to pre-Phase-5 turns.
        parallel_enabled = _setting_truthy(
            self._settings, "pipeline_parallel_enabled", default=False,
        )

        decomp_system = (
            DECOMPOSITION_PROMPT_PARALLEL if parallel_enabled
            else DECOMPOSITION_PROMPT
        ).format(
            agent_list=agent_list,
            max_steps=MAX_SUBTASKS,
        )
        decomp_messages = [{"role": "user", "content": user_message}]

        coordinator_task = TaskDescriptor(
            text=user_message, preferred_agent_id=coordinator_id,
        )
        decomp_decision = self._hub.route_for_agent(coordinator_id, coordinator_task)
        decomp_result = self._hub.invoke(
            decomp_decision, decomp_system, decomp_messages, max_tokens=2048,
        )

        subtasks = self._parse_subtasks(
            decomp_result.text, members, coordinator, parallel=parallel_enabled,
        )
        if not subtasks:
            log.warning(
                "Coordinator produced no subtasks; falling back to coordinator-only",
            )
            return self._single_agent_fallback(
                coordinator, user_message, history, pipeline_id, emit, on_token,
            )

        emit("pipeline_plan", {
            "pipeline_id": pipeline_id,
            "steps": [
                {"agent": s.agent_name, "task": s.description} for s in subtasks
            ],
        })

        # ── Step 2: Execute each sub-task under the saga ────────────────────
        # Each sub-task opens a workflow_checkpoints row in 'provisional'.
        # On structural + semantic validation pass we commit it; on failure
        # we roll it back and retry up to MAX_RETRIES_PER_STEP, injecting
        # the prior failure_reason into the next prompt. After the final
        # commit (or exhaustion) we optionally run the adversarial
        # challenger and persist its ChallengePacket to debate_log.
        handoffs: list[HandoffPacket] = []
        challenges: list[ChallengePacket] = []
        step_summaries: list[dict] = []
        debate_id = str(uuid.uuid4())  # one debate per turn; many challenges
        debate_active = self._debate_should_run(user_message)

        # Perf Phase 5: the parallel scheduler replaces the sequential loop
        # below when the flag is on; flag off iterates the same loop body,
        # textually untouched, over the full subtask list.
        if parallel_enabled:
            handoffs, challenges, step_summaries = self._run_steps_parallel(
                subtasks=subtasks,
                user_message=user_message,
                team_allowed_tools=team_allowed_tools,
                pipeline_id=pipeline_id,
                debate_active=debate_active,
                debate_id=debate_id,
                emit=emit,
            )
            sequential_subtasks: list = []
        else:
            sequential_subtasks = subtasks

        for i, subtask in enumerate(sequential_subtasks):
            emit("pipeline_step_started", {
                "step": i + 1,
                "total": len(subtasks),
                "agent": subtask.agent_name,
                "task": subtask.description,
            })

            specialist_row = _db.fetchone(
                "SELECT * FROM agents WHERE id = ?", (subtask.agent_id,),
            )
            if not specialist_row:
                log.error("Specialist %s not found, skipping", subtask.agent_id)
                continue
            specialist = dict(specialist_row)

            specialist_system = (
                specialist.get("system_prompt") or "You are a helpful specialist."
            )

            # Phase 4: tool-restriction notice. Union the team-level allowed
            # tools list with this specialist's own list — when either side
            # restricts, append a one-liner so the specialist self-limits
            # the tools it claims to use in its deliverable. Empty union
            # means "no tighter constraint than what's already in place".
            specialist_allowed_tools = _parse_allowed_tools(
                specialist.get("allowed_tools"),
            )
            effective_tools = _union_allowed_tools(
                specialist_allowed_tools, team_allowed_tools,
            )
            if effective_tools:
                specialist_system += (
                    "\n\nAllowed tools for this team: "
                    + ", ".join(effective_tools)
                )

            upstream_context = self._build_upstream_context(handoffs)
            if upstream_context:
                specialist_system += "\n\n" + upstream_context

            spec_task = TaskDescriptor(
                text=subtask.description, preferred_agent_id=subtask.agent_id,
            )
            spec_decision = self._hub.route_for_agent(subtask.agent_id, spec_task)

            packet = self._run_step_with_saga(
                spec_decision=spec_decision,
                specialist_system=specialist_system,
                subtask=subtask,
                user_message=user_message,
                pipeline_id=pipeline_id,
                step_index=i,
                emit=emit,
            )

            self._log_handoff(packet)
            handoffs.append(packet)

            challenge = None
            if debate_active and packet.validation_passed:
                challenge = self._run_challenger(
                    subtask=subtask,
                    packet=packet,
                    pipeline_id=pipeline_id,
                    debate_id=debate_id,
                    emit=emit,
                )
                if challenge is not None:
                    challenges.append(challenge)
                    self._log_challenge(challenge)

            summary = {
                "step": i + 1,
                "agent": subtask.agent_name,
                "task": subtask.description,
                "confidence": packet.confidence_label,
                "validation_passed": packet.validation_passed,
                "tokens": packet.input_tokens + packet.output_tokens,
                "duration_ms": round(packet.duration_ms),
                "challenger_signal": (
                    challenge.has_signal() if challenge is not None else False
                ),
            }
            step_summaries.append(summary)
            emit("pipeline_step_complete", summary)

        # ── Step 3: Coordinator synthesises ─────────────────────────────────
        emit("pipeline_synthesising", {"agent": coordinator["name"]})

        handoff_blocks = "\n\n".join(h.to_context_block() for h in handoffs)
        challenge_text = "\n\n".join(
            c.to_context_block() for c in challenges if c.has_signal()
        )
        challenge_blocks = (
            "\nChallenger reviews:\n" + challenge_text + "\n"
            if challenge_text else ""
        )

        synth_system = (
            coordinator.get("system_prompt") or "You are a team coordinator."
        )
        synth_messages = [{
            "role": "user",
            "content": SYNTHESIS_PROMPT.format(
                user_message=user_message,
                handoff_blocks=handoff_blocks,
                challenge_blocks=challenge_blocks,
            ),
        }]

        synth_decision = self._hub.route_for_agent(coordinator_id, coordinator_task)
        synth_result = self._hub.invoke(
            synth_decision, synth_system, synth_messages,
            max_tokens=4096, on_token=on_token,
        )

        emit("pipeline_complete", {
            "pipeline_id": pipeline_id,
            "steps_completed": len(step_summaries),
            "total_steps": len(subtasks),
        })

        total_in = (
            sum(h.input_tokens for h in handoffs)
            + (decomp_result.input_tokens or 0)
            + (synth_result.input_tokens or 0)
        )
        total_out = (
            sum(h.output_tokens for h in handoffs)
            + (decomp_result.output_tokens or 0)
            + (synth_result.output_tokens or 0)
        )

        return PipelineResult(
            synthesis=synth_result.text,
            steps=step_summaries,
            handoffs=handoffs,
            total_tokens_in=total_in,
            total_tokens_out=total_out,
            synthesis_model=synth_result.model_name or "pipeline",
            pipeline_id=pipeline_id,
        )

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _run_step_with_saga(
        self,
        spec_decision: RoutingDecision,
        specialist_system: str,
        subtask: SubTask,
        user_message: str,
        pipeline_id: str,
        step_index: int,
        emit: Callable[[str, dict], None],
    ) -> HandoffPacket:
        """Run one specialist step under the workflow-checkpoints saga.

        Opens a 'provisional' checkpoint, invokes the specialist, validates
        the resulting HandoffPacket (structural + semantic when a local
        client is wired), and either commits the checkpoint or rolls it
        back. On rollback, retries up to ``max_retries`` times with the
        previous failure_reason injected into the prompt. The final packet
        — committed or exhausted — is returned to the caller.

        Always returns a HandoffPacket; never raises. Checkpoint and SSE
        writes are best-effort and never block the pipeline.
        """
        max_retries = MAX_RETRIES_PER_STEP
        checkpoint_id = self._open_checkpoint(
            pipeline_id=pipeline_id,
            step_index=step_index,
            subtask=subtask,
            max_retries=max_retries,
        )
        emit("checkpoint_state", {
            "checkpoint_id": checkpoint_id,
            "step": step_index + 1,
            "agent": subtask.agent_name,
            "state": CHECKPOINT_PROVISIONAL,
        })

        last_packet: Optional[HandoffPacket] = None
        last_failure_reason = ""
        attempt = 0
        # Total attempts = 1 initial + max_retries retries.
        for attempt in range(max_retries + 1):
            messages = self._build_specialist_messages(
                subtask=subtask,
                user_message=user_message,
                prior_failure_reason=last_failure_reason,
            )
            packet = self._invoke_specialist(
                decision=spec_decision,
                system=specialist_system,
                messages=messages,
                subtask=subtask,
                pipeline_id=pipeline_id,
                step_index=step_index,
                is_retry=attempt > 0,
            )
            last_packet = packet

            if packet.validation_passed:
                self._commit_checkpoint(checkpoint_id, packet)
                emit("checkpoint_state", {
                    "checkpoint_id": checkpoint_id,
                    "step": step_index + 1,
                    "agent": subtask.agent_name,
                    "state": CHECKPOINT_COMMITTED,
                    "confidence": packet.confidence,
                })
                return packet

            last_failure_reason = "; ".join(packet.validation_notes) or "validation failed"
            # ``retry_count`` is "retries used" — 0 on the initial attempt's
            # failure, max_retries when the last retry also fails. Counting
            # the initial attempt as a retry would push the column past
            # max_retries on exhaustion, which reads wrong in queries.
            self._rollback_checkpoint(
                checkpoint_id, packet, last_failure_reason, retry_count=attempt,
            )
            emit("checkpoint_state", {
                "checkpoint_id": checkpoint_id,
                "step": step_index + 1,
                "agent": subtask.agent_name,
                "state": CHECKPOINT_ROLLED_BACK,
                "reason": last_failure_reason,
                "retry": attempt,
                "max_retries": max_retries,
            })
            if attempt < max_retries:
                emit("pipeline_step_retry", {
                    "step": step_index + 1,
                    "agent": subtask.agent_name,
                    "reason": last_failure_reason,
                    "attempt": attempt + 2,
                })

        # Retries exhausted. Return the last packet so the caller can still
        # log it and the synthesizer can see the failure flagged.
        return last_packet  # type: ignore[return-value]

    # ── Perf Phase 5: parallel wave scheduler ───────────────────────────────

    def _run_steps_parallel(
        self,
        subtasks: list,
        user_message: str,
        team_allowed_tools: list,
        pipeline_id: str,
        debate_active: bool,
        debate_id: str,
        emit: Callable[[str, dict], None],
    ) -> tuple[list, list, list]:
        """Execute the subtasks as a dependency DAG over a thread pool.

        Wave/topological scheduling: a step becomes runnable once every step
        in its ``depends_on`` has reached a TERMINAL outcome — committed OR
        retries-exhausted (final rollback). That mirrors the sequential loop
        exactly: there, a failed step's packet still flows downstream (it is
        appended to ``handoffs`` and shows up — failure flag and all — in
        later steps' upstream context), so dependents run either way and see
        whatever packet the dependency produced.

        Each step executes the existing ``_run_step_with_saga`` UNCHANGED,
        so per-step saga semantics (provisional → committed/rolled_back,
        retry-with-failure-reason) are identical to sequential execution.

        Results land in index-slotted lists — exactly one writer per slot,
        no appends from worker threads — and are compacted in step order
        afterwards, so the synthesis prompt has the exact shape sequential
        execution produces for the same packets.

        Admission control: workers reach the hub through _AdmissionGatedHub,
        which serializes local inference (``pipeline_local_concurrency``,
        default 1 — one GPU is single-stream) while Claude/litellm calls
        overlap up to ``pipeline_max_concurrency``.

        A step that raises is recorded as an error packet and its dependents
        still run — one failure never deadlocks a wave. SSE events may
        interleave across steps; every payload carries its 1-based ``step``.

        Returns ``(handoffs, challenges, step_summaries)`` compacted in step
        order, matching what the sequential loop accumulates.
        """
        total = len(subtasks)
        max_workers = min(
            MAX_PARALLEL_WORKERS,
            max(MIN_PARALLEL_WORKERS,
                _setting_int(self._settings, "pipeline_max_concurrency", 3)),
        )
        local_limit = min(
            MAX_PARALLEL_WORKERS,
            max(1, _setting_int(self._settings, "pipeline_local_concurrency", 1)),
        )

        # Shallow copy of the executor with the hub swapped for the gated
        # proxy: worker threads run the existing saga/challenger methods on
        # this copy, so every hub.invoke inside them — and ONLY them; the
        # sequential path never sees the proxy — passes admission control.
        gated = copy.copy(self)
        gated._hub = _AdmissionGatedHub(self._hub, local_limit, max_workers)

        packet_slots: list = [None] * total
        challenge_slots: list = [None] * total
        summary_slots: list = [None] * total
        finished = [False] * total

        def _run_one(i: int) -> None:
            subtask = subtasks[i]
            emit("pipeline_step_started", {
                "step": i + 1,
                "total": total,
                "agent": subtask.agent_name,
                "task": subtask.description,
            })

            specialist_row = _db.fetchone(
                "SELECT * FROM agents WHERE id = ?", (subtask.agent_id,),
            )
            if not specialist_row:
                log.error("Specialist %s not found, skipping", subtask.agent_id)
                return
            specialist = dict(specialist_row)

            specialist_system = (
                specialist.get("system_prompt") or "You are a helpful specialist."
            )
            specialist_allowed_tools = _parse_allowed_tools(
                specialist.get("allowed_tools"),
            )
            effective_tools = _union_allowed_tools(
                specialist_allowed_tools, team_allowed_tools,
            )
            if effective_tools:
                specialist_system += (
                    "\n\nAllowed tools for this team: "
                    + ", ".join(effective_tools)
                )

            # Parallel-mode upstream context: packets of this step's
            # TRANSITIVE dependencies only (see _transitive_deps). All of
            # them are terminal by the time the scheduler made this step
            # runnable, so reading their slots is race-free.
            dep_packets = [
                packet_slots[d]
                for d in self._transitive_deps(subtasks, i)
                if packet_slots[d] is not None
            ]
            upstream_context = self._build_upstream_context(dep_packets)
            if upstream_context:
                specialist_system += "\n\n" + upstream_context

            spec_task = TaskDescriptor(
                text=subtask.description, preferred_agent_id=subtask.agent_id,
            )
            spec_decision = self._hub.route_for_agent(subtask.agent_id, spec_task)

            packet = gated._run_step_with_saga(
                spec_decision=spec_decision,
                specialist_system=specialist_system,
                subtask=subtask,
                user_message=user_message,
                pipeline_id=pipeline_id,
                step_index=i,
                emit=emit,
            )

            self._log_handoff(packet)
            packet_slots[i] = packet

            challenge = None
            if debate_active and packet.validation_passed:
                challenge = gated._run_challenger(
                    subtask=subtask,
                    packet=packet,
                    pipeline_id=pipeline_id,
                    debate_id=debate_id,
                    emit=emit,
                )
                if challenge is not None:
                    challenge_slots[i] = challenge
                    self._log_challenge(challenge)

            summary = {
                "step": i + 1,
                "agent": subtask.agent_name,
                "task": subtask.description,
                "confidence": packet.confidence_label,
                "validation_passed": packet.validation_passed,
                "tokens": packet.input_tokens + packet.output_tokens,
                "duration_ms": round(packet.duration_ms),
                "challenger_signal": (
                    challenge.has_signal() if challenge is not None else False
                ),
            }
            summary_slots[i] = summary
            emit("pipeline_step_complete", summary)

        def _run_one_safe(i: int) -> None:
            # The sequential loop surfaces model errors INSIDE the
            # WorkerResult (hub.invoke never raises in practice), so an
            # exception here is unexpected — but it must terminate the step
            # with an error packet rather than deadlock its dependents.
            try:
                _run_one(i)
            except Exception as exc:
                log.error("Parallel pipeline step %d failed: %s", i + 1, exc)
                if packet_slots[i] is not None:
                    return  # step already produced its packet; just finish
                subtask = subtasks[i]
                packet = HandoffPacket(
                    agent_id=subtask.agent_id,
                    agent_name=subtask.agent_name,
                    subtask_completed=subtask.description,
                    artifact=f"[Error: {exc}]",
                    uncertainties=[
                        "Specialist invocation raised an exception.",
                    ],
                    confidence=0.3,
                    workflow_id=pipeline_id,
                    step_index=i,
                    validation_passed=False,
                    validation_notes=[str(exc)[:500]],
                )
                self._log_handoff(packet)
                packet_slots[i] = packet
                summary = {
                    "step": i + 1,
                    "agent": subtask.agent_name,
                    "task": subtask.description,
                    "confidence": packet.confidence_label,
                    "validation_passed": False,
                    "tokens": 0,
                    "duration_ms": 0,
                    "challenger_signal": False,
                }
                summary_slots[i] = summary
                emit("pipeline_step_complete", summary)

        pending = set(range(total))
        running: dict = {}  # future → step index
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
        ) as pool:
            while pending or running:
                runnable = sorted(
                    i for i in pending
                    if all(
                        finished[d]
                        for d in subtasks[i].depends_on
                        if isinstance(d, int) and 0 <= d < total
                    )
                )
                for i in runnable:
                    pending.discard(i)
                    running[pool.submit(_run_one_safe, i)] = i
                if not running:
                    # Unreachable when depends_on came through
                    # _parse_subtasks (DAG by construction), but a malformed
                    # in-memory SubTask must degrade to skipped steps, never
                    # to a hang.
                    log.error(
                        "Parallel scheduler stalled with %d unrunnable "
                        "steps; skipping them", len(pending),
                    )
                    break
                done, _ = concurrent.futures.wait(
                    running, return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    finished[running.pop(fut)] = True

        handoffs = [p for p in packet_slots if p is not None]
        challenges = [c for c in challenge_slots if c is not None]
        step_summaries = [s for s in summary_slots if s is not None]
        return handoffs, challenges, step_summaries

    @staticmethod
    def _transitive_deps(subtasks: list, index: int) -> list:
        """All steps ``index`` transitively depends on, sorted by step index.

        Parallel-mode upstream context is built from EXACTLY these packets.
        This is the deliberate semantic difference from sequential mode,
        where every prior handoff flows into every later step: the
        coordinator declaring a step independent (empty depends_on) is a
        statement that the step can proceed from the user request alone, so
        sibling output is withheld — both to honor that contract and because
        siblings may still be running when this step starts.
        """
        seen: set[int] = set()
        stack = list(subtasks[index].depends_on)
        while stack:
            d = stack.pop()
            if not isinstance(d, int) or d in seen or not 0 <= d < index:
                continue
            seen.add(d)
            stack.extend(subtasks[d].depends_on)
        return sorted(seen)

    def _build_specialist_messages(
        self, subtask: SubTask, user_message: str, prior_failure_reason: str = "",
    ) -> list:
        """Build the user-message list for a specialist invocation.

        On retry, ``prior_failure_reason`` is injected so the model knows
        what the validator complained about and can avoid repeating it.
        """
        prefix = ""
        if prior_failure_reason:
            prefix = (
                f"Your previous attempt at this task failed validation:\n"
                f"  {prior_failure_reason}\n\n"
                "Address the failure explicitly. Be more specific and concrete. "
                "State your uncertainties.\n\n"
            )
        return [{
            "role": "user",
            "content": (
                f"{prefix}"
                f"You are working as part of a team. Your specific task:\n\n"
                f"{subtask.description}\n\n"
                f"The user's original request was: {user_message}\n\n"
                f"Complete your task thoroughly. Be specific and concrete in "
                f"your output."
            ),
        }]

    def _invoke_specialist(
        self,
        decision: RoutingDecision,
        system: str,
        messages: list,
        subtask: SubTask,
        pipeline_id: str,
        step_index: int,
        is_retry: bool = False,
    ) -> HandoffPacket:
        """Invoke a specialist and wrap the WorkerResult into a HandoffPacket.

        Validates with semantic_validate_handoff when a local client is
        wired (catches off-topic / empty deliverables that the structural
        check misses); otherwise falls back to validate_handoff_packet.
        """
        start_ms = time.monotonic()
        result = self._hub.invoke(decision, system, messages, max_tokens=4096)
        elapsed_ms = (time.monotonic() - start_ms) * 1000

        # The specialists are unaware of the HandoffPacket schema (we don't
        # inject HANDOFF_SYSTEM_FRAGMENT to keep their prompts simple), so
        # we synthesise an uncertainties list. Without this, validation would
        # always fail at confidence < 0.95 and trigger a spurious retry.
        if result.had_error:
            confidence = 0.3
            uncertainties = ["Specialist invocation returned an error."]
        else:
            confidence = 0.6 if is_retry else 0.8
            uncertainties = [
                "Specialist did not self-assess; confidence is a pipeline default.",
            ]

        packet = HandoffPacket(
            agent_id=subtask.agent_id,
            agent_name=subtask.agent_name,
            subtask_completed=subtask.description,
            artifact=redact(result.text or ""),
            uncertainties=uncertainties,
            confidence=confidence,
            workflow_id=pipeline_id,
            step_index=step_index,
            raw_output=(result.text or "")[:2000],
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=elapsed_ms,
        )
        if self._local is not None:
            return semantic_validate_handoff(packet, self._local, self._claude)
        return validate_handoff_packet(packet)

    def _parse_subtasks(
        self, raw: str, members: list, coordinator: dict,
        parallel: bool = False,
    ) -> list:
        """Parse the coordinator's JSON decomposition into SubTask objects.

        When ``parallel`` is True (pipeline_parallel_enabled), each item's
        ``depends_on`` — 1-based indexes of EARLIER steps as the coordinator
        listed them — is normalized to 0-BASED indexes into the RETURNED
        subtask list (the internal convention everywhere downstream: slots,
        scheduler, transitive-deps walk). Validation makes the result a DAG
        by construction:
          - only ints referencing strictly earlier steps are kept; forward
            and self references are dropped with a log warning;
          - references to steps that were themselves dropped (unknown agent,
            empty description) are dropped — their output cannot exist,
            mirroring how the sequential loop contributes nothing for them;
          - a missing or malformed ``depends_on`` makes the step depend on
            ALL earlier kept steps — safe degradation to sequential
            semantics rather than risking an incorrect parallel ordering.
        When ``parallel`` is False, ``depends_on`` in the coordinator output
        is ignored and every SubTask keeps the pre-Phase-5 empty list.
        """
        text = (raw or "").strip()
        if "```" in text:
            match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Coordinator output is not valid JSON: %s", text[:200])
            return []

        if not isinstance(items, list):
            return []

        member_ids = {m["id"] for m in members}
        member_ids.add(coordinator["id"])

        subtasks: list[SubTask] = []
        # 1-based position in the coordinator's list → index in ``subtasks``
        # for items that survived filtering. Parallel-mode depends_on
        # references resolve through this map.
        kept_index_by_position: dict[int, int] = {}
        for position, item in enumerate(items[:MAX_SUBTASKS], start=1):
            if not isinstance(item, dict):
                continue
            aid = item.get("agent_id", "")
            if aid not in member_ids:
                log.warning(
                    "Coordinator referenced unknown agent %s, skipping", aid,
                )
                continue
            description = str(item.get("description") or "").strip()
            if not description:
                continue
            depends_on: list = []
            if parallel:
                depends_on = self._parse_depends_on(
                    item.get("depends_on"), position,
                    kept_index_by_position, len(subtasks),
                )
            kept_index_by_position[position] = len(subtasks)
            subtasks.append(SubTask(
                agent_id=aid,
                agent_name=str(item.get("agent_name") or aid),
                description=description,
                depends_on=depends_on,
            ))

        return subtasks

    @staticmethod
    def _parse_depends_on(
        raw, position: int, kept_index_by_position: dict, kept_count: int,
    ) -> list:
        """Validate one item's ``depends_on`` (parallel mode only).

        ``position`` is the item's own 1-based position in the coordinator's
        list, ``kept_index_by_position`` maps earlier positions to indexes in
        the kept subtask list, and ``kept_count`` is the item's own future
        kept index. Returns sorted 0-based kept indexes. A missing or
        non-list value degrades to "depends on all earlier kept steps".
        """
        if not isinstance(raw, list):
            if raw is not None:
                log.warning(
                    "Step %d depends_on is not a list (%.80r); treating the "
                    "step as depending on all earlier steps", position, raw,
                )
            return list(range(kept_count))

        deps: set[int] = set()
        for ref in raw:
            # bool is an int subclass; ``true`` is not a step reference.
            if isinstance(ref, bool) or not isinstance(ref, int):
                log.warning(
                    "Step %d depends_on entry %.80r is not an integer; "
                    "dropping it", position, ref,
                )
                continue
            if ref < 1 or ref >= position:
                log.warning(
                    "Step %d depends_on %d is not a strictly earlier step; "
                    "dropping it (forward/self references are invalid)",
                    position, ref,
                )
                continue
            kept = kept_index_by_position.get(ref)
            if kept is None:
                log.warning(
                    "Step %d depends_on %d references a dropped step; "
                    "dropping the reference", position, ref,
                )
                continue
            deps.add(kept)
        return sorted(deps)

    def _build_upstream_context(self, handoffs: list) -> str:
        """Build upstream context from an explicit list of HandoffPackets.

        The caller chooses the packets, which is where the two execution
        modes differ semantically:
          - sequential mode passes ALL prior handoffs (every later step sees
            everything that ran before it);
          - parallel mode passes only the step's TRANSITIVE dependencies,
            ordered by step index — the coordinator declaring a step
            independent means it must not need sibling output.
        Either way the same MAX_UPSTREAM_CONTEXT_CHARS cap applies to
        prevent context rot on downstream agents. Most recent handoffs get
        priority — they're more likely to be directly relevant to the
        current step.
        """
        if not handoffs:
            return ""

        blocks: list[str] = []
        total_chars = 0
        for h in reversed(handoffs):
            block = h.to_context_block()
            if total_chars + len(block) > MAX_UPSTREAM_CONTEXT_CHARS:
                break
            blocks.insert(0, block)
            total_chars += len(block)

        if not blocks:
            return ""

        return (
            "## Results from earlier pipeline steps\n"
            "(These are outputs from your teammates. Build on them, don't repeat them.)\n\n"
            + "\n\n".join(blocks)
        )

    def _log_handoff(self, packet: HandoffPacket) -> None:
        """Persist a HandoffPacket to the handoff_log table."""
        try:
            with _db.transaction() as conn:
                conn.execute(
                    "INSERT INTO handoff_log "
                    "(packet_id, workflow_id, step_index, agent_id, agent_name, "
                    " subtask_completed, artifact_summary, assumptions_json, "
                    " uncertainties_json, confidence, validation_passed, "
                    " validation_notes_json, duration_ms, input_tokens, "
                    " output_tokens, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        packet.workflow_id,
                        packet.step_index,
                        packet.agent_id,
                        packet.agent_name,
                        packet.subtask_completed,
                        packet.artifact[:500],
                        json.dumps(packet.assumptions),
                        json.dumps(packet.uncertainties),
                        packet.confidence,
                        1 if packet.validation_passed else 0,
                        json.dumps(packet.validation_notes),
                        packet.duration_ms,
                        packet.input_tokens,
                        packet.output_tokens,
                        packet.timestamp or datetime.now(timezone.utc).isoformat(),
                    ),
                )
        except Exception as exc:
            log.debug("handoff_log write failed (non-fatal): %s", exc)

    # ── workflow_checkpoints (saga) ─────────────────────────────────────────
    #
    # Three states make up the happy and unhappy paths:
    #   provisional → committed   (validation passed)
    #   provisional → rolled_back (validation failed; retry window open)
    #   provisional → abandoned   (process died mid-flight; resolved at startup)
    # All writes are best-effort: a DB error here MUST NOT take down the turn.

    def _open_checkpoint(
        self, pipeline_id: str, step_index: int, subtask: SubTask,
        max_retries: int,
    ) -> str:
        """Insert a 'provisional' workflow_checkpoints row. Returns its id.

        Returns an empty string if the write failed — callers treat that as
        a no-op checkpoint (the saga still works, just without persistence).
        """
        checkpoint_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        try:
            with _db.transaction() as conn:
                conn.execute(
                    "INSERT INTO workflow_checkpoints "
                    "(checkpoint_id, workflow_id, step_index, task_id, "
                    " agent_id, agent_name, state, success_criteria, "
                    " retry_count, max_retries, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        pipeline_id,
                        step_index,
                        subtask.agent_id,  # task_id ≈ owning agent for now
                        subtask.agent_id,
                        subtask.agent_name,
                        CHECKPOINT_PROVISIONAL,
                        subtask.description,
                        0,
                        max_retries,
                        now,
                    ),
                )
        except Exception as exc:
            log.debug("workflow_checkpoints open failed (non-fatal): %s", exc)
            return ""
        return checkpoint_id

    def _commit_checkpoint(self, checkpoint_id: str, packet: HandoffPacket) -> None:
        """Mark a provisional checkpoint 'committed'."""
        if not checkpoint_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            with _db.transaction() as conn:
                conn.execute(
                    "UPDATE workflow_checkpoints "
                    "SET state = ?, artifact_summary = ?, confidence_score = ?, "
                    "    validation_passed = 1, validation_reasoning = ?, "
                    "    validated_at = ?, committed_at = ? "
                    "WHERE checkpoint_id = ?",
                    (
                        CHECKPOINT_COMMITTED,
                        packet.artifact[:500],
                        packet.confidence,
                        "; ".join(packet.validation_notes)[:500],
                        now,
                        now,
                        checkpoint_id,
                    ),
                )
        except Exception as exc:
            log.debug("workflow_checkpoints commit failed (non-fatal): %s", exc)

    def _rollback_checkpoint(
        self, checkpoint_id: str, packet: HandoffPacket,
        failure_reason: str, retry_count: int,
    ) -> None:
        """Mark a provisional checkpoint 'rolled_back'."""
        if not checkpoint_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            with _db.transaction() as conn:
                conn.execute(
                    "UPDATE workflow_checkpoints "
                    "SET state = ?, artifact_summary = ?, confidence_score = ?, "
                    "    validation_passed = 0, validation_reasoning = ?, "
                    "    failure_reason = ?, retry_count = ?, "
                    "    validated_at = ?, rolled_back_at = ? "
                    "WHERE checkpoint_id = ?",
                    (
                        CHECKPOINT_ROLLED_BACK,
                        packet.artifact[:500],
                        packet.confidence,
                        "; ".join(packet.validation_notes)[:500],
                        failure_reason[:500],
                        retry_count,
                        now,
                        now,
                        checkpoint_id,
                    ),
                )
        except Exception as exc:
            log.debug("workflow_checkpoints rollback failed (non-fatal): %s", exc)

    # ── debate_log (adversarial debate) ─────────────────────────────────────

    def _debate_should_run(self, user_message: str) -> bool:
        """Decide whether the challenger fires this turn.

        Two gates:
          - debate_enabled — global on/off (default off in fresh installs)
          - debate_only_high_stakes — when true, only fire on messages
            classified as high-stakes by services.governance. When false,
            fire on every team turn (more cost, more reliability).

        The challenger needs at least the local client to run; without it
        we'd have no model to send the critique through and we silently no-op.
        """
        if self._local is None and self._claude is None:
            return False
        if not _setting_truthy(self._settings, "debate_enabled", default=False):
            return False
        if _setting_truthy(self._settings, "debate_only_high_stakes", default=True):
            try:
                from services.governance import is_high_stakes_message
            except Exception:
                return False
            return is_high_stakes_message(user_message)
        return True

    def _run_challenger(
        self,
        subtask: SubTask,
        packet: HandoffPacket,
        pipeline_id: str,
        debate_id: str,
        emit: Callable[[str, dict], None],
    ) -> Optional[ChallengePacket]:
        """Invoke the challenger and return a ChallengePacket.

        Routes through HubRouter.invoke just like a specialist so that all
        model traffic still flows through the single boundary. Failures
        (parse errors, model unavailability) are non-fatal — the pipeline
        continues without the critique. Returns ``None`` only when the
        challenger could not run at all.
        """
        # The challenger reuses the specialist's agent_id so HubRouter can
        # score+authorize it. A separate "challenger" agent could be added
        # later; for now reusing the same worker keeps the wiring simple.
        decision = self._hub.route_for_agent(
            subtask.agent_id,
            TaskDescriptor(
                text=subtask.description, preferred_agent_id=subtask.agent_id,
            ),
        )
        emit("challenger_started", {
            "step": packet.step_index + 1,
            "agent": subtask.agent_name,
        })
        start_ms = time.monotonic()
        challenger_user = CHALLENGER_PROMPT.format(
            task=subtask.description[:500],
            artifact=packet.artifact[:1500],
        )
        result = self._hub.invoke(
            decision,
            "You are an adversarial reviewer. Output JSON only.",
            [{"role": "user", "content": challenger_user}],
            max_tokens=600,
        )
        elapsed_ms = (time.monotonic() - start_ms) * 1000
        challenge_id = str(uuid.uuid4())

        if result.had_error or not result.text:
            packet_out = ChallengePacket(
                challenge_id=challenge_id,
                debate_id=debate_id,
                workflow_id=pipeline_id,
                agent_id=subtask.agent_id,
                agent_name=f"Challenger of {subtask.agent_name}",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_ms=elapsed_ms,
                parse_failed=True,
            )
            emit("challenger_complete", {
                "step": packet.step_index + 1,
                "signal": False,
                "parse_failed": True,
            })
            return packet_out

        verdict = self._parse_challenger_json(result.text)
        if verdict is None:
            return ChallengePacket(
                challenge_id=challenge_id,
                debate_id=debate_id,
                workflow_id=pipeline_id,
                agent_id=subtask.agent_id,
                agent_name=f"Challenger of {subtask.agent_name}",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_ms=elapsed_ms,
                parse_failed=True,
            )

        challenge = ChallengePacket(
            challenge_id=challenge_id,
            debate_id=debate_id,
            workflow_id=pipeline_id,
            agent_id=subtask.agent_id,
            agent_name=f"Challenger of {subtask.agent_name}",
            assumption_diffs=_str_list(verdict.get("assumption_diffs")),
            fact_conflicts=_str_list(verdict.get("fact_conflicts")),
            missing_analysis=_str_list(verdict.get("missing_analysis")),
            changed_position=bool(verdict.get("changed_position")),
            revised_conclusion=(
                str(verdict.get("revised_conclusion") or "").strip() or None
            ),
            overall_assessment=str(verdict.get("overall_assessment") or "")[:500],
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=elapsed_ms,
        )
        emit("challenger_complete", {
            "step": packet.step_index + 1,
            "signal": challenge.has_signal(),
            "parse_failed": False,
        })
        return challenge

    @staticmethod
    def _parse_challenger_json(raw: str) -> Optional[dict]:
        text = (raw or "").strip()
        if "```" in text:
            match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()
        qstart = text.find("{")
        qend = text.rfind("}")
        if qstart == -1 or qend == -1 or qend <= qstart:
            return None
        try:
            parsed = json.loads(text[qstart:qend + 1])
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    def _log_challenge(self, challenge: ChallengePacket) -> None:
        """Persist a ChallengePacket to the debate_log table."""
        try:
            with _db.transaction() as conn:
                conn.execute(
                    "INSERT INTO debate_log "
                    "(challenge_id, debate_id, workflow_id, agent_id, agent_name, "
                    " assumption_diffs_json, fact_conflicts_json, "
                    " missing_analysis_json, changed_position, revised_conclusion, "
                    " overall_assessment, input_tokens, output_tokens, "
                    " duration_ms, parse_failed, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        challenge.challenge_id,
                        challenge.debate_id,
                        challenge.workflow_id,
                        challenge.agent_id,
                        challenge.agent_name,
                        json.dumps(challenge.assumption_diffs),
                        json.dumps(challenge.fact_conflicts),
                        json.dumps(challenge.missing_analysis),
                        1 if challenge.changed_position else 0,
                        challenge.revised_conclusion,
                        challenge.overall_assessment,
                        challenge.input_tokens,
                        challenge.output_tokens,
                        challenge.duration_ms,
                        1 if challenge.parse_failed else 0,
                        challenge.timestamp,
                    ),
                )
        except Exception as exc:
            log.debug("debate_log write failed (non-fatal): %s", exc)

    def _single_agent_fallback(
        self, coordinator, user_message, history, pipeline_id, emit, on_token,
    ) -> PipelineResult:
        """Run the coordinator alone when the team has no specialists."""
        task = TaskDescriptor(
            text=user_message, preferred_agent_id=coordinator["id"],
        )
        decision = self._hub.route_for_agent(coordinator["id"], task)
        system = (
            coordinator.get("system_prompt")
            or "You are a helpful AI assistant."
        )
        messages = list(history) + [{"role": "user", "content": user_message}]
        result = self._hub.invoke(
            decision, system, messages, max_tokens=4096, on_token=on_token,
        )
        emit("pipeline_complete", {
            "pipeline_id": pipeline_id,
            "steps_completed": 0,
            "total_steps": 0,
        })
        return PipelineResult(
            synthesis=result.text,
            steps=[],
            handoffs=[],
            total_tokens_in=result.input_tokens,
            total_tokens_out=result.output_tokens,
            synthesis_model=result.model_name or "pipeline",
            pipeline_id=pipeline_id,
        )
