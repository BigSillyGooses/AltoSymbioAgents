"""
services/orchestrator_graph.py — Stage-2 #7 LangGraph rewrite of the
ChatOrchestrator.send() control flow.

Behind the ``orchestrator_engine`` setting (default ``"legacy"``). When
set to ``"graph"``, ChatOrchestrator.send() dispatches into
``run_turn_graph`` below, which composes the same downstream services
the legacy body already calls — TurnLifecycle, MemoryRecall, TurnRouter,
SecurityGate, WorkerDispatch, EscalationLadder, HubRouter,
GovernanceEngine, CaMeL, Reader/Actor split, high-stakes voting — as a
LangGraph 1.x ``StateGraph`` of nodes and edges instead of as
straight-line code.

Every named moat is preserved verbatim by reference. This module never
re-implements business logic; the graph nodes only delegate to service
instances on the ChatOrchestrator that invokes it. The migration is
intentionally control-flow-only so AgentDojo / agentic-misalignment /
governance-escalation / CaMeL adversarial / high-stakes-voting tests
exercise identical downstream code paths on either engine.

Graph topology (linear with early-exit guards on ``state["result"]``)::

    START
      └─ open_turn               TurnLifecycle.open + budget check
      └─ team_check              early-exit when conversation has team_id
      └─ load_agent              agent dict + system_prompt + allowed_tools
      └─ load_context            history trim + ephemeral + image attach
      └─ memory_recall           MemoryRecall.recall + emit + maybe_summarize
      └─ route_decision          TurnRouter.decide + emit + adaptive trim
      └─ resolve_target          WorkerDispatch.build_turn_decision +
                                 _resolve_target + vision dispatch
      └─ security_gate           SecurityGate.evaluate
      └─ governance_check        chat_invoke + token-budget verdicts
      └─ compute_flags           split / camel / voting flag computation
      └─ phase8_voting           high_stakes_consensus when applicable
      └─ phase5_escalation_check escalation_channel.check_escalation
      └─ phase12_camel           CaMeL plan+execute when camel_active
      └─ phase6_split            Reader/Actor split when split_enabled
      └─ interleaved_reasoning   extended thinking when applicable
      └─ monolithic_dispatch     worker_dispatch.dispatch fallback
      └─ alignment_check         best-effort agent alignment
      └─ escalation_ladder       EscalationLadder.maybe_escalate
      └─ finalize_turn           router_log + TurnLifecycle.close +
                                 memory update + ephemeral purge
      └─ END

Each node is idempotent w.r.t. ``state["result"]``: when an early-exit
node sets ``state["result"]`` (budget exceeded, security abort, governance
block, escalation pending, vision unavailable), every subsequent node
short-circuits and forwards state unchanged. The terminal node returns
the assembled ChatResult.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional, TypedDict

import db as _db
from models import (
    ChatResult, ExecutionTarget, RoutingDecision, TaskDescriptor,
    WorkerResult,
)
from services.governance import is_high_stakes_message
from services.local_client import LocalVisionUnavailable
from services.security_engine import quarantine_chunks, render_quarantined_context
from services.turn_context import TurnContext

log = logging.getLogger("altosybioagents.chat.graph")


class TurnState(TypedDict, total=False):
    """Per-turn graph state.

    ``total=False`` keeps each node free to return only the keys it
    mutates — LangGraph merges partial updates into the running state.

    Fields populated by ``open_turn`` survive to ``finalize_turn``; nodes
    that early-exit set ``result`` to a ChatResult, which every
    downstream node treats as "already done, fall through."
    """

    # Inputs / orchestrator handle ─────────────────────────────────────
    orchestrator:    Any
    conversation_id: str
    user_message:    str
    agent_id:        Optional[str]
    on_token:        Any
    on_event:        Any

    # Lifecycle ────────────────────────────────────────────────────────
    ctx:             TurnContext
    result:          Optional[ChatResult]      # set by any early-exit node

    # Context assembly ─────────────────────────────────────────────────
    agent:           Optional[dict]
    system_prompt:   str
    full_system:     str
    allowed_tools:   Optional[list]
    messages:        list
    image_attachments: list
    mem:             Any                       # MemoryResult.mem
    mem_result:      Any                       # MemoryResult wrapper (for adaptive RAG trim)

    # Routing ──────────────────────────────────────────────────────────
    route_outcome:   Any
    route_model:     str
    route_reason:    str
    complexity:      str
    decision:        RoutingDecision
    target:          ExecutionTarget

    # Security ─────────────────────────────────────────────────────────
    security:        Any

    # Pipeline flags / state ───────────────────────────────────────────
    split_enabled:   bool
    camel_active:    bool
    should_vote:     bool
    asst_msg_id:     str
    voting_samples:  Optional[list]

    # Worker outputs ───────────────────────────────────────────────────
    response_text:   str
    tokens_in:       int
    tokens_out:      int
    model_name:      str
    had_error:       bool
    response_empty:  bool                      # post-escalation-ladder flag


# ── Node implementations ─────────────────────────────────────────────────────
#
# All node functions take a TurnState and return a partial-update dict.
# Each node guards on ``state.get("result")`` so an early-exit node
# (budget exceeded, security abort, etc.) short-circuits the rest of the
# chain. LangGraph merges the returned dict into the running state.

def _short_circuit(state: TurnState) -> bool:
    return state.get("result") is not None


def _emit(state: TurnState, event_type: str, data: dict) -> None:
    """Forward an SSE event via the user-supplied on_event, exception-safe.

    Mirrors the inline ``_emit_event`` closure inside the legacy send().
    """
    on_event = state.get("on_event")
    if on_event is None:
        return
    try:
        on_event(event_type, data)
    except Exception:
        pass


def open_turn(state: TurnState) -> dict:
    """TurnLifecycle.open + budget guard.

    Mirrors chat_orchestrator.send():1131-1158.
    """
    orch = state["orchestrator"]
    ctx = TurnContext(
        conversation_id=state["conversation_id"],
        user_message=state["user_message"],
        agent_id=state.get("agent_id"),
        on_event=state.get("on_event"),
        on_token=state.get("on_token"),
    )
    budget_exceeded = not orch._turn_lifecycle.open(ctx)
    if budget_exceeded:
        budget = ctx.budget
        return {
            "ctx": ctx,
            "result": ChatResult(
                text=(
                    f"⚠️ This conversation has reached the "
                    f"${budget:.2f} budget limit. Start a new conversation "
                    f"or increase the limit in Settings."
                ),
                model="", route_reason="budget_exceeded",
                tokens_in=0, tokens_out=0, cost_usd=0.0,
                message_id=str(uuid.uuid4()),
            ),
        }
    return {"ctx": ctx}


def team_check(state: TurnState) -> dict:
    """Early-exit when the conversation is bound to a team.

    Mirrors send():1171-1200. Falls back to _run_team_pipeline when the
    conversation has team_id, or when an agent is registered as a team
    coordinator. The team pipeline owns its own decomposition/synthesis.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    conversation_id = state["conversation_id"]
    agent_id = state.get("agent_id")

    team_id_resolved: Optional[str] = None
    conv_row = _db.fetchone(
        "SELECT team_id FROM conversations WHERE id = ?", (conversation_id,),
    )
    if conv_row and conv_row["team_id"]:
        team_id_resolved = conv_row["team_id"]
    elif agent_id:
        team_row = _db.fetchone(
            "SELECT id FROM agent_teams WHERE coordinator_id = ? "
            "AND COALESCE(is_adhoc, 0) = 0",
            (agent_id,),
        )
        if team_row:
            team_id_resolved = team_row["id"]

    if team_id_resolved:
        ctx = state["ctx"]
        team_result = orch._run_team_pipeline(
            team_id=team_id_resolved,
            conversation_id=conversation_id,
            user_message=state["user_message"],
            spent=ctx.spent,
            budget=ctx.budget,
            warn_pct=ctx.warn_pct,
            on_event=state.get("on_event"),
            on_token=state.get("on_token"),
        )
        return {"result": team_result}
    return {}


