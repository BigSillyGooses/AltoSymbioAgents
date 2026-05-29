"""
services/llm_litellm_adapter.py — Third LLMClient implementation, BYO-key path.

LiteLLM normalises 100+ provider APIs (OpenAI / Gemini / Groq / Mistral /
DeepSeek / Grok / Cohere / Ollama, etc.) behind a single OpenAI-compatible
call. This adapter exposes that surface through the existing ``LLMClient``
ABC so ``HubRouter.invoke()`` can dispatch BYO-keyed traffic to any
LiteLLM-supported provider without rewriting ``ClaudeClient`` or
``LocalClient``.

Model strings follow LiteLLM's ``provider/model`` convention:
  * ``openai/gpt-4o-mini``
  * ``gemini/gemini-2.5-flash``
  * ``groq/llama-3.3-70b-versatile``
  * ``mistral/mistral-small-latest``
  * ``anthropic/claude-3-5-sonnet-latest`` (note: still goes through the
    LiteLLM path — the dedicated ``ClaudeClient`` is used for first-class
    Anthropic features like cache_control + thinking_delta streaming.)

The system prompt is prepended as ``{"role": "system", ...}`` because
LiteLLM normalises to the OpenAI chat format — the LocalClient pattern
applies here too.

LiteLLM reads the per-call ``api_key`` kwarg first and falls back to
provider-specific environment variables (e.g. ``OPENAI_API_KEY``,
``GEMINI_API_KEY``). We pass the key explicitly so per-conversation key
overrides work without touching process env.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from services.llm_interface import LLMClient

log = logging.getLogger("altosybioagents.litellm")

# Sentinel returned by chat_unified / stream_unified on failure so callers
# get a well-formed dict instead of having to defend against None.
_FALLBACK_TEXT = "[LiteLLM call failed — see backend log for the underlying provider error.]"


class LiteLLMClient(LLMClient):
    """Routes chat through LiteLLM's provider-agnostic surface.

    Construct one instance per (model, api_key) pair. ``update_config`` is
    available so Settings can swap the model or rotate the key without
    re-creating the object (matches the ``ClaudeClient.update_config``
    contract).
    """

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        *,
        api_base: Optional[str] = None,
        extra_kwargs: Optional[dict] = None,
    ) -> None:
        self._model = (model or "").strip()
        self._api_key = (api_key or "").strip() or None
        self._api_base = (api_base or "").strip() or None
        # Provider-specific extras (e.g. ``deepseek_reasoning_effort``,
        # ``response_format``). Passed through unchanged to litellm.completion.
        self._extra_kwargs: dict = dict(extra_kwargs or {})

    # ── Configuration ───────────────────────────────────────────────────────

    def update_config(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        extra_kwargs: Optional[dict] = None,
    ) -> None:
        if model is not None:
            self._model = model.strip()
        if api_key is not None:
            self._api_key = api_key.strip() or None
        if api_base is not None:
            self._api_base = api_base.strip() or None
        if extra_kwargs is not None:
            self._extra_kwargs = dict(extra_kwargs)

    # ── LLMClient interface ─────────────────────────────────────────────────

    def chat_unified(
        self,
        system: str,
        messages: list,
        max_tokens: int = 4096,
    ) -> dict:
        """Non-streaming chat. Returns ``{text, input_tokens, output_tokens}``.

        Falls back to a sentinel string on provider error so the hub
        router's caller doesn't have to handle ``None`` — matching the
        defensive shape ``LocalClient.chat_unified`` returns.
        """
        # Imported lazily so the litellm import cost (significant: pulls in
        # openai + httpx + tokenizers) is only paid when the BYO-key path is
        # actually exercised. Sidecars that never see a LiteLLM-routed turn
        # never warm this module.
        import litellm

        kwargs = self._build_kwargs(system, messages, max_tokens, stream=False)
        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:
            log.warning("LiteLLMClient.chat_unified (%s) failed: %s", self._model, exc)
            return {"text": _FALLBACK_TEXT, "input_tokens": 0, "output_tokens": 0}

        return self._parse_completion(resp)

    def stream_unified(
        self,
        system: str,
        messages: list,
        on_token: Callable[[str], None],
        max_tokens: int = 4096,
    ) -> dict:
        """Streaming chat. Calls ``on_token`` for each text delta.

        On any stream error we fall back to the non-streaming path so the
        caller still receives a well-formed result. This mirrors the
        LocalClient fallback discipline.
        """
        import litellm

        kwargs = self._build_kwargs(system, messages, max_tokens, stream=True)
        full_text = ""
        input_tokens = 0
        output_tokens = 0
        try:
            stream = litellm.completion(**kwargs)
            for chunk in stream:
                delta = self._extract_delta_text(chunk)
                if delta:
                    full_text += delta
                    on_token(delta)
                # Usage may arrive on the final chunk (OpenAI streams it when
                # ``stream_options={"include_usage": true}`` is set; LiteLLM
                # surfaces it the same way). We read it when present.
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) or input_tokens
                    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) or output_tokens
            return {
                "text": full_text,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        except Exception as exc:
            log.warning(
                "LiteLLMClient.stream_unified (%s) failed mid-stream: %s. Falling back to non-streaming.",
                self._model, exc,
            )
            # Re-issue as non-streaming so the caller still gets a complete
            # answer (without per-token callbacks for the remainder of the
            # request). The partial ``full_text`` is discarded because the
            # second call yields the full response and mixing them would
            # produce duplicate prose.
            return self.chat_unified(system, messages, max_tokens=max_tokens)

    def is_available(self) -> bool:
        """An adapter is available when both a model and an API key are set.

        Some LiteLLM providers (Ollama, local LM Studio) don't need a key,
        but the BYO-key flow this adapter exists for always requires one.
        The HubRouter treats a False return as "do not dispatch here", so
        unset-key configurations fail closed rather than silently calling
        a provider with whatever's in the process env.
        """
        return bool(self._model and self._api_key)

    def client_name(self) -> str:
        return self._model or "litellm"

    # ── Internals ───────────────────────────────────────────────────────────

    def _build_kwargs(
        self,
        system: str,
        messages: list,
        max_tokens: int,
        *,
        stream: bool,
    ) -> dict:
        """Compose the kwargs dict handed to ``litellm.completion``.

        LiteLLM normalises to the OpenAI chat format, which means the system
        prompt rides in the messages list as ``{"role": "system", ...}``.
        This matches the LocalClient pattern and avoids leaking
        provider-specific shapes into HubRouter callers.
        """
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        kwargs: dict = {
            "model": self._model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if stream:
            # Ask the upstream to attach usage on the final chunk. Most
            # OpenAI-compatible providers honour this; the ones that don't
            # silently ignore it, in which case stream_unified returns zeros
            # for token counts (same as the streaming Anthropic fallback path
            # in ClaudeClient).
            kwargs.setdefault("stream_options", {"include_usage": True})
        if self._extra_kwargs:
            # Caller-supplied extras win — they're explicit choices.
            kwargs.update(self._extra_kwargs)
        return kwargs

    @staticmethod
    def _parse_completion(resp) -> dict:
        """Pull text + usage out of a LiteLLM ``ModelResponse``.

        LiteLLM normalises to the OpenAI shape so the access path is stable
        across providers, but we still defend against missing fields — some
        providers omit usage entirely and we'd rather return zeros than
        crash the orchestrator.
        """
        try:
            text = resp.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            text = ""
        usage = getattr(resp, "usage", None)
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        return {"text": text, "input_tokens": in_tok, "output_tokens": out_tok}

    @staticmethod
    def _extract_delta_text(chunk) -> str:
        """Return the ``content`` delta from a LiteLLM stream chunk, or ''."""
        try:
            delta = chunk.choices[0].delta
        except (AttributeError, IndexError, TypeError):
            return ""
        text = getattr(delta, "content", None)
        return text or ""
