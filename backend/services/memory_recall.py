"""
services/memory_recall.py — Memory recall + system-prompt assembly.

The first extraction in the Layer 3 decomposition of ChatOrchestrator.send().
Owns three concerns previously inlined into the orchestrator:

  1. Recall: ``memory.get_context()`` + buffer summarisation trigger.
  2. Adaptive trimming: cap RAG chunks per complexity tier (Engram-inspired).
  3. System-prompt assembly: stitch ``system_prompt + mem_suffix + tool
     restrictions + MCP tool descriptions`` into one final string.

The orchestrator keeps the SSE emission and the routing decision; this
module returns a ``MemoryRecallResult`` and lets the caller decide what
to do with it. Single source of truth for the system-prompt rebuild
formerly duplicated between the initial recall and the post-trim branch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from services.memory import MemoryContext

log = logging.getLogger("altosybioagents.memory_recall")

# Engram U-shaped finding: ~25% memory, ~75% reasoning is optimal. For
# simple queries we trim aggressively to avoid RAG noise overwhelming the
# model; complex queries get more headroom. Kept here (not at orchestrator
# scope) because trimming is a memory-recall concern, not a routing one.
MAX_CONTEXT_ITEMS_BY_COMPLEXITY: dict = {"simple": 2, "medium": 4, "complex": 8}
DEFAULT_MAX_CONTEXT_ITEMS = 4


@dataclass
class MemoryRecallResult:
    """The output of MemoryRecall.recall() / trim_for_complexity().

    Mutability is deliberate: ``trim_for_complexity`` mutates the wrapped
    ``MemoryContext`` in place and rebuilds ``full_system``. Callers that
    need an immutable view can take a copy themselves.
    """
    mem:            MemoryContext
    mem_suffix:     str
    full_system:    str
    guidance_block: str = ""


class MemoryRecall:
    """Memory recall + system-prompt assembly for the chat turn."""

    def __init__(self, memory, settings, mcp_registry=None):
        self.memory = memory
        self._settings = settings
        self._mcp_registry = mcp_registry

    # ── Public API ──────────────────────────────────────────────────────

    def recall(
        self,
        conversation_id: str,
        user_message: str,
        system_prompt: str,
        allowed_tools: Optional[list] = None,
        agent: Optional[dict] = None,
    ) -> MemoryRecallResult:
        """Pull memory context + assemble the full system prompt.

        ``allowed_tools`` and ``agent`` are optional: when present, the
        result's ``full_system`` carries tool-restriction and MCP-tool
        sections respectively. Missing values just produce a shorter
        prompt — same behavior the inline orchestrator code had.
        """
        mem = self.memory.get_context(conversation_id, user_message)
        guidance_block = self._guidance_block(user_message, agent)
        return self._assemble(
            mem, system_prompt, allowed_tools, agent, guidance_block,
        )

    def trim_for_complexity(
        self,
        result: MemoryRecallResult,
        complexity: str,
        system_prompt: str,
        allowed_tools: Optional[list] = None,
        agent: Optional[dict] = None,
    ) -> MemoryRecallResult:
        """Cap RAG chunks per complexity tier, rebuild the system prompt.

        Returns the same ``MemoryRecallResult`` instance with its
        ``MemoryContext`` mutated and ``full_system`` re-stitched. Idempotent
        when no trim was needed: the result is returned as-is.
        """
        max_items = MAX_CONTEXT_ITEMS_BY_COMPLEXITY.get(
            complexity, DEFAULT_MAX_CONTEXT_ITEMS,
        )
        if len(result.mem.rag_chunks) <= max_items:
            return result
        log.debug(
            "Memory budget: trimming RAG from %d to %d chunks (%s)",
            len(result.mem.rag_chunks), max_items, complexity,
        )
        result.mem.rag_chunks = result.mem.rag_chunks[:max_items]
        return self._assemble(
            result.mem, system_prompt, allowed_tools, agent,
            result.guidance_block,
        )

    def maybe_summarize(self, conversation_id: str) -> None:
        """Trigger the memory buffer summariser if the manager says so."""
        try:
            if self.memory.should_summarize(conversation_id):
                self.memory.summarize_buffer(conversation_id)
        except Exception as exc:
            log.debug("memory summarize skipped: %s", exc)

    @staticmethod
    def memory_recalled_event(mem: MemoryContext) -> dict:
        """SSE payload mirroring the inline event the orchestrator used to emit."""
        return {
            "facts_count": len(mem.session_facts),
            "rag_chunks":  len(mem.rag_chunks),
            "memories":    len(mem.memories),
        }

    # ── Internals ───────────────────────────────────────────────────────

    def _assemble(
        self,
        mem: MemoryContext,
        system_prompt: str,
        allowed_tools: Optional[list],
        agent: Optional[dict],
        guidance_block: str = "",
    ) -> MemoryRecallResult:
        """Build (mem_suffix, full_system) from the contributing parts.

        Pre-Layer-3 the orchestrator had two near-identical copies of this
        logic — once at initial recall, once after the RAG trim. Centralising
        here makes RAG-trim correctness a one-line concern. ``guidance_block``
        (Feature 2) is computed once in ``recall`` and threaded through so it
        survives the RAG-trim rebuild.
        """
        mem_suffix = mem.to_system_suffix()
        full_system = system_prompt
        if mem_suffix:
            full_system = system_prompt + "\n\n" + mem_suffix
        if allowed_tools:
            full_system += self._tool_restriction_block(allowed_tools)
        if agent and agent.get("skills") and self._mcp_registry is not None:
            mcp_block = self._mcp_tool_block(agent)
            if mcp_block:
                full_system += mcp_block
        if guidance_block:
            full_system += guidance_block
        return MemoryRecallResult(
            mem=mem, mem_suffix=mem_suffix, full_system=full_system,
            guidance_block=guidance_block,
        )

    def _guidance_block(self, user_message: str, agent: Optional[dict]) -> str:
        """Feature 2: retrieve only the rules relevant to this message.

        Flag-gated and best-effort: any failure yields an empty block so the
        turn proceeds with the base prompt unchanged.
        """
        if not self._settings.get("guidance_compiler_enabled", False):
            return ""
        try:
            from services import guidance
            scope = "global"
            if agent and agent.get("id"):
                scope = f"agent:{agent['id']}"
            rules = guidance.retrieve(
                user_message,
                scope=scope,
                top_k=int(self._settings.get("guidance_top_k", 5) or 5),
                min_sim=float(self._settings.get("guidance_min_similarity", 0.45) or 0.45),
            )
            return guidance.format_block(rules)
        except Exception as exc:
            log.debug("guidance block skipped: %s", exc)
            return ""

    @staticmethod
    def _tool_restriction_block(allowed_tools: list) -> str:
        tool_names = ", ".join(allowed_tools)
        return (
            "\n\n## Tool Restrictions\n"
            f"You may ONLY use these tools: {tool_names}. "
            "Do not attempt to use any other tools or capabilities "
            "outside this list."
        )

    def _mcp_tool_block(self, agent: dict) -> str:
        try:
            raw = agent.get("skills")
            agent_skills = json.loads(raw) if isinstance(raw, str) else raw
            skill_names = [
                s.get("name", "") for s in (agent_skills or [])
                if isinstance(s, dict)
            ]
            mcp_tools = self._mcp_registry.get_tools_for_tags(skill_names)
            if not mcp_tools:
                return ""
            tool_lines = "\n".join(
                f"- **{t['name']}**: {t['description']}" for t in mcp_tools[:10]
            )
            return (
                "\n\n## Available External Tools\n"
                "(These tools are available via MCP. Mention them if relevant.)\n\n"
                + tool_lines
            )
        except Exception as exc:
            log.debug("MCP tool block assembly skipped: %s", exc)
            return ""