def load_agent(state: TurnState) -> dict:
    """Resolve agent dict, persona system_prompt, and allowed_tools.

    Mirrors send():1160-1170 and the allowed_tools block at 1202-1210.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    agent_id = state.get("agent_id")
    agent: Optional[dict] = None
    if agent_id:
        row = _db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row:
            agent = dict(row)
    system_prompt = (
        agent.get("system_prompt", "You are a helpful AI assistant.") if agent
        else orch._settings.get("system_prompt", "You are a helpful AI assistant.")
    )
    allowed_tools: Optional[list] = None
    if agent and agent.get("allowed_tools") and agent["allowed_tools"] != "[]":
        try:
            parsed = json.loads(agent["allowed_tools"])
            if parsed and isinstance(parsed, list):
                allowed_tools = parsed
                log.info(
                    "Agent %s restricted to tools: %s",
                    agent["name"], allowed_tools,
                )
        except (json.JSONDecodeError, TypeError):
            pass

    state["ctx"].agent = agent  # TurnRouter reads agent off ctx
    return {
        "agent": agent,
        "system_prompt": system_prompt,
        "allowed_tools": allowed_tools,
    }


def load_context(state: TurnState) -> dict:
    """Load history (trimmed), splice ephemeral attachments, fetch images.

    Mirrors send():1212-1262.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    conversation_id = state["conversation_id"]

    from services.chat_orchestrator import MAX_HISTORY_MESSAGES
    history_rows = _db.fetchall(
        "SELECT role, content FROM messages WHERE conversation_id = ? "
        "AND role IN ('user', 'assistant') "
        "ORDER BY created_at DESC LIMIT ?",
        (conversation_id, MAX_HISTORY_MESSAGES),
    )
    messages = [
        {"role": r["role"], "content": r["content"]}
        for r in reversed(history_rows)
    ]
    messages = orch._trim_history_to_budget(messages)

    # PR 8: ephemeral attachment chunks ride inside the quarantine envelope
    # on the most recent user turn so the model treats them as data, not
    # instructions. Never persisted back to the messages table.
    ephemeral_chunks = orch._fetch_ephemeral_attachment_chunks(conversation_id)
    if ephemeral_chunks and messages:
        quarantined = quarantine_chunks(
            ephemeral_chunks,
            source_type="user_document",
            source_id=f"attach:{conversation_id}",
        )
        attachment_block = render_quarantined_context(quarantined)
        if attachment_block:
            last = messages[-1]
            if last.get("role") == "user":
                last["content"] = (
                    attachment_block + "\n\n" + last.get("content", "")
                )

    image_attachments: list[dict] = []
    if orch._settings.get("vision_enabled", True):
        try:
            image_attachments = orch._fetch_image_attachments(conversation_id)
        except Exception as exc:
            log.debug("image attachment fetch failed: %s", exc)

    return {"messages": messages, "image_attachments": image_attachments}


