"""
core/api/usage.py — usage-domain bridge methods (Perf Phase 4).

One method today: ``usage_predict`` — the pre-send "this turn ≈ $0.04" hint
behind POST /api/usage/predict. It builds an APPROXIMATE prompt (the default
system prompt from settings + the conversation's recent history + the
pending message) and runs it through ``services.cost_predictor.predict``.

Approximate by design: the real per-turn prompt also carries memory recall,
RAG context, agent personas, security-gate additions, and attachment blocks
— none of which exist until the turn actually runs. This endpoint powers a
UI hint before send, not enforcement; the enforcing prediction (the
``cost_prediction_block_over_budget`` guard) is computed inside
ChatOrchestrator.send() against the REAL assembled prompt. The endpoint
works regardless of ``cost_prediction_enabled`` — calling it is an explicit
request for an estimate.
"""

from __future__ import annotations

import db as _db

from ._base import BaseAPI

# Mirrors chat_orchestrator.MAX_HISTORY_MESSAGES — the window a real turn
# would load before trimming. Kept as a literal to avoid importing the
# orchestrator module (and its service graph) for one int.
_HISTORY_WINDOW = 40


class UsageAPI(BaseAPI):

    def usage_predict(self, conversation_id: str | None, message: str) -> dict:
        """Approximate pre-send cost prediction for the UI."""
        system_prompt = self._settings.get(
            "system_prompt", "You are a helpful AI assistant.",
        )

        messages: list[dict] = []
        if conversation_id:
            try:
                rows = _db.fetchall(
                    "SELECT role, content FROM messages WHERE conversation_id = ? "
                    "AND role IN ('user', 'assistant') "
                    "ORDER BY created_at DESC LIMIT ?",
                    (conversation_id, _HISTORY_WINDOW),
                )
                messages = [
                    {"role": r["role"], "content": r["content"]}
                    for r in reversed(rows)
                ]
            except Exception as exc:  # noqa: BLE001 — estimate without history
                self._log.debug("usage_predict history read failed: %s", exc)
                messages = []
        messages.append({"role": "user", "content": message or ""})

        # Price against the configured Claude model — the worst (paid) case;
        # a turn the router sends to a local model costs $0 anyway.
        model_name = self._settings.get("claude_model", "") or ""

        from services import cost_predictor
        prediction = cost_predictor.predict(
            system_prompt, messages, model_name, self._settings,
            claude_client=self._claude,
            conversation_id=conversation_id,
        )
        return {
            "conversation_id": conversation_id,
            "model": model_name,
            "est_input_tokens": prediction.est_input_tokens,
            "est_cached_tokens": prediction.est_cached_tokens,
            "est_output_tokens": prediction.est_output_tokens,
            "est_cost_usd": prediction.est_cost_usd,
            "method": prediction.method,
        }
