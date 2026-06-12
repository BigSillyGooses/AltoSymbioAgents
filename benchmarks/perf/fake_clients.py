"""
benchmarks/perf/fake_clients.py — Deterministic model clients for the harness.

``FakeClaudeClient`` implements the ``LLMClient`` interface (chat_unified /
stream_unified / is_available / client_name) so it slots into HubRouter /
ChatOrchestrator anywhere the real ClaudeClient does, plus ``chat_multi_turn``
for the few callers (MAST classifier, conftest patterns) that use the raw
shape. Replies are scripted and cycled; token counts are ``len(text) // 4``
so two runs over the same fixture produce identical numbers.

The interesting part is the **Anthropic prefix-cache simulation** — see
``_simulate_cache`` for the one commented place that encodes the real API
rules. It lets the harness measure cache hit rates and cached-aware cost
without an API key, and lets tests assert the exact creation/read accounting
that Phase 3 (history caching + compaction) will be graded against.

``FakeLocalClient`` is the same idea for the local backend: scripted replies,
configurable simulated latency (for wall-clock scenarios), always-zero cache
counts (local backends have no prefix cache).

``FakeTaskRouter`` is a two-line stand-in for ``services.router.TaskRouter``
returning a fixed RouteDecision, so scenario routing is fixture-controlled
rather than heuristic-controlled.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Callable

from services.llm_interface import LLMClient

# Minimum cacheable prefix in fixture tokens. The real minimum is
# model-dependent (2048 tokens on claude-sonnet-4-6, 4096 on Opus-tier);
# the harness fixes it at the sonnet value. Constructor-overridable so
# tests can exercise the rule without 8KB fixtures.
MIN_CACHEABLE_PREFIX_TOKENS = 2048

# The real API enforces at most 4 cache_control breakpoints per request.
MAX_CACHE_BREAKPOINTS = 4

# When looking for a hit, the real API checks the content-block boundaries
# BEFORE each breakpoint (roughly 20 blocks of lookback) for the longest
# already-cached prefix. This is what makes a per-turn-advancing history
# breakpoint (Phase 3: the marker moves 2 blocks forward each turn) read the
# previous turn's written prefix instead of rewriting from byte 0.
CACHE_HIT_LOOKBACK_BLOCKS = 20


def count_tokens(text: str) -> int:
    """Fixture tokenizer: 4 chars ≈ 1 token. Deterministic by construction."""
    return len(text or "") // 4


class FakeClaudeClient(LLMClient):
    """Scripted Claude stand-in with simulated Anthropic prefix caching."""

    def __init__(
        self,
        replies: list[str],
        model: str = "claude-sonnet-4-6",
        use_caching: bool = True,
        min_cacheable_prefix_tokens: int = MIN_CACHEABLE_PREFIX_TOKENS,
        simulated_latency_ms: float = 0.0,
        keyed_replies: dict[str, str] | None = None,
    ):
        self._replies = list(replies) or ["ok"]
        self._reply_idx = 0
        # Perf Phase 5 (additive): simulated wall-clock latency per call —
        # same knob FakeLocalClient has — so parallel-vs-sequential pipeline
        # scenarios can measure real speedups without a network.
        self.simulated_latency_ms = float(simulated_latency_ms)
        # Perf Phase 5 (additive): content-keyed replies. When a key
        # substring appears in the rendered request, its reply is returned
        # instead of the next cycled one (first matching key in insertion
        # order wins). This keeps parallel-pipeline scenarios deterministic:
        # concurrent steps reach the client in nondeterministic order, so
        # positional cycling would shuffle the reply↔step assignment from
        # run to run. Empty/None → pure cycling, exactly as before.
        self._keyed_replies = dict(keyed_replies or {})
        # Reply cursor + call log are shared mutable state; the parallel
        # pipeline calls _respond from several threads at once.
        self._respond_lock = threading.Lock()
        # ``_model`` mirrors the real ClaudeClient attribute — the
        # orchestrator's _resolve_target / hub_router.target_for read it.
        self._model = model
        # Mirrors ClaudeClient._use_caching: when True, a plain-string system
        # prompt is treated as one cache_control block (the production
        # _build_system_with_cache behavior).
        self._use_caching = use_caching
        self._min_prefix_tokens = int(min_cacheable_prefix_tokens)
        # The simulated server-side cache: hashes of rendered prefixes that
        # have been written. In-memory and per-client — the harness builds a
        # fresh client per scenario run, which is what makes runs identical.
        self._cached_prefix_hashes: set[str] = set()
        # Per-call usage log so scenarios/tests can audit every request.
        self.calls: list[dict] = []

    # ── Cache bookkeeping ────────────────────────────────────────────────

    def reset_cache(self) -> None:
        """Forget all cached prefixes (simulates the 5-minute TTL expiring)."""
        self._cached_prefix_hashes.clear()

    # ── Request rendering ────────────────────────────────────────────────

    @staticmethod
    def _render_block(block) -> str:
        """Deterministic text for one content block (text or otherwise)."""
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
        return json.dumps(block, sort_keys=True, default=str)

    def _render_request(self, system, messages) -> tuple[str, list[str], list[int]]:
        """Render the request the way the API tokenizes it: system first,
        then messages in order. Returns ``(full_text, boundaries,
        marker_positions)`` where ``boundaries[i]`` is the rendered request up
        to and including the i-th content block, and ``marker_positions``
        indexes the boundaries whose block carries a cache_control marker.
        """
        pieces: list[str] = []
        boundaries: list[str] = []
        marker_positions: list[int] = []

        def add(text: str, marked: bool) -> None:
            pieces.append(text)
            boundaries.append("".join(pieces))
            if marked:
                marker_positions.append(len(boundaries) - 1)

        # System: mirror ClaudeClient._build_system_with_cache — a non-empty
        # plain-string system gets one ephemeral cache_control block when
        # caching is on. An explicit block list keeps its own markers.
        if isinstance(system, str):
            if system:
                add("system\x00" + system + "\x00", marked=self._use_caching)
        else:
            for block in system or []:
                add(
                    "system\x00" + self._render_block(block) + "\x00",
                    marked=isinstance(block, dict) and bool(block.get("cache_control")),
                )

        for msg in messages or []:
            role = msg.get("role", "user") if isinstance(msg, dict) else "user"
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            if isinstance(content, str):
                add(role + "\x00" + content + "\x00", marked=False)
            else:
                for block in content:
                    add(
                        role + "\x00" + self._render_block(block) + "\x00",
                        marked=isinstance(block, dict) and bool(block.get("cache_control")),
                    )
        return "".join(pieces), boundaries, marker_positions

    # ── Prefix-cache simulation ──────────────────────────────────────────

    def _simulate_cache(self, full_text: str, boundaries: list[str],
                        marker_positions: list[int]) -> tuple[int, int, int]:
        """Apply Anthropic's prompt-caching rules to one rendered request.

        Returns ``(input_tokens, cache_creation_tokens, cache_read_tokens)``
        where ``input_tokens`` is the UNCACHED portion only — exactly how the
        real API's ``usage`` object reports it.

        ── Anthropic prefix-cache rules, simulated here (single source) ──
        1. At most MAX_CACHE_BREAKPOINTS (4) cache_control markers count per
           request; markers beyond the 4th are ignored.
        2. Matching is a strict byte-prefix match: the cache key is a hash of
           the EXACT rendered request up to and including a content block.
           Changing any earlier byte produces a different hash → cache miss
           for that prefix and everything after it.
        3. Minimum cacheable prefix: a marker whose prefix is shorter than
           ``min_cacheable_prefix_tokens`` (2048 fixture tokens ≈ the
           claude-sonnet-4-6 floor) is SILENTLY ignored — it reports neither
           creation nor read, and its tokens bill as plain input.
        4. Lookback: a hit does not require the marker's OWN prefix to be
           cached — the API checks the content-block boundaries up to
           CACHE_HIT_LOOKBACK_BLOCKS (20) before each breakpoint and reads
           the longest already-cached one. This is why a history breakpoint
           that advances 2 blocks per turn still reads the previous turn's
           prefix back instead of paying a full write every turn.
        5. Billing split: the longest cached boundary found in rule 4 bills
           as ``cache_read_tokens``; tokens between that hit and the longest
           valid marker bill as ``cache_creation_tokens`` (the cache is
           written at every valid breakpoint); everything after the last
           valid marker bills as ordinary ``input_tokens``.
        """
        total = count_tokens(full_text)

        valid_positions = [
            pos for pos in marker_positions[:MAX_CACHE_BREAKPOINTS]   # rule 1
            if count_tokens(boundaries[pos]) >= self._min_prefix_tokens  # rule 3
        ]
        if not valid_positions:
            return total, 0, 0

        candidates: set[int] = set()                                  # rule 4
        for pos in valid_positions:
            candidates.update(
                range(max(0, pos - CACHE_HIT_LOOKBACK_BLOCKS), pos + 1)
            )
        read = 0
        for pos in sorted(candidates, reverse=True):  # longest prefix first
            digest = hashlib.sha256(                                  # rule 2
                boundaries[pos].encode("utf-8")
            ).hexdigest()
            if digest in self._cached_prefix_hashes:
                read = count_tokens(boundaries[pos])
                break

        longest = max(count_tokens(boundaries[p]) for p in valid_positions)
        creation = max(0, longest - read)                             # rule 5
        for pos in valid_positions:  # writes happen at every valid breakpoint
            self._cached_prefix_hashes.add(
                hashlib.sha256(boundaries[pos].encode("utf-8")).hexdigest()
            )
        return total - longest, creation, read

    # ── Scripted replies ─────────────────────────────────────────────────

    def _next_reply(self) -> str:
        text = self._replies[self._reply_idx % len(self._replies)]
        self._reply_idx += 1
        return text

    def _keyed_reply(self, rendered_request: str) -> str | None:
        """First keyed reply whose key appears in the rendered request."""
        for key, reply in self._keyed_replies.items():
            if key in rendered_request:
                return reply
        return None

    def _respond(self, system, messages) -> dict:
        if self.simulated_latency_ms > 0:
            time.sleep(self.simulated_latency_ms / 1000.0)
        full_text, boundaries, marker_positions = self._render_request(system, messages)
        with self._respond_lock:
            input_tokens, creation, read = self._simulate_cache(
                full_text, boundaries, marker_positions,
            )
            text = self._keyed_reply(full_text)
            if text is None:
                text = self._next_reply()
            result = {
                "text": text,
                "input_tokens": input_tokens,
                "output_tokens": count_tokens(text),
                "cache_creation_tokens": creation,
                "cache_read_tokens": read,
            }
            self.calls.append(dict(result))
        return result

    # ── LLMClient interface ──────────────────────────────────────────────

    def chat_unified(self, system, messages, max_tokens: int = 4096) -> dict:
        return self._respond(system, messages)

    def stream_unified(self, system, messages, on_token: Callable[[str], None],
                       max_tokens: int = 4096) -> dict:
        result = self._respond(system, messages)
        if on_token and result["text"]:
            on_token(result["text"])
        return result

    def is_available(self) -> bool:
        return True

    def client_name(self) -> str:
        return self._model

    # ── Raw ClaudeClient shape (MAST classifier, legacy call sites) ──────

    def chat_multi_turn(self, system, messages, max_tokens: int = 4096) -> dict:
        return self._respond(system, messages)


class FakeLocalClient(LLMClient):
    """Scripted local-model stand-in with configurable simulated latency."""

    def __init__(self, replies: list[str] | None = None,
                 simulated_latency_ms: float = 0.0,
                 model: str = "fake-local"):
        self._replies = list(replies or ["local reply."])
        self._reply_idx = 0
        self._model = model
        self.simulated_latency_ms = float(simulated_latency_ms)
        self.calls: list[dict] = []

    def _sleep(self) -> None:
        if self.simulated_latency_ms > 0:
            time.sleep(self.simulated_latency_ms / 1000.0)

    def _next_reply(self) -> str:
        text = self._replies[self._reply_idx % len(self._replies)]
        self._reply_idx += 1
        return text

    def _respond(self) -> dict:
        self._sleep()
        text = self._next_reply()
        result = {
            "text": text,
            "input_tokens": count_tokens(text),
            "output_tokens": count_tokens(text),
            # Local backends have no prefix cache — always 0 (matches what
            # hub_router.invoke records for the local path in production).
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }
        self.calls.append(dict(result))
        return result

    # ── LLMClient interface ──────────────────────────────────────────────

    def chat_unified(self, system, messages, max_tokens: int = 4096) -> dict:
        return self._respond()

    def stream_unified(self, system, messages, on_token: Callable[[str], None],
                       max_tokens: int = 4096) -> dict:
        result = self._respond()
        if on_token and result["text"]:
            on_token(result["text"])
        return result

    def is_available(self, backend: str | None = None) -> bool:
        return True

    def client_name(self) -> str:
        return self._model

    # ── Side-channel helpers the orchestrator's best-effort hooks call ──

    def chat(self, system: str, user_message: str, *args, **kwargs) -> str:
        """Fact extraction / auto-title path. Returns an empty JSON array so
        no session facts get stored — that keeps the assembled system prompt
        identical from turn to turn, which is what makes the prompt-cache
        numbers in chat_short reflect the system-prompt cache and nothing
        else. ("[]" is also too short to pass the auto-title length check,
        so conversation titles stay deterministic.)
        """
        self._sleep()
        return "[]"


class FakeTaskRouter:
    """Deterministic stand-in for ``services.router.TaskRouter``.

    Always returns the same RouteDecision so scenario routing is controlled
    by the fixture, not by keyword heuristics over fixture text.
    """

    def __init__(self, model: str = "claude", complexity: str = "simple"):
        self._model = model
        self._complexity = complexity

    def classify(self, user_message, messages, mem):
        from models import RouteDecision
        return RouteDecision(
            model=self._model,
            complexity=self._complexity,
            reasoning="perf-fixture route",
            confidence=1.0,
            needs_context=False,
        )