def memory_recall(state: TurnState) -> dict:
    """MemoryRecall.recall + memory_recalled event + maybe_summarize.

    Mirrors send():1269-1280.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    mem_result = orch._memory_recall.recall(
        conversation_id=state["conversation_id"],
        user_message=state["user_message"],
        system_prompt=state["system_prompt"],
        allowed_tools=state.get("allowed_tools"),
        agent=state.get("agent"),
    )
    _emit(state, "memory_recalled",
          orch._memory_recall.memory_recalled_event(mem_result.mem))
    orch._memory_recall.maybe_summarize(state["conversation_id"])

    return {
        "mem": mem_result.mem,
        "full_system": mem_result.full_system,
        # The orchestrator object is mutated by trim_for_complexity below;
        # we hold onto the wrapper too via state["mem_result"] for the
        # subsequent route_decision node.
        "mem_result": mem_result,
    }


def route_decision(state: TurnState) -> dict:
    """TurnRouter.decide + compound-query detection + adaptive RAG trim.

    Mirrors send():1287-1309.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    ctx = state["ctx"]
    messages = state["messages"]
    mem = state["mem"]

    route_outcome = orch._turn_router.decide(ctx, messages, mem)
    orch._turn_router.emit_decision(ctx, route_outcome)

    from services.chat_orchestrator import _detect_compound
    if _detect_compound(state["user_message"]):
        _emit(state, "compound_query_detected", {
            "message": "This looks like multiple requests. A team of agents might handle this better.",
            "suggestion": "Try selecting a team coordinator for complex multi-part requests.",
        })

    # Adaptive RAG trim — keeps initial recall and post-trim rebuild in lockstep.
    mem_result = state["mem_result"]
    mem_result = orch._memory_recall.trim_for_complexity(
        mem_result, route_outcome.complexity, state["system_prompt"],
        allowed_tools=state.get("allowed_tools"), agent=state.get("agent"),
    )
    return {
        "route_outcome": route_outcome,
        "route_model": route_outcome.model,
        "route_reason": route_outcome.reasoning,
        "complexity": route_outcome.complexity,
        "mem": mem_result.mem,
        "full_system": mem_result.full_system,
        "mem_result": mem_result,
    }


def resolve_target(state: TurnState) -> dict:
    """Phase 1: build RoutingDecision + ExecutionTarget + handle vision.

    Mirrors send():1311-1369.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    agent = state.get("agent")
    agent_id = state.get("agent_id")
    user_message = state["user_message"]
    image_attachments = state["image_attachments"]
    messages = state["messages"]

    task = TaskDescriptor(
        text=user_message,
        preferred_agent_id=agent_id,
        backend_hint=state["route_model"],
    )
    decision = orch._worker_dispatch.build_turn_decision(
        agent_id, task, state["route_outcome"],
    )
    target = orch._resolve_target(decision.backend, agent)

    if image_attachments and target.backend == "local":
        local_model = target.model_name or orch._settings.get(
            "default_local_model", "",
        )
        if not (
            hasattr(orch.local, "is_vision_model")
            and orch.local.is_vision_model(local_model)
        ):
            families = orch._settings.get("vision_local_models", []) or []
            fams = ", ".join(str(f) for f in families) or "(none configured)"
            msg = (
                f"\U0001f5bc️ Your active local model ({local_model or 'none'}) "
                f"can't see images. Switch to a vision-capable model "
                f"such as: {fams}, then resend."
            )
            _emit(state, "vision_unavailable", {
                "active_model": local_model,
                "families": list(families),
            })
            try:
                orch._purge_ephemeral_attachments(state["conversation_id"])
            except Exception:
                pass
            return {
                "decision": decision,
                "target": target,
                "result": ChatResult(
                    text=msg, model=local_model,
                    route_reason="vision_unavailable_local",
                    tokens_in=0, tokens_out=0, cost_usd=0.0,
                    message_id=str(uuid.uuid4()),
                ),
            }
    if image_attachments and target.backend == "claude":
        messages = orch._attach_images_to_messages(messages, image_attachments)

    return {"decision": decision, "target": target, "messages": messages}


def security_gate(state: TurnState) -> dict:
    """SecurityGate.evaluate — quarantine + rules + sliding-window risk.

    Mirrors send():1376-1394.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    ctx = state["ctx"]
    security_result = orch._security_gate.evaluate(
        ctx, state["full_system"], state["mem"], state["target"],
    )
    full_system = security_result.full_system
    security = security_result.assessment
    if security_result.blocked:
        return {
            "full_system": full_system,
            "security": security,
            "result": ChatResult(
                text=(
                    f"\U0001f6e1️ This workflow has been paused because the "
                    f"cumulative risk score "
                    f"({security.risk_assessment.cumulative_score:.1f}) "
                    f"exceeds the safety threshold. This happens when a "
                    f"conversation involves many high-risk operations. "
                    f"Start a new conversation or adjust the risk threshold "
                    f"in Settings."
                ),
                model="", route_reason="security_abort",
                tokens_in=0, tokens_out=0, cost_usd=0.0,
                message_id=str(uuid.uuid4()),
            ),
        }
    return {"full_system": full_system, "security": security}


