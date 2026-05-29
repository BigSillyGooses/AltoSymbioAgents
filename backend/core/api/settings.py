"""
core/api/settings.py — Settings, first-run wizard, and pricing bridge methods.
"""

from __future__ import annotations

from typing import Any

from services import input_sanitizer

from ._base import BaseAPI


# Used only by verify_api_key to send the cheapest possible probe message.
# Not user-visible; not a default for regular chat (that comes from
# SETTINGS_DEFAULTS["claude_model"]).
_API_KEY_VERIFY_MODEL = "claude-haiku-4-5-20251001"


def _mask_secret(value: str) -> str:
    """Render a secret as a fixed-width masked string for the UI.

    Keeps the prefix and last four characters intact for long keys so the user
    can confirm at a glance that the right key is loaded; short keys collapse
    to a string of bullets. Empty input returns an empty string.
    """
    if not value:
        return ""
    if len(value) > 8:
        return value[:7] + "•" * (len(value) - 11) + value[-4:]
    return "•" * len(value)


class SettingsAPI(BaseAPI):

    def get_settings(self) -> dict:
        raw_key = self._settings.get("claude_api_key", "")
        masked_key = _mask_secret(raw_key)

        return {
            "lm_studio_url":         self._settings.get("lm_studio_url"),
            "ollama_url":            self._settings.get("ollama_url"),
            "claude_api_key":        masked_key,
            "claude_api_key_set":    bool(raw_key),
            "claude_model":          self._settings.get("claude_model"),
            "claude_prompt_caching": self._settings.get("claude_prompt_caching"),
            "default_local_backend": self._settings.get("default_local_backend"),
            "default_local_model":   self._settings.get("default_local_model"),
            "pinned_local_model":    self._settings.get("pinned_local_model"),
            "system_prompt":         self._settings.get("system_prompt"),
            "start_tab":             self._settings.get("start_tab"),
            "routing_enabled":               self._settings.get("routing_enabled"),
            "smart_routing_enabled":         self._settings.get("routing_enabled"),
            "interleaved_reasoning_enabled": self._settings.get("interleaved_reasoning_enabled"),
            "firewall_enabled":              self._settings.get("firewall_enabled"),
            "is_first_run":                  not self._settings.get("first_run_complete"),
            "first_run_complete":            self._settings.get("first_run_complete"),
            "max_conversation_budget_usd":   self._settings.get("max_conversation_budget_usd"),
            "budget_warning_threshold_pct":  self._settings.get("budget_warning_threshold_pct"),
            # Free-shippable v1: tri-state update mechanism ("off" | "auto" | "manual").
            "update_mechanism":           str(self._settings.get("update_mechanism") or "auto"),
            # PR 17: voice input/output toggles + selected models.
            "voice_input_enabled":        bool(self._settings.get("voice_input_enabled")),
            "voice_output_enabled":       bool(self._settings.get("voice_output_enabled")),
            "stt_model_id":               self._settings.get("stt_model_id") or "",
            "tts_voice_id":               self._settings.get("tts_voice_id") or "",
            # Stage-2 #9: which Thinking-timeline render the chat bubble uses.
            "timeline_variant":           str(self._settings.get("timeline_variant") or "compact"),
        }

    def save_setting(self, key: str, value: Any) -> None:
        if key == "smart_routing_enabled":
            key = "routing_enabled"
        self._settings.set(key, value)
        if key == "routing_enabled" and self._router is not None:
            self._router.set_enabled(bool(value))
        if key == "firewall_enabled":
            try:
                input_sanitizer.set_firewall_enabled(bool(value))
            except Exception:
                pass
        if key == "default_local_model" and self._local is not None and value:
            try:
                self._local.signal_model_loaded(str(value))
            except Exception:
                pass

    def set_setting(self, key: str, value: Any) -> dict:
        self._settings.set(key, value)
        _claude_keys = {"claude_api_key", "claude_model", "claude_prompt_caching"}
        if key in _claude_keys:
            self._claude.update_config(
                api_key=self._settings.get("claude_api_key", "") if key == "claude_api_key" else None,
                model=self._settings.get("claude_model") if key == "claude_model" else None,
                use_caching=self._settings.get("claude_prompt_caching") if key == "claude_prompt_caching" else None,
            )
        if key in ("routing_enabled", "smart_routing_enabled"):
            self._settings.set("routing_enabled", bool(value))
            if self._router is not None:
                self._router.set_enabled(bool(value))
        if key == "firewall_enabled":
            try:
                input_sanitizer.set_firewall_enabled(bool(value))
            except Exception:
                pass
        if key == "default_local_model" and self._local is not None and value:
            try:
                self._local.signal_model_loaded(str(value))
            except Exception:
                pass
        return {"ok": True}

    def get_setting(self, key: str) -> dict:
        return {"value": self._settings.get(key, None)}

    def complete_first_run(self, start_tab: str) -> None:
        self._settings.set("first_run_complete", True)
        self._settings.set("start_tab", start_tab)

    def verify_api_key(self, key: str) -> dict:
        """Synchronously verify an Anthropic API key. Used by the setup wizard."""
        key = (key or "").strip()
        if not key:
            return {"ok": False, "message": "Please enter your API key."}
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=key)
            client.messages.create(
                model=_API_KEY_VERIFY_MODEL,
                max_tokens=5,
                messages=[{"role": "user", "content": "hi"}],
            )
            self._settings.set("claude_api_key", key)
            self._claude.update_config(api_key=key)
            return {"ok": True, "message": "Connected to Claude ✓"}
        except Exception as exc:
            name = type(exc).__name__
            msg = str(exc).lower()
            if "authentication" in name.lower() or "auth" in msg or "invalid" in msg:
                return {"ok": False, "message": "Invalid API key — double-check it at console.anthropic.com"}
            if any(w in name.lower() for w in ("connection", "timeout", "network")):
                return {"ok": False, "message": "Can't reach Anthropic — check your internet connection"}
            return {"ok": False, "message": f"Unexpected error: {exc}"}

    def detect_local_setup(self) -> dict:
        """
        Probe for local model backends and suggest a model based on RAM.
        Called synchronously by the setup wizard.
        """
        try:
            import psutil
            ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        except Exception:
            ram_gb = 0.0

        if ram_gb >= 32:
            recommended, rec_reason = "llama3:8b", f"{ram_gb} GB RAM — 8B model runs comfortably"
        elif ram_gb >= 16:
            recommended, rec_reason = "llama3:8b", f"{ram_gb} GB RAM — 8B model should work well"
        elif ram_gb >= 8:
            recommended, rec_reason = "phi3:mini", f"{ram_gb} GB RAM — smaller model recommended"
        else:
            recommended, rec_reason = "phi3:mini", f"{ram_gb} GB RAM — lightweight model recommended"

        if self._local is not None:
            ollama_running = self._local.is_available(backend="ollama")
            lmstudio_running = self._local.is_available(backend="lmstudio")
            ollama_models = self._local.list_models(backend="ollama") if ollama_running else []
            lmstudio_models = self._local.list_models(backend="lmstudio") if lmstudio_running else []
            # Phase 3: probe both backends for the recommended Qwen3-30B-A3B
            # GGUF. LM Studio is the canonical target per the spec; Ollama is
            # checked as a courtesy. The first detected hit wins.
            qwen_status = self._local.detect_qwen3_30b_a3b(backend="lmstudio")
            if not qwen_status.get("detected"):
                qwen_status = self._local.detect_qwen3_30b_a3b(backend="ollama")
        else:
            ollama_running = lmstudio_running = False
            ollama_models = lmstudio_models = []
            qwen_status = {
                "detected":        False,
                "model_id":        "",
                "fallback_reason": "Local model client not initialized.",
            }

        return {
            "ram_gb": ram_gb,
            "recommended_model": recommended,
            "recommendation_reason": rec_reason,
            "ollama_running": ollama_running,
            "ollama_models": ollama_models,
            "lmstudio_running": lmstudio_running,
            "lmstudio_models": lmstudio_models,
            "qwen_status": qwen_status,
        }

    def get_model_prices(self) -> dict:
        """Return current model pricing (defaults + any user overrides).

        Defaults come from the catalog's ``family_fallback_prices`` so
        ``core/config/models.json`` remains the single source of truth.
        Per-family user overrides via ``settings.model_prices`` still
        flow through unchanged.
        """
        from core.model_catalog import get_catalog
        catalog = get_catalog()
        defaults: dict[str, dict[str, float]] = {}
        for family, (price_in, price_out) in catalog.family_fallback_prices.items():
            defaults[family] = {"input": price_in, "output": price_out}
        custom = self._settings.get("model_prices", None)
        if custom and isinstance(custom, dict):
            for k, v in custom.items():
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    defaults[k] = {"input": float(v[0]), "output": float(v[1])}
        return defaults

    def set_model_prices(self, prices: dict) -> dict:
        """
        Update model pricing. Format: {"haiku": [0.80, 4.0], "sonnet": [3.0, 15.0]}
        Values are per-million-token [input_price, output_price].
        """
        if not isinstance(prices, dict):
            return {"error": "prices must be a dict"}
        clean = {}
        for key, val in prices.items():
            if isinstance(val, (list, tuple)) and len(val) == 2:
                clean[key] = [float(val[0]), float(val[1])]
            elif isinstance(val, dict) and "input" in val and "output" in val:
                clean[key] = [float(val["input"]), float(val["output"])]
        self._settings.set("model_prices", clean)
        return {"ok": True, "prices": clean}

    def manifest(self) -> dict:
        """Return the settings manifest used by the generated Settings UI.

        Combines static field metadata (label / type / group / options /
        validation hints) with each field's current value and "is this the
        default?" flag. Secret values are never returned in clear — only a
        ``is_set`` boolean and a masked preview.

        Fields absent from FIELD_METADATA still exist in SETTINGS_DEFAULTS and
        can be read/written via the standard /api/settings endpoints; they're
        just not part of the manifest-driven UI yet.
        """
        from core.settings import (
            FIELD_METADATA,
            GROUPS_META,
            SETTINGS_DEFAULTS,
            is_secret_key,
        )

        fields: dict[str, dict] = {}
        for key, meta in FIELD_METADATA.items():
            if key not in SETTINGS_DEFAULTS:
                # Metadata for a key that was removed from the schema. Skip
                # silently rather than crashing the whole manifest fetch.
                continue
            expected_type, default_value = SETTINGS_DEFAULTS[key]
            raw_current = self._settings.get(key, default_value)

            type_name = (
                "|".join(t.__name__ for t in expected_type if t is not type(None))
                if isinstance(expected_type, tuple)
                else expected_type.__name__
            )

            entry: dict[str, Any] = {
                **meta,
                "key":          key,
                "value_type":   type_name,
                "default":      default_value,
                "is_default":   raw_current == default_value,
            }

            if is_secret_key(key):
                # Never return the secret value itself. Mask preview + boolean.
                entry["is_set"] = bool(raw_current)
                entry["preview"] = _mask_secret(raw_current or "")
            else:
                entry["value"] = raw_current

            # Phase 4: dynamic enum options for fields whose option list comes
            # from a live data source (currently just the agents catalog).
            # Done here rather than at module load so renaming an agent shows
            # up on the next manifest refresh without restarting the sidecar.
            if key == "default_agent_id":
                try:
                    from services.agent_registry import list_agents
                    agents = list_agents() or []
                    entry["options"] = [
                        {"value": "", "label": "— Smart routing (no default agent) —"},
                    ] + [
                        {"value": a["id"], "label": a.get("name") or a["id"]}
                        for a in agents
                    ]
                except Exception:
                    # Fall back to the static placeholder option from
                    # FIELD_METADATA so the field still renders.
                    pass

            fields[key] = entry

        return {
            "version": 1,
            "groups":  list(GROUPS_META),
            "fields":  fields,
        }

    def studio_mode_get(self) -> dict:
        """Return current studio mode state."""
        return {"enabled": bool(self._settings.get("studio_mode", False))}

    def studio_mode_set(self, enabled: bool) -> dict:
        """Enable or disable Studio Mode (shows advanced nav items)."""
        self._settings.set("studio_mode", enabled)
        return {"ok": True, "enabled": enabled}
