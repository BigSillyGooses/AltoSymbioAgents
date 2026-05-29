"""
services/hub_router.py — Phase 1: Centralized hub routing.

Single boundary that selects a worker (agent) for a TaskDescriptor and is the
only call site permitted to invoke ``claude_client`` / ``local_client`` for
worker work.

Routing strategy:
  1. ``route_for_agent(agent_id, task)`` — caller specifies the agent; router
     validates skill/scope authorization, then returns a RoutingDecision.
  2. ``route(task)`` — caller does not specify an agent; router scores all
     skill-declaring agents by deterministic skill match (target p99 < 50ms).
     If no agent's score exceeds ``MIN_SKILL_MATCH_SCORE``, the LLM fallback
     hook fires (filled in by Phase 3 — see ``_llm_fallback``).
  3. ``invoke(decision, system, messages, ...)`` — dispatches the chosen
     decision to the right model client and returns a uniform WorkerResult.

Authz model:
  A task's ``required_scopes`` must be a subset of the chosen worker's declared
  scopes for the matched skill. If not, routing raises ``AuthorizationError``
  rather than silently downgrading.

Design notes:
  - No I/O during ``route()`` other than a single agents-table read; scoring
    is pure Python.
  - ``invoke()`` is the only place under ``app/services/`` (besides the
    orchestrator's bootstrap) allowed to call worker model methods. The Phase 1
    test suite enforces this via static AST inspection.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

import db as _db
from models import (
    ExecutionTarget,
    RoutingDecision,
    Skill,
    TaskDescriptor,
    WorkerResult,
)

log = logging.getLogger("altosybioagents.hub_router")

# Below this score, deterministic routing fails over to the LLM fallback.
MIN_SKILL_MATCH_SCORE: float = 0.5

# Maximum agents to include in the LLM fallback prompt. Five is enough for
# accurate selection while keeping the prompt small enough for models under
# 30B params (which start drifting once tool/skill descriptions pile up).
_MAX_AGENTS_FOR_LLM: int = 5

# Roles that are always included in the LLM fallback list regardless of
# keyword score. The coordinator is special-cased because it's the
# universal fan-out target — without it the fallback can't recover when
# none of the specialists score well.
_ALWAYS_INCLUDED_ROLES: frozenset[str] = frozenset({"coordinator"})

# Words shorter than this are ignored during keyword scoring (filters out
# stopwords like "and", "the", "is" without maintaining a list).
_KEYWORD_MIN_LEN: int = 3

# MAST (Multi-Agent System failure Taxonomy, Cemri et al. 2025) 14 codes.
# Hard-coded so classification never depends on a live network fetch.
_MAST_CATEGORIES: tuple[str, ...] = (
    "1.1",  # Disobey Task Specification
    "1.2",  # Disobey Role Specification
    "1.3",  # Step Repetition
    "1.4",  # Loss of Conversation History
    "1.5",  # Unaware of Termination Conditions
    "2.1",  # Conversation Reset
    "2.2",  # Fail to Ask for Clarification
    "2.3",  # Task Derailment
    "2.4",  # Information Withholding
    "2.5",  # Ignored Other Agent's Input
    "2.6",  # Reasoning-Action Mismatch
    "3.1",  # Premature Termination
    "3.2",  # No or Incomplete Verification
    "3.3",  # Incorrect Verification
)


class AuthorizationError(RuntimeError):
    """Raised when a task's required_scopes are not a subset of the worker's."""


class HubRouter:
    """The hub's single boundary for worker selection and invocation."""

    def __init__(
        self,
        claude_client,
        local_client,
        settings,
        llm_fallback: Optional[Callable[..., RoutingDecision]] = None,
        litellm_client=None,
    ):
        self._claude = claude_client
        self._local = local_client
        self._settings = settings
        # Stage-1 (Atelier plan) BYO-key adapter. Optional — when None, the
        # router rejects ``decision.backend == "litellm"`` so configurations
        # that never wired a third client fail closed instead of silently
        # routing to Claude.
        self._litellm = litellm_client
        # Phase 3 wires Qwen /no_think here; Phase 1 leaves it None and routing
        # raises if it would be needed without a fallback configured. The
        # current contract is ``fallback(task, *, agent_list=...)`` — older
        # fallbacks that only accept ``task`` are still supported through a
        # TypeError catch in route().
        self._llm_fallback = llm_fallback

    # ── Skill scoring (deterministic, no LLM) ────────────────────────────────

    @staticmethod
    def _parse_skills(raw: str | None) -> list[Skill]:
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        out: list[Skill] = []
        for item in data:
            if isinstance(item, dict) and item.get("name"):
                out.append(Skill.from_dict(item))
        return out

    @staticmethod
    def _keyword_score(task_text: str, agent: dict) -> float:
        """Score an agent's relevance to a task by keyword overlap.

        Checks the agent's name, role, and skill names/descriptions against
        the task text. Returns 0.0-1.0. No LLM call, runs in <1ms per agent.
        Used by the route() pre-filter to narrow the LLM-fallback prompt
        when many agents are registered (avoids context rot on small models).
        """
        if not task_text:
            return 0.0

        task_lower = task_text.lower()
        task_words = set(re.findall(rf"\w{{{_KEYWORD_MIN_LEN},}}", task_lower))
        if not task_words:
            return 0.0

        searchable_parts: list[str] = []
        searchable_parts.append((agent.get("name") or "").lower())
        searchable_parts.append((agent.get("role") or "").lower())

        skills_raw = agent.get("skills") or "[]"
        try:
            skills = json.loads(skills_raw) if isinstance(skills_raw, str) else skills_raw
            if isinstance(skills, list):
                for skill in skills:
                    if isinstance(skill, dict):
                        searchable_parts.append((skill.get("name") or "").lower())
                        desc = skill.get("description") or ""
                        if desc:
                            searchable_parts.append(desc.lower())
                    elif isinstance(skill, str):
                        searchable_parts.append(skill.lower())
        except (json.JSONDecodeError, TypeError):
            pass

        agent_text = " ".join(searchable_parts)
        agent_words = set(re.findall(rf"\w{{{_KEYWORD_MIN_LEN},}}", agent_text))
        if not agent_words:
            return 0.0

        overlap = task_words & agent_words
        score = len(overlap) / max(len(task_words), 1)

        # Bonus if the agent's role appears verbatim in the task text — a
        # cheap way to honor explicit asks like "have the writer draft this".
        role = (agent.get("role") or "").lower()
        if role and role in task_lower:
            score += 0.3

        return min(score, 1.0)

    @classmethod
    def _prefilter_agents(cls, task_text: str, agents: list[dict]) -> list[dict]:
        """Pick the agents most likely to be relevant for the LLM fallback.

        Keeps everyone whose role is in ``_ALWAYS_INCLUDED_ROLES`` and the
        top ``_MAX_AGENTS_FOR_LLM`` by keyword score. If the agent list is
        already at or below the cap, returns it unchanged so small teams
        skip the scoring overhead.
        """
        if len(agents) <= _MAX_AGENTS_FOR_LLM:
            return list(agents)

        scored = sorted(
            ((cls._keyword_score(task_text, a), a) for a in agents),
            key=lambda x: x[0],
            reverse=True,
        )

        selected: list[dict] = []
        seen_ids: set[str] = set()
        # Always-included roles first, regardless of score.
        for _, agent in scored:
            role = (agent.get("role") or "").lower()
            if role in _ALWAYS_INCLUDED_ROLES and agent.get("id") not in seen_ids:
                selected.append(agent)
                seen_ids.add(agent.get("id", ""))
        # Then top-scoring agents until we hit the cap.
        for _, agent in scored:
            if len(selected) >= _MAX_AGENTS_FOR_LLM:
                break
            if agent.get("id") in seen_ids:
                continue
            selected.append(agent)
            seen_ids.add(agent.get("id", ""))
        return selected

    @staticmethod
    def _score_match(declared: list[Skill], task: TaskDescriptor) -> tuple[float, str]:
        """
        Return (score, matched_skill_name).

        Score combines:
          - Required-skill coverage: fraction of task.required_skills present in
            declared skill names. Zero means no overlap → unrouteable.
          - Scope fit: required_scopes must be a subset of the declared skill's
            scopes for at least one matched skill.
          - Specificity bonus: agents with fewer total skills get a small boost
            so a generalist doesn't outrank a specialist on equal coverage.
        """
        if not task.required_skills:
            # No skills required: any agent matches with neutral score.
            return (0.5, "")
        declared_names = {s.name for s in declared}
        matched = [s for s in declared if s.name in task.required_skills]
        if not matched:
            return (0.0, "")

        coverage = len(matched) / len(task.required_skills)

        # Pick the matched skill that satisfies scopes; if none, scope=0.
        scope_ok_skill = next(
            (s for s in matched if set(task.required_scopes).issubset(set(s.scopes))),
            None,
        )
        if scope_ok_skill is None:
            return (0.0, "")  # no skill can satisfy scopes → unauthorized

        # Specificity: 1.0 when agent declares only the matched skill,
        # decays as the agent declares unrelated skills.
        specificity = 1.0 / max(1, len(declared_names))
        score = (0.7 * coverage) + (0.3 * specificity)
        # Clamp to [0, 1].
        score = max(0.0, min(1.0, score))
        return (score, scope_ok_skill.name)

    # ── Public routing API ──────────────────────────────────────────────────

    def route_for_agent(self, agent_id: str, task: TaskDescriptor) -> RoutingDecision:
        """Caller-specified agent path. Validates authz; never runs the LLM."""
        row = _db.fetchone(
            "SELECT id, model_preference, skills, thinking_budget FROM agents WHERE id = ?",
            (agent_id,),
        )
        if not row:
            raise AuthorizationError(f"Unknown agent: {agent_id}")

        declared = self._parse_skills(row["skills"])
        score, matched = self._score_match(declared, task)

        # Authz: if the task declared required_skills/scopes, the agent must
        # cover them. If the task is open (no required_skills), we accept any
        # agent — this preserves the existing chat flow where the user picks
        # an agent freely.
        if task.required_skills and score == 0.0:
            raise AuthorizationError(
                f"Agent {agent_id} cannot satisfy required skills "
                f"{list(task.required_skills)} with scopes "
                f"{list(task.required_scopes)}"
            )

        backend = self._resolve_backend(row["model_preference"], task.backend_hint)
        # Phase 3: Cap the per-agent thinking budget by the global ceiling.
        budget = self._capped_budget(row)
        return RoutingDecision(
            agent_id=row["id"],
            backend=backend,
            score=score if task.required_skills else 1.0,
            reasoning=f"caller-selected agent {agent_id}",
            used_fallback=False,
            skill_matched=matched,
            thinking_budget=budget,
        )

    def _capped_budget(self, row) -> int:
        """Resolve a per-agent thinking budget capped by the global setting."""
        try:
            agent_budget = int(row["thinking_budget"] or 0)
        except (KeyError, IndexError, ValueError, TypeError):
            agent_budget = 0
        if agent_budget <= 0:
            return 0
        cap = int(self._settings.get("qwen_thinking_global_budget_cap", 8192) or 0)
        if cap <= 0:
            return agent_budget
        return min(agent_budget, cap)

    def route(self, task: TaskDescriptor) -> RoutingDecision:
        """No-agent-specified path. Picks by skill match across all agents."""
        if task.preferred_agent_id:
            return self.route_for_agent(task.preferred_agent_id, task)

        rows = _db.fetchall(
            "SELECT id, model_preference, skills, thinking_budget FROM agents "
            "WHERE skills IS NOT NULL AND skills != '[]'"
        )

        best_row = None
        best_backend: str = "claude"
        best_score: float = 0.0
        best_skill: str = ""
        for r in rows:
            declared = self._parse_skills(r["skills"])
            score, matched = self._score_match(declared, task)
            if score > best_score:
                best_score = score
                best_row = r
                best_backend = self._resolve_backend(r["model_preference"], task.backend_hint)
                best_skill = matched

        if best_row is not None and best_score >= MIN_SKILL_MATCH_SCORE:
            return RoutingDecision(
                agent_id=best_row["id"],
                backend=best_backend,
                score=best_score,
                reasoning=f"skill-match on '{best_skill}' (score {best_score:.2f})",
                used_fallback=False,
                skill_matched=best_skill,
                thinking_budget=self._capped_budget(best_row),
            )

        # No deterministic winner — use LLM fallback if Phase 3 wired one.
        if self._llm_fallback is None:
            raise RoutingError(
                f"No agent declared a skill matching {list(task.required_skills)}; "
                "LLM fallback not configured."
            )
        # Pre-filter agents by keyword overlap before handing the list to the
        # LLM. With many registered agents the fallback prompt grows fast and
        # small models start ignoring the actual query — a keyword pass picks
        # the top _MAX_AGENTS_FOR_LLM candidates without an extra LLM call.
        all_agents = [dict(r) for r in _db.fetchall(
            "SELECT id, name, role, skills, model_preference FROM agents"
        )]
        prefiltered = self._prefilter_agents(task.text, all_agents) if all_agents else []
        try:
            decision = self._llm_fallback(task, agent_list=prefiltered)
        except TypeError:
            # Fallback predates the agent_list kwarg — call the old signature.
            decision = self._llm_fallback(task)
        return RoutingDecision(
            agent_id=decision.agent_id,
            backend=decision.backend,
            score=decision.score,
            reasoning=decision.reasoning,
            used_fallback=True,
            skill_matched=decision.skill_matched,
        )

    # ── Worker invocation (only call site for model clients) ────────────────

    def invoke(
        self,
        decision: RoutingDecision,
        system: str,
        messages: list,
        max_tokens: int = 4096,
        on_token: Optional[Callable[[str], None]] = None,
        agent_role: str = "monolithic",
    ) -> WorkerResult:
        """Dispatch a routed task to its model client. Single source of truth.

        ``agent_role`` is one of "monolithic" (legacy single-agent path),
        "reader", or "actor" (Phase 6 Reader/Actor split). It is recorded on
        the WorkerResult and downstream router_log row so failure analysis
        can attribute issues to the role that produced them.
        """
        # Track the role for downstream logging. The actual router_log write
        # happens in the orchestrator (single source of truth for that table).
        self._last_agent_role = agent_role
        # Stage-1 (Atelier): the BYO-key LiteLLM adapter is the third backend.
        # Fail closed when the decision targets ``litellm`` but no client was
        # wired into the router — otherwise the call would silently fall
        # through to Claude and bill the user's Anthropic key for a request
        # they meant to send via Groq/Gemini/etc.
        if decision.backend == "litellm":
            if self._litellm is None:
                return WorkerResult(
                    text="[Error: routing chose litellm backend but no LiteLLM client is configured.]",
                    backend="litellm",
                    model_name=decision.skill_matched or "litellm",
                    had_error=True,
                )
            client = self._litellm
        elif decision.backend == "claude":
            client = self._claude
        else:
            client = self._local
        # Per-backend max_tokens caps: claude has no engineering-enforced
        # cap, local is clamped to 2048 (Qwen3-4B context budget), and
        # litellm rides on whatever the provider supports — pass through
        # the caller's request and let LiteLLM/the provider error if it's
        # over the model's limit.
        if decision.backend == "claude" or decision.backend == "litellm":
            local_max = max_tokens
        else:
            local_max = min(max_tokens, 2048)

        # Phase 3: thinking budget for local Qwen models
        if decision.backend == "local" and int(decision.thinking_budget or 0) > 0:
            from services import qwen_thinking
            budget = min(int(decision.thinking_budget), local_max)
            text = qwen_thinking.worker_think(
                self._local, system, messages,
                budget_tokens=budget, on_token=on_token,
            )
            return WorkerResult(
                text=text or "", backend="local",
                model_name=client.client_name(),
            )

        try:
            if on_token:
                result = client.stream_unified(system, messages, on_token, max_tokens=local_max)
            else:
                result = client.chat_unified(system, messages, max_tokens=local_max)
            raw_logprobs = result.get("logprobs") if isinstance(result, dict) else None
            return WorkerResult(
                text=result["text"],
                backend=decision.backend,
                model_name=client.client_name(),
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
                logprobs=tuple(raw_logprobs) if raw_logprobs else None,
            )
        except Exception as exc:
            log.error("%s invocation failed: %s", decision.backend, exc)
            return WorkerResult(
                text=f"[Error: {exc}]",
                backend=decision.backend,
                model_name=client.client_name(),
                had_error=True,
            )

    # ── MAST failure-mode classification ────────────────────────────────────

    def classify_failure(
        self, user_message: str, response: str, error: str
    ) -> str:
        """Map a failed turn to one of the 14 MAST codes.

        Best-effort: prefers Claude when available, falls back to the local
        client. Returns a code string from ``_MAST_CATEGORIES``; on any parse
        or call failure raises so the caller can record NULL.
        """
        prompt = (
            "Classify the failure of an AI assistant turn into exactly one "
            "MAST (Multi-Agent System failure Taxonomy) code. Reply with "
            "ONLY the code (e.g. '1.1' or '3.2'), no prose. Valid codes: "
            "1.1 Disobey Task Specification, 1.2 Disobey Role Specification, "
            "1.3 Step Repetition, 1.4 Loss of Conversation History, "
            "1.5 Unaware of Termination Conditions, 2.1 Conversation Reset, "
            "2.2 Fail to Ask for Clarification, 2.3 Task Derailment, "
            "2.4 Information Withholding, 2.5 Ignored Other Agent's Input, "
            "2.6 Reasoning-Action Mismatch, 3.1 Premature Termination, "
            "3.2 No or Incomplete Verification, 3.3 Incorrect Verification."
        )
        user_block = (
            f"USER MESSAGE: {(user_message or '')[:600]}\n"
            f"RESPONSE: {(response or '')[:600]}\n"
            f"ERROR: {(error or '')[:300]}"
        )
        raw: str | None = None
        if self._claude is not None:
            try:
                result = self._claude.chat_multi_turn(
                    prompt,
                    [{"role": "user", "content": user_block}],
                    max_tokens=8,
                )
                raw = result.get("text") if isinstance(result, dict) else result
            except Exception:
                raw = None
        if not raw and self._local is not None:
            try:
                raw = self._local.chat(prompt, user_block, max_tokens=8)
            except Exception:
                raw = None
        if not raw:
            raise RuntimeError("no classifier output")

        match = re.search(r"\b([123]\.[1-6])\b", raw)
        if not match:
            raise ValueError(f"no MAST code in {raw!r}")
        code = match.group(1)
        if code not in _MAST_CATEGORIES:
            raise ValueError(f"unknown MAST code {code!r}")
        return code

    def target_for(self, decision: RoutingDecision, max_tokens: int) -> ExecutionTarget:
        """Public helper so the orchestrator can build an ExecutionTarget."""
        if decision.backend == "claude":
            return ExecutionTarget(
                backend="claude",
                model_name=getattr(self._claude, "_model", "claude"),
                max_tokens=max_tokens,
            )
        if decision.backend == "litellm":
            # The LiteLLM client carries its own model name (``openai/gpt-4o``
            # etc.). Falls back to a generic label when the client wasn't
            # wired so the resulting ExecutionTarget still validates.
            return ExecutionTarget(
                backend="litellm",
                model_name=getattr(self._litellm, "_model", "litellm"),
                max_tokens=max_tokens,
            )
        return ExecutionTarget(
            backend="local",
            model_name=self._settings.get("default_local_model", "local"),
            max_tokens=min(max_tokens, 2048),
        )

    # ── Backend resolution (mirrors orchestrator's pre-Phase-1 logic) ────────

    @staticmethod
    def _resolve_backend(model_preference: str | None, hint: Optional[str]) -> str:
        # Agent preference is a hard constraint; backend_hint is advisory and
        # only consulted when the agent is configured for ``auto``. This
        # matches the pre-Phase-1 orchestrator logic where model_preference
        # short-circuited the TaskRouter.
        pref = (model_preference or "auto").lower()
        if pref == "claude":
            return "claude"
        if pref == "local":
            return "local"
        if pref == "litellm":
            return "litellm"
        if hint in ("claude", "local", "litellm"):
            return hint
        return "claude"


class RoutingError(RuntimeError):
    """Raised when no agent can be routed and no fallback is available."""