def governance_check(state: TurnState) -> dict:
    """Per-agent governance policies (tool + token budget).

    Mirrors send():1402-1437.
    """
    if _short_circuit(state):
        return {}
    agent_id = state.get("agent_id")
    if not agent_id:
        return {}
    orch = state["orchestrator"]
    conversation_id = state["conversation_id"]
    target = state["target"]

    tool_verdict = orch._governance.check_tool_call(
        tool_name="chat_invoke",
        agent_id=agent_id,
        task_key=conversation_id,
    )
    if not tool_verdict.allowed:
        _emit(state, "governance_blocked", {
            "agent_id": agent_id,
            "reason": tool_verdict.reason,
            "policy": tool_verdict.policy_name,
        })
        return {
            "result": ChatResult(
                text=f"⚠️ Governance policy blocked this request: "
                     f"{tool_verdict.reason}",
                model="", route_reason="governance_blocked",
                tokens_in=0, tokens_out=0, cost_usd=0.0,
                message_id=str(uuid.uuid4()),
            ),
        }

    budget_verdict = orch._governance.check_token_budget(
        tokens_used=target.max_tokens,
        agent_id=agent_id,
        task_key=conversation_id,
    )
    if not budget_verdict.allowed:
        _emit(state, "governance_blocked", {
            "agent_id": agent_id,
            "reason": budget_verdict.reason,
            "policy": budget_verdict.policy_name,
        })
        return {
            "result": ChatResult(
                text=f"⚠️ Token budget exceeded: {budget_verdict.reason}",
                model="", route_reason="governance_budget",
                tokens_in=0, tokens_out=0, cost_usd=0.0,
                message_id=str(uuid.uuid4()),
            ),
        }
    return {}


def compute_flags(state: TurnState) -> dict:
    """Compute split / camel / voting flags and pre-allocate asst_msg_id.

    Mirrors send():1439-1497.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    user_message = state["user_message"]
    full_system = state["full_system"]
    security = state["security"]
    target = state["target"]
    mem = state["mem"]

    split_enabled = bool(
        orch._settings.get("reader_actor_split_enabled", False)
    )
    camel_enabled = bool(orch._settings.get("camel_enabled", False))
    camel_active = camel_enabled and bool(mem.rag_chunks)
    if camel_active:
        split_enabled = False  # CaMeL takes precedence

    voting_enabled = bool(
        orch._settings.get("high_stakes_voting_enabled", True)
    )
    risk_score = (
        security.risk_assessment.cumulative_score
        if security.risk_assessment else 0.0
    )
    escalation_will_trigger = (
        orch._governance.escalation_channel.would_trigger(
            user_message, full_system,
        )
    )
    is_high_stakes = (
        escalation_will_trigger
        or is_high_stakes_message(user_message)
        or risk_score > 0.7
    )
    should_vote = (
        is_high_stakes
        and voting_enabled
        and target.backend == "claude"
        and not camel_active
    )
    return {
        "split_enabled": split_enabled,
        "camel_active": camel_active,
        "should_vote": should_vote,
        "asst_msg_id": str(uuid.uuid4()),
        "voting_samples": None,
        "response_text": "",
        "tokens_in": 0,
        "tokens_out": 0,
        "model_name": target.model_name,
        "had_error": False,
    }


def phase8_voting(state: TurnState) -> dict:
    """Phase 8: Symphony-style weighted-vote consensus.

    Mirrors send():1504-1524. Only fires when should_vote AND not split.
    """
    if _short_circuit(state):
        return {}
    if not state.get("should_vote") or state.get("split_enabled"):
        return {}
    orch = state["orchestrator"]
    target = state["target"]
    asst_msg_id = state["asst_msg_id"]

    _emit(state, "chat_event", {
        "type": "high_stakes_voting_started",
        "message_id": asst_msg_id,
    })
    voting_result, voting_samples = orch._high_stakes_consensus(
        state["decision"], state["full_system"], state["messages"],
        max_tokens=target.max_tokens,
        on_token=state.get("on_token"),
    )
    _emit(state, "chat_event", {
        "type": "high_stakes_voting_complete",
        "message_id": asst_msg_id,
    })
    update: dict[str, Any] = {
        "voting_samples": voting_samples,
        "response_text": voting_result.text,
        "tokens_in": voting_result.input_tokens,
        "tokens_out": voting_result.output_tokens,
        "model_name": voting_result.model_name or target.model_name,
    }
    if voting_result.had_error:
        update["had_error"] = True
    return update


def phase5_escalation_check(state: TurnState) -> dict:
    """Phase 5: Wiser-Human escalation channel.

    Mirrors send():1533-1569. Returns escalation_pending early-exit
    when the channel fires, preserving any consensus samples in router_log.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    ctx = state["ctx"]
    conversation_id = state["conversation_id"]

    verdict = orch._governance.escalation_channel.check_escalation(
        conversation_id=conversation_id,
        user_message=state["user_message"],
        system_prompt=state["full_system"],
        proposed_action=None,
    )
    if verdict.requires_review:
        _emit(state, "escalation_required", {
            "escalation_id": verdict.escalation_id,
            "trigger_type": verdict.trigger_type,
            "trigger_detail": verdict.trigger_detail,
            "conversation_id": conversation_id,
        })
        voting_samples = state.get("voting_samples")
        if voting_samples is not None:
            from services.chat_orchestrator import _log_router_event
            _log_router_event(
                conversation_id=conversation_id,
                message_preview=state["user_message"],
                route_taken=state["route_model"],
                complexity=state["complexity"],
                reasoning="voting before escalation_pending",
                tokens_out=state.get("tokens_out", 0),
                had_error=state.get("had_error", False),
                response_empty=True,
                model_used=state.get("model_name", ""),
                agent_role="monolithic",
                voting_samples_json=json.dumps(voting_samples),
                turn_id=ctx.turn_id,
            )
        return {
            "result": ChatResult(
                text="Awaiting your review for this action.",
                model="", route_reason="escalation_pending",
                tokens_in=state.get("tokens_in", 0),
                tokens_out=state.get("tokens_out", 0),
                cost_usd=0.0,
                message_id=str(uuid.uuid4()),
            ),
        }
    return {}


def phase12_camel(state: TurnState) -> dict:
    """Phase 12: CaMeL Privileged/Quarantined LLM split.

    Mirrors send():1571-1648. Mutually exclusive with split. Falls back
    to monolithic via ``camel_active=False`` on internal failure so a
    crash never eats the user's message.
    """
    if _short_circuit(state):
        return {}
    if not state.get("camel_active"):
        return {}
    orch = state["orchestrator"]
    conversation_id = state["conversation_id"]
    agent_id = state.get("agent_id")
    mem = state["mem"]
    target = state["target"]
    on_token = state.get("on_token")
    try:
        from services.camel import (
            camel_plan_and_execute, make_tool_executor_for_turn,
        )
        tool_executor = make_tool_executor_for_turn(
            agent_id=agent_id or "",
            conversation_id=conversation_id,
            governance=orch._governance,
            execution_bridge=None,
        )
        _emit(state, "camel_started", {"rag_chunks": len(mem.rag_chunks)})
        camel_result = camel_plan_and_execute(
            user_message=state["user_message"],
            retrieved_chunks=list(mem.rag_chunks),
            privileged_client=orch.claude,
            quarantined_client=orch.local if (
                orch.local
                and getattr(orch.local, "is_available", lambda: False)()
            ) else orch.claude,
            tool_executor=tool_executor,
        )
        response_text = camel_result.get("output_text", "") or ""
        if on_token and response_text:
            try:
                on_token(response_text)
            except Exception:
                pass
        try:
            _db.execute(
                "INSERT INTO camel_log "
                "(id, conversation_id, plan_source, executed_steps, "
                "capability_violations, blocked_calls, output_text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    conversation_id,
                    camel_result.get("plan_source", "") or "",
                    int(camel_result.get("executed_steps", 0) or 0),
                    int(camel_result.get("capability_violations", 0) or 0),
                    json.dumps(camel_result.get("blocked_calls", []) or []),
                    response_text,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            _db.commit()
        except Exception as exc:
            log.debug("camel_log insert failed: %s", exc)
        _emit(state, "camel_complete", {
            "executed_steps": camel_result.get("executed_steps", 0),
            "capability_violations": camel_result.get(
                "capability_violations", 0,
            ),
            "blocked_calls": len(
                camel_result.get("blocked_calls", []) or []
            ),
            "error": camel_result.get("error", "") or "",
        })
        return {
            "response_text": response_text,
            "model_name": target.model_name,
        }
    except Exception as exc:
        log.warning(
            "CaMeL pipeline crashed, falling back to monolithic: %s", exc,
        )
        # Re-enable downstream paths so the turn still produces an answer.
        return {"response_text": "", "camel_active": False}


def phase6_split(state: TurnState) -> dict:
    """Phase 6: Hackett et al. Reader/Actor split (3-phase).

    Mirrors send():1657-1700. The Actor never sees the raw user message
    or retrieved data — only the Reader's structured plan.
    """
    if _short_circuit(state):
        return {}
    if not state.get("split_enabled"):
        return {}
    orch = state["orchestrator"]
    ctx = state["ctx"]
    conversation_id = state["conversation_id"]
    agent_id = state.get("agent_id")
    target = state["target"]
    on_token = state.get("on_token")
    try:
        reader_output = orch._read_phase(
            conversation_id=conversation_id,
            user_message=state["user_message"],
            agent_id=agent_id,
            history=state["messages"],
            mem=state["mem"],
            turn_id=ctx.turn_id,
        )
        _emit(state, "reader_complete", {
            "intent": reader_output.intent[:200],
            "proposed_tools": list(reader_output.proposed_tools),
            "red_flags": list(reader_output.red_flags),
        })

        # Build an emit shim that matches the legacy positional call
        # convention used by _act_phase's voting telemetry.
        def _act_emit(event_type: str, data: dict) -> None:
            _emit(state, event_type, data)

        actor_result, actor_voting_samples = orch._act_phase(
            conversation_id=conversation_id,
            reader_output=reader_output,
            history=state["messages"],
            full_system=state["system_prompt"],  # persona only — not full_system
            agent_id=agent_id,
            on_token=on_token,
            max_tokens=target.max_tokens,
            vote=state.get("should_vote", False),
            voting_message_id=state["asst_msg_id"],
            voting_emit=_act_emit,
            turn_id=ctx.turn_id,
        )
        final = orch._synthesize_phase(reader_output, actor_result)
        update: dict[str, Any] = {
            "response_text": final.text,
            "tokens_in": final.input_tokens,
            "tokens_out": final.output_tokens,
            "model_name": final.model_name or target.model_name,
        }
        if actor_voting_samples is not None:
            update["voting_samples"] = actor_voting_samples
        if final.had_error:
            update["had_error"] = True
        return update
    finally:
        # Per-turn ledger: clear so the next turn re-establishes from its
        # own Reader output (no stale carry-over between turns).
        orch._governance.clear_proposed_tools(conversation_id)


def interleaved_reasoning(state: TurnState) -> dict:
    """v4.0 #4 extended thinking on complex Claude turns.

    Mirrors send():1707-1742. Skipped when voting already produced
    response_text, when the split ran, when streaming, or with images.
    """
    if _short_circuit(state):
        return {}
    if state.get("split_enabled") or state.get("response_text"):
        return {}
    orch = state["orchestrator"]
    target = state["target"]
    reasoning_enabled = orch._settings.get("interleaved_reasoning_enabled", True)
    if not reasoning_enabled:
        return {}
    if target.backend != "claude":
        return {}
    if state.get("complexity") != "complex":
        return {}
    if state.get("on_token"):
        return {}
    if state.get("image_attachments"):
        return {}
    try:
        _emit(state, "reasoning_started", {
            "label": "Extended reasoning…",
            "detail": "Claude is thinking through your request",
        })
        thinking_result = orch.claude.extended_thinking_chat(
            system=state["full_system"],
            user_message=state["user_message"],
            budget_tokens=5000,
        )
        if thinking_result.get("thinking"):
            _emit(state, "reasoning_complete", {
                "label": "Reasoning complete",
                "thinking_preview": thinking_result["thinking"][:200],
                "detail": f"{len(thinking_result['thinking'])} chars of reasoning",
            })
            answer = thinking_result.get("answer", "")
            if answer:
                return {"response_text": answer}
    except Exception as exc:
        log.debug("Extended thinking skipped: %s", exc)
    return {}


def monolithic_dispatch(state: TurnState) -> dict:
    """Fallback monolithic worker invocation.

    Mirrors send():1753-1791. Skipped when split ran or response_text
    was already produced by CaMeL / voting / interleaved reasoning.
    """
    if _short_circuit(state):
        return {}
    if state.get("response_text") or state.get("split_enabled"):
        return {}
    orch = state["orchestrator"]
    ctx = state["ctx"]
    target = state["target"]
    image_attachments = state["image_attachments"]

    if image_attachments and target.backend == "local":
        try:
            text = orch.local.chat_with_images(
                state["full_system"], state["messages"],
                [img["data"] for img in image_attachments],
                max_tokens=target.max_tokens,
            )
            response_text = text or ""
            if state.get("on_token") and response_text:
                try:
                    state["on_token"](response_text)
                except Exception:
                    pass
            return {
                "response_text": response_text,
                "tokens_in": 0,
                "tokens_out": 0,
            }
        except LocalVisionUnavailable as exc:
            return {
                "response_text": (
                    f"\U0001f5bc️ {exc}. Switch to a vision-capable "
                    f"model and resend."
                ),
                "had_error": True,
            }
        except Exception as exc:
            log.warning("local vision invocation failed: %s", exc)
            return {"response_text": f"[Error: {exc}]", "had_error": True}

    worker_result = orch._worker_dispatch.dispatch(
        state["decision"], state["full_system"], state["messages"],
        max_tokens=target.max_tokens, on_token=state.get("on_token"),
    )
    update: dict[str, Any] = {
        "response_text": worker_result.text,
        "tokens_in": worker_result.input_tokens,
        "tokens_out": worker_result.output_tokens,
    }
    if worker_result.had_error:
        update["had_error"] = True
    # QLPT Stage 1: surface per-token logprobs for the escalation ladder.
    ctx.worker_logprobs = worker_result.logprobs
    return update


def alignment_check(state: TurnState) -> dict:
    """Best-effort agent alignment check (post-assembly).

    Mirrors send():1798-1842. Emits alignment_warning events and logs to
    agent_performance; never blocks or rewrites the response.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    agent_id = state.get("agent_id")
    response_text = state.get("response_text", "")
    user_message = state["user_message"]
    if state.get("had_error") or agent_id is None or not response_text:
        return {}
    if len(user_message.split()) < 8:
        return {}
    try:
        from services.task_artifacts import local_first_call
        align_raw = local_first_call(
            orch.local, None,
            "Does this response address the user's original request? "
            "Return ONLY JSON: {\"aligned\": true/false, \"reason\": "
            "\"one sentence\"}",
            f"REQUEST: {user_message[:300]}\nRESPONSE: {response_text[:500]}",
            max_tokens=100,
        )
        if align_raw:
            _astart = align_raw.find("{")
            _aend = align_raw.rfind("}")
            if _astart != -1 and _aend != -1 and _aend > _astart:
                try:
                    parsed = json.loads(align_raw[_astart:_aend + 1])
                except (ValueError, TypeError):
                    parsed = {}
                if parsed.get("aligned") is False:
                    _emit(state, "alignment_warning", {
                        "reason": parsed.get(
                            "reason",
                            "Response may not address your request",
                        ),
                    })
                try:
                    _db.execute(
                        "INSERT INTO agent_performance "
                        "(id, agent_id, conversation_id, aligned, "
                        "quality_score, tokens_used, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()), agent_id,
                            state["conversation_id"],
                            1 if parsed.get("aligned", True) else 0,
                            None,
                            state.get("tokens_in", 0) + state.get("tokens_out", 0),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    _db.commit()
                except Exception:
                    pass
    except Exception:
        pass
    return {}


def escalation_ladder(state: TurnState) -> dict:
    """EscalationLadder.maybe_escalate (empty-response rung + quality gate).

    Mirrors send():1848-1867.
    """
    if _short_circuit(state):
        return {}
    orch = state["orchestrator"]
    outcome = orch._escalation_ladder.maybe_escalate(
        ctx=state["ctx"],
        decision=state["decision"],
        target=state["target"],
        full_system=state["full_system"],
        messages=state["messages"],
        response_text=state.get("response_text", ""),
        tokens_in=state.get("tokens_in", 0),
        tokens_out=state.get("tokens_out", 0),
        route_model=state["route_model"],
        model_name=state.get("model_name", ""),
        had_error=state.get("had_error", False),
        split_enabled=state.get("split_enabled", False),
    )
    return {
        "response_text": outcome.response_text,
        "tokens_in": outcome.tokens_in,
        "tokens_out": outcome.tokens_out,
        "route_model": outcome.route_model,
        "model_name": outcome.model_name,
        "response_empty": outcome.response_empty,
    }


def finalize_turn(state: TurnState) -> dict:
    """Router log + TurnLifecycle.close + memory update + ephemeral purge.

    Mirrors send():1869-1949.
    """
    if _short_circuit(state):
        # Even on early-exit, still attempt the lifecycle close so token
        # counters and message rows reflect the truncated turn — but only
        # if open() actually ran (state has a ctx). The legacy body
        # returns directly on early-exit without close(); we match that
        # to preserve byte-identical behavior.
        return {}
    orch = state["orchestrator"]
    ctx = state["ctx"]
    conversation_id = state["conversation_id"]
    user_message = state["user_message"]
    response_text = state.get("response_text", "")
    voting_samples = state.get("voting_samples")
    response_empty = state.get("response_empty", False)
    had_error = state.get("had_error", False)
    tokens_in = state.get("tokens_in", 0)
    tokens_out = state.get("tokens_out", 0)
    route_model = state["route_model"]
    route_reason = state["route_reason"]
    model_name = state.get("model_name", "")
    split_enabled = state.get("split_enabled", False)
    camel_active = state.get("camel_active", False)

    turn_failed = had_error or response_text.startswith("[Error")
    mast_category: Optional[str] = None
    if turn_failed:
        try:
            mast_category = orch.hub_router.classify_failure(
                user_message, response_text,
                response_text if response_text.startswith("[Error") else "",
            )
        except Exception as exc:
            log.debug("MAST classify_failure skipped: %s", exc)

    if not split_enabled:
        from services.chat_orchestrator import _log_router_event
        _log_router_event(
            conversation_id=conversation_id,
            message_preview=user_message,
            route_taken=route_model,
            complexity=state["complexity"],
            reasoning=("camel plan+execute" if camel_active else route_reason),
            tokens_out=tokens_out,
            had_error=turn_failed,
            response_empty=response_empty,
            model_used=model_name,
            mast_category=mast_category,
            agent_role=("camel" if camel_active else "monolithic"),
            voting_samples_json=(
                json.dumps(voting_samples) if voting_samples is not None
                else None
            ),
            turn_id=ctx.turn_id,
        )

    from services.chat_orchestrator import _estimate_cost
    cost = _estimate_cost(model_name, tokens_in, tokens_out, orch._settings)
    close_result = orch._turn_lifecycle.close(
        ctx,
        asst_msg_id=state["asst_msg_id"],
        response_text=response_text,
        route_reason=route_reason,
        model_name=model_name,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
    )
    budget_warning = close_result.budget_warning
    orch._turn_lifecycle.maybe_auto_title(ctx, response_text)

    orch.memory.add_to_buffer(conversation_id, "user", user_message)
    orch.memory.add_to_buffer(conversation_id, "assistant", response_text)
    orch.memory.extract_facts(conversation_id, user_message, response_text)

    try:
        orch._purge_ephemeral_attachments(conversation_id)
    except Exception as exc:
        log.debug("ephemeral attachment purge failed: %s", exc)

    return {
        "result": ChatResult(
            text=response_text,
            model=model_name,
            route_reason=route_reason,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            message_id=state["asst_msg_id"],
            budget_warning=budget_warning,
        ),
    }


# ── Graph builder ────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _compiled_graph():
    """Build and cache the compiled StateGraph.

    The compiled runnable is stateless across invocations (state is
    constructed per turn in run_turn_graph); caching avoids re-compiling
    on every send().
    """
    from langgraph.graph import StateGraph, START, END

    g: StateGraph = StateGraph(TurnState)
    nodes = [
        ("open_turn",                open_turn),
        ("team_check",               team_check),
        ("load_agent",               load_agent),
        ("load_context",             load_context),
        ("memory_recall",            memory_recall),
        ("route_decision",           route_decision),
        ("resolve_target",           resolve_target),
        ("security_gate",            security_gate),
        ("governance_check",         governance_check),
        ("compute_flags",            compute_flags),
        ("phase8_voting",            phase8_voting),
        ("phase5_escalation_check",  phase5_escalation_check),
        ("phase12_camel",            phase12_camel),
        ("phase6_split",             phase6_split),
        ("interleaved_reasoning",    interleaved_reasoning),
        ("monolithic_dispatch",      monolithic_dispatch),
        ("alignment_check",          alignment_check),
        ("escalation_ladder",        escalation_ladder),
        ("finalize_turn",            finalize_turn),
    ]
    for name, fn in nodes:
        g.add_node(name, fn)
    g.add_edge(START, nodes[0][0])
    for (prev, _), (nxt, _) in zip(nodes, nodes[1:]):
        g.add_edge(prev, nxt)
    g.add_edge(nodes[-1][0], END)
    return g.compile()


# ── Entry point ──────────────────────────────────────────────────────────────

def run_turn_graph(orchestrator, conversation_id: str, user_message: str,
                   agent_id: Optional[str] = None,
                   on_token=None, on_event=None) -> ChatResult:
    """Run one chat turn through the StateGraph engine.

    The orchestrator instance is threaded through state as the holder of
    every service the nodes need. The compiled graph is cached so
    repeated invocations don't pay the build cost.
    """
    initial: TurnState = {
        "orchestrator":    orchestrator,
        "conversation_id": conversation_id,
        "user_message":    user_message,
        "agent_id":        agent_id,
        "on_token":        on_token,
        "on_event":        on_event,
        "result":          None,
    }
    final_state = _compiled_graph().invoke(initial)
    result = final_state.get("result")
    if result is None:
        # Defensive: should not happen — finalize_turn always sets result.
        log.error("orchestrator_graph: final state missing result; "
                  "falling back to empty ChatResult")
        return ChatResult(
            text="",
            model="",
            route_reason="graph_no_result",
            tokens_in=0, tokens_out=0, cost_usd=0.0,
            message_id=str(uuid.uuid4()),
        )
    return result
