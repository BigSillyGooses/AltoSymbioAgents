"""
core/settings.py — Persistent settings backed by a JSON file on disk.

Stage 2 additions:
  - SETTINGS_DEFAULTS: typed schema with default values
  - _migrate(): fills missing keys with defaults on startup
  - set(): validates type and rejects unknown keys with a warning log
  - get_all_with_defaults(): helper for frontend introspection
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("altosybioagents.settings")

# ── Secret routing via OS keyring ────────────────────────────────────────────
# Keys in this set are stored in the platform keyring (DPAPI on Windows,
# Keychain on macOS, SecretService on Linux) instead of settings.json.
# On first load, a plaintext value found in settings.json is migrated to the
# keyring and cleared from the JSON file so the secret only lives on-disk in
# the OS-native store.
SECRET_KEYS: set[str] = {"claude_api_key"}
KEYRING_SERVICE = "altosybioagents"

# Process-level flag, assumed True until a keyring call actually fails. The
# /service_status route exposes this via keyring_available() so the renderer
# can render a "API key stored in plaintext" warning when the OS keyring is
# unreachable and we've fallen back to settings.json.
_keyring_available: bool = True


def keyring_available() -> bool:
    """Return False if any keyring call has failed in this process."""
    return _keyring_available


def _keyring_get(key: str) -> str | None:
    # Broad BaseException catch is intentional: some backends (e.g. pyo3-based
    # SecretService on a host missing its native deps) raise PanicException,
    # which derives from BaseException. A broken keyring must never bring the
    # app down — fall back to plaintext silently.
    global _keyring_available
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, key)
    except BaseException as exc:
        # Warn so the user can diagnose why their API key appears missing.
        # A broken keyring makes stored secrets inaccessible, which looks
        # like an empty API key rather than a configuration problem.
        _keyring_available = False
        log.warning(
            "keyring.get_password(%s) failed: %s — stored secrets may be "
            "inaccessible. Check your OS keyring/SecretService setup.", key, exc
        )
        return None


def _keyring_set(key: str, value: str) -> bool:
    global _keyring_available
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, key, value)
        return True
    except BaseException as exc:
        _keyring_available = False
        log.warning("keyring.set_password(%s) failed: %s — falling back to plaintext", key, exc)
        return False


def _keyring_delete(key: str) -> None:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, key)
    except BaseException as exc:
        log.debug("keyring.delete_password(%s) failed: %s", key, exc)

# ── Schema ────────────────────────────────────────────────────────────────────
# Each entry: key -> (python_type_or_types, default_value)
# Use a tuple of types to allow multiple acceptable types (e.g. str and NoneType).
SETTINGS_DEFAULTS: dict[str, tuple] = {
    # API / model
    "claude_api_key":              (str,   ""),
    "claude_model":                (str,   "claude-sonnet-4-6"),
    "default_local_model":         (str,   ""),
    # Chat-header pin for "always use this exact local model" mode. When
    # non-empty, the router forces every turn to local with this model id —
    # the symmetric counterpart of routing_enabled=false + claude_model=X
    # for the Claude side. Mutually exclusive with routing_enabled=false
    # (the ModelSwitcher enforces this). Cleared by Smart Routing or any
    # Claude pick.
    "pinned_local_model":          (str,   ""),
    "system_prompt":               (str,   "You are a helpful AI assistant."),

    # Local model backends
    "ollama_url":                  (str,   "http://localhost:11434"),
    "lm_studio_url":               (str,   "http://localhost:1234"),
    "default_local_backend":       (str,   "ollama"),
    # How long the model-listing probes wait for each local backend before
    # giving up. Two seconds is plenty for a localhost connect — a real
    # connection-refused fails in <50ms; anything longer means the daemon
    # is hung and the user doesn't want to keep waiting. Surfaced in the
    # settings manifest so users can lengthen it on slow/remote setups.
    "local_probe_timeout_sec":     (float, 2.0),
    # How long to wait for a chat response from any local model backend.
    # Promoted from the hardcoded timeout=120 in local_client.py.
    "local_inference_timeout_sec": (int,   120),

    # Phase 9: Bundled llama.cpp server.
    # local_backend_mode picks which local stack the LocalClient routes to:
    #   "auto"      — preserve historical detection (Ollama → LM Studio fallback)
    #   "ollama"    — force the Ollama URL
    #   "lm_studio" — force the LM Studio URL
    #   "bundled"   — use the bundled llama-server spawned from BundledServer
    # Validation of the string values lives in the LocalClient routing layer;
    # the schema is intentionally a plain str so legacy installs don't lose
    # the field on a forward-incompatible enum check.
    "local_backend_mode":          (str,   "auto"),
    "bundled_model_id":            (str,   ""),
    # HTTP timeout for downloading bundled GGUF model files from HuggingFace.
    # Promoted from _DOWNLOAD_TIMEOUT_SEC in bundled_server.py.
    "bundled_download_timeout_sec": (int,  60),
    # Set by BundledServer._drain_pipe on the first "offloaded N/M layers to
    # GPU" line emitted by llama-server. True when N == 0 (the Vulkan build
    # could not find a usable GPU and fell back to CPU); False otherwise.
    # The renderer reads this via the settings GET route to surface a
    # "running on CPU" notice.
    "bundled_gpu_offload_failed":  (bool,  False),

    # Routing
    "routing_enabled":             (bool,  True),
    "local_model_min_params":      (str,   "7B"),

    # Caching
    "claude_prompt_caching":       (bool,  True),

    # UI — start tab
    "start_tab":                   (str,   "chat"),

    # RAG / indexing
    "rag_folder":                  (str,   ""),
    "rag_chunk_size":              (int,   800),
    "rag_chunk_overlap":           (int,   200),

    # Memory
    "memory_similarity_threshold": (float, 0.5),
    "memory_history_cap":          (int,   40),
    "memory_write_gate_enabled":   (bool,  True),

    # Phase 5: Local-model behavior-drift canary (arXiv 2511.15992).
    # When enabled, the canary captures a 30-prompt baseline on the first
    # observed load of each model_id and re-checks on subsequent loads.
    # mean_drift > 0.40 emits a `model_canary_alert` SSE event.
    "model_canary_enabled":        (bool,  True),

    # Health / diagnostics
    "health_check_enabled":        (bool,  True),
    "diagnostics_retention_days":  (int,   7),

    # UI
    "theme":                       (str,   "system"),
    "show_token_counts":           (bool,  True),
    "show_cost_estimates":         (bool,  True),

    # First-run
    "first_run_complete":          (bool,  False),
    "onboarding_step":             (int,   0),
    "last_seen_version":           (str,   ""),

    # Phase 10 → free-shippable v1: tri-state update mechanism.
    # Valid values: "off" | "auto" | "manual".
    #   "off"    — never check for updates.
    #   "auto"   — electron-updater downloads and installs new releases in
    #              the background. The user clicks "Restart now" in the
    #              UpdateBanner to apply. Only works if the OS trusts the
    #              binary (signed, or already opted into via SmartScreen).
    #   "manual" — main polls the GitHub releases-latest endpoint every 6h
    #              and shows a banner that links to the download page. The
    #              user clicks through, downloads the .exe, and installs it
    #              themselves. Required when auto-install fails because the
    #              installer is unsigned.
    "update_mechanism":            (str,   "auto"),

    # Token budget (Stage 5)
    "max_conversation_budget_usd":  (float, 5.0),    # stop sending if cumulative cost exceeds this
    "budget_warning_threshold_pct": (float, 80.0),    # warn frontend at this % of budget

    # Feature flags (v4.0+)
    "goal_decomposition_enabled":    (bool,  True),
    "interleaved_reasoning_enabled": (bool,  True),
    "knowledge_graph_enabled":       (bool,  True),
    "studio_mode":                   (bool,  False),
    "firewall_enabled":              (bool,  True),
    # Adversarial debate (Du 2024) defaults off — it adds an LLM call per
    # specialist step. Power users opt in. When enabled, the high-stakes
    # gate below keeps it from firing on cheap "what's 2+2" turns.
    "debate_enabled":                (bool,  False),
    "debate_only_high_stakes":       (bool,  True),
    "guardrails_enabled":            (bool,  False),

    # DiLoCo-inspired sliding-window risk ledger. Default off — the legacy
    # per-turn-reset behavior stays in place. When enabled, the per-conversation
    # risk ledger persists across turns and prunes entries older than the
    # window, catching sustained risky behavior without locking conversations
    # out after ~9 messages.
    "sliding_window_risk_enabled":   (bool,  False),
    "sliding_window_risk_minutes":   (float, 10.0),

    # Phase 5: Wiser-Human-style escalation channel. When the orchestrator
    # detects a Lynch et al. trigger (replacement_threat, autonomy_reduction,
    # goal_conflict) it pauses worker invocation and surfaces the action to
    # the human for approval. Default on for new installs; user-toggleable.
    "escalation_channel_enabled":    (bool,  True),

    # Phase 6: Hackett et al. (ACL 2025) Reader/Actor split. When True the
    # orchestrator runs a 3-phase pipeline (read → act → synthesize) where
    # the Actor never sees raw retrieved data and may only call tools the
    # Reader proposed. Default False so existing behavior is preserved.
    "reader_actor_split_enabled":    (bool,  False),

    # Stage-2 #9: drillable Devin-style timeline variant. Picks which render
    # of the per-turn ThinkingRow[] timeline the chat bubble uses:
    #   "compact"   — the original ThinkingTimeline (one row per event, no
    #                 affordances, detail line-clamped to 2 lines).
    #   "drillable" — DevinTimeline: same rows but each row is a button
    #                 that toggles a panel with the un-truncated narrative.
    # Default "compact" so an upgrade doesn't surprise existing users; the
    # drillable variant is opt-in via the Appearance settings group until
    # we collect real-world feedback.
    "timeline_variant":              (str,   "compact"),

    # Stage-2 #7: LangGraph 1.2 StateGraph engine. Selects the control-flow
    # implementation for ChatOrchestrator.send():
    #   "legacy" — the imperative 800+-line body that has shipped since v5.
    #   "graph"  — services/orchestrator_graph.py: same downstream services
    #              (TurnLifecycle, MemoryRecall, TurnRouter, SecurityGate,
    #              WorkerDispatch, EscalationLadder, hub_router, governance,
    #              CaMeL, Reader/Actor split, voting), but composed as
    #              LangGraph nodes + edges instead of straight-line code.
    # Default "legacy" so existing behaviour is preserved until two clean
    # weekly bench cycles (AgentDojo + agentic-misalignment) confirm parity.
    "orchestrator_engine":           (str,   "legacy"),

    # QLPT Stage 1: logprob-derived margin-proxy quality scorer.
    # When True, EscalationLadder uses services.margin_proxy in place of
    # the self-score LLM call — but only when the worker actually
    # produced per-token logprobs. Falls back to the self-score path
    # otherwise (Claude rescue, Ollama < 0.12.11, qwen_thinking path).
    # Default off so existing users see no behavior change.
    #   - escalation_margin_proxy_params: optional dict overriding
    #     services.margin_proxy.SCORING_PARAMS keys (clamp_low,
    #     threshold_uncertain, penalty_weight). None means use the
    #     module defaults.
    #   - escalation_log_margin_proxy_scores: when True, log the score
    #     and the raw per-token logprob array each time the margin path
    #     runs, so the data can be re-aggregated offline (geometric
    #     mean, min-token, etc.) without re-running inference.
    "escalation_use_margin_proxy":         (bool,                False),
    "escalation_margin_proxy_params":      ((dict, type(None)),  None),
    "escalation_log_margin_proxy_scores":  (bool,                False),

    # Phase 12: CaMeL (Defeating Prompt Injections by Design — DeepMind/ETH,
    # arXiv 2503.18813). Privileged-LLM / Quarantined-LLM split with
    # capability-tagged plan execution. Only fires when the turn has
    # retrieved RAG chunks. Mutually exclusive with the Reader/Actor split:
    # when both flags are on, CaMeL wins (it is a stricter superset).
    # Default False so existing behavior is preserved.
    "camel_enabled":                 (bool,  False),

    # Phase 8: Symphony-style weighted-vote consensus on high-stakes turns.
    # Three parallel CoT samples, majority weighted by self-reported
    # confidence + lexical similarity. Only fires when the message is
    # high-stakes (escalation trigger, governance.HIGH_STAKES_KEYWORDS, or
    # cumulative risk_score > 0.7) AND the resolved target is Claude.
    "high_stakes_voting_enabled":    (bool,  True),

    # Phase 11: image input. Claude has built-in vision; local routes need
    # a vision-capable model. ``vision_local_models`` is a prefix-match
    # family list — any local model id that starts with one of these
    # strings (case-insensitive) is treated as vision-capable.
    "vision_enabled":                (bool,  True),
    "vision_local_models":           (list,  [
        "qwen2.5-vl", "llava", "minicpm-v", "moondream",
    ]),

    # Phase 13: voice input (Whisper.cpp) + voice output (Piper). Both
    # default off so a fresh install doesn't show the mic / speaker UI
    # until the user opts in. The model ids reference the build-pipeline
    # catalog at branding/sidecar-bundle/voice_assets.json; the actual
    # binary files (Whisper .bin, Piper .onnx + .json) live under
    # userData/voice/ and download on first feature use.
    "voice_input_enabled":           (bool,  False),
    "voice_output_enabled":          (bool,  False),
    "stt_model_id":                  (str,   "whisper-base.en"),
    "tts_voice_id":                  (str,   "en_US-amy-medium"),
    # Subprocess timeouts for voice features. Promoted from module-level
    # constants in voice.py so users on slow CPUs can increase them.
    "voice_transcribe_timeout_sec":  (float, 600.0),
    "voice_synthesize_timeout_sec":  (float,  60.0),

    # Agent / project
    "agent_project_root":            (str,   ""),

    # Phase 2: MCP servers (per-server enable list lives in this setting; the
    # registry itself reads server folders from paths.mcp_servers_dir()).
    "mcp_servers_disabled":          ((list, type(None)), []),

    # Phase 3: Qwen3 hybrid thinking. Per-agent budget overrides live on the
    # agents table; this is the global ceiling enforced for any agent.
    "qwen_thinking_global_budget_cap": (int, 8192),

    # Advanced (complex types)
    "model_prices":                  ((dict, type(None)),  None),
    "hooks":                         ((list, type(None)),  None),
    "channel_allowlist":             ((dict, type(None)),  None),

    # Misc
    "default_agent_id":            ((str, type(None)),  None),
    "app_version":                 (str,   "5.0.2"),
}


def _coerce(key: str, value: Any, expected_type) -> Any:
    """
    Attempt to coerce a value to the expected type.
    Returns the coerced value, or the original value if coercion fails
    (type mismatch is logged as a warning instead of crashing).
    """
    # Handle tuple of types (Union-like)
    if isinstance(expected_type, tuple):
        if isinstance(value, expected_type):
            return value
        for t in expected_type:
            if t is type(None):
                continue
            try:
                return t(value)
            except (ValueError, TypeError):
                pass
        log.warning(
            "settings: key '%s' has value %r which could not be coerced to %s; "
            "keeping as-is.", key, value, expected_type
        )
        return value

    if isinstance(value, expected_type):
        return value

    # Special-case bool: "true"/"false" strings are common in JSON config files
    if expected_type is bool:
        if isinstance(value, str):
            if value.lower() in ("true", "1", "yes"):
                return True
            if value.lower() in ("false", "0", "no"):
                return False
        if isinstance(value, int):
            return bool(value)

    try:
        return expected_type(value)
    except (ValueError, TypeError):
        log.warning(
            "settings: key '%s' has value %r; expected %s, keeping as-is.",
            key, value, expected_type.__name__
        )
        return value


class Settings:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()
        self._migrate()
        self._migrate_secrets_to_keyring()

    # ── Private ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                log.warning("settings: could not load %s, starting with defaults.", self._path)
                self._data = {}
        else:
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def _migrate(self) -> None:
        """
        Fill any missing keys with their default values and coerce existing
        values to the expected type. Saves the file if any changes were made.
        Runs once at startup so all callers can rely on the full key set.
        """
        changed = False
        for key, (expected_type, default_value) in SETTINGS_DEFAULTS.items():
            if key not in self._data:
                self._data[key] = default_value
                changed = True
                log.debug("settings: migrated missing key '%s' = %r", key, default_value)
            else:
                coerced = _coerce(key, self._data[key], expected_type)
                if coerced != self._data[key]:
                    self._data[key] = coerced
                    changed = True

        if changed:
            try:
                self._save()
            except OSError as exc:
                log.warning("settings: could not save migrated settings: %s", exc)

    def _migrate_secrets_to_keyring(self) -> None:
        """
        One-time migration: move plaintext secrets from settings.json into the
        OS keyring and clear the JSON copy. Runs on every load; no-op once the
        JSON value is blank.
        """
        changed = False
        for key in SECRET_KEYS:
            plain = self._data.get(key)
            if not plain:
                continue
            if _keyring_set(key, plain):
                self._data[key] = ""
                changed = True
                log.info("settings: migrated secret '%s' into OS keyring", key)
        if changed:
            try:
                self._save()
            except OSError as exc:
                log.warning("settings: could not save after secret migration: %s", exc)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        if key in SECRET_KEYS:
            stored = _keyring_get(key)
            if stored:
                return stored
            # Fall through to plaintext lookup — either keyring is unavailable
            # (headless Linux without dbus, tests) or the secret was never set.
        with self._lock:
            if key in self._data:
                return self._data[key]
            if key in SETTINGS_DEFAULTS:
                return SETTINGS_DEFAULTS[key][1]
            return default

    def set(self, key: str, value: Any) -> None:
        """
        Set a setting value. Unknown keys are rejected with a warning.
        Known keys are type-coerced before saving.
        """
        if key not in SETTINGS_DEFAULTS:
            log.warning(
                "settings: attempted to set unknown key '%s'; ignoring. "
                "Add it to SETTINGS_DEFAULTS if this is intentional.", key
            )
            return

        expected_type, _ = SETTINGS_DEFAULTS[key]
        value = _coerce(key, value, expected_type)

        if key in SECRET_KEYS:
            str_value = "" if value is None else str(value)
            if str_value:
                if _keyring_set(key, str_value):
                    with self._lock:
                        # Don't persist secrets to disk when keyring succeeds.
                        self._data[key] = ""
                        self._save()
                    return
                # keyring unavailable — fall through to plaintext write so the
                # app still works (with the same security properties as before
                # this change).
            else:
                _keyring_delete(key)

        with self._lock:
            self._data[key] = value
            self._save()

    def set_raw(self, key: str, value: Any) -> None:
        """
        Bypass schema validation — use only for keys that are dynamically
        generated at runtime (e.g. version strings, per-install IDs).
        Prefer set() in all other cases.
        """
        with self._lock:
            self._data[key] = value
            self._save()

    def all(self) -> dict:
        """Return a snapshot of all settings merged with defaults."""
        with self._lock:
            result = {k: v for k, (_, v) in SETTINGS_DEFAULTS.items()}
            result.update(self._data)
            return result

    def get_schema(self) -> dict:
        """
        Return the schema as {key: {"type": str, "default": value}}.
        Useful for frontend introspection.
        """
        out = {}
        for key, (expected_type, default_value) in SETTINGS_DEFAULTS.items():
            if isinstance(expected_type, tuple):
                type_name = "|".join(
                    t.__name__ for t in expected_type if t is not type(None)
                )
            else:
                type_name = expected_type.__name__
            out[key] = {"type": type_name, "default": default_value}
        return out


# ── Field metadata for the settings manifest ─────────────────────────────────
# Populate one entry per key that the UI needs to render as a real form field.
# Keys absent from this table still exist in SETTINGS_DEFAULTS and can be
# read/written via the existing /api/settings endpoints — they just don't
# appear in the manifest-driven UI yet. Adding a row here is the only step
# needed to surface a new setting in the generated Settings UI.
#
# Recognised field-level keys:
#   label          — short human label (sentence case)
#   description    — one-line explanation, used as form help text
#   type           — "string" | "url" | "int" | "float" | "bool" | "enum" | "secret"
#   group          — id of a GROUPS_META entry
#   placeholder    — input placeholder (optional)
#   unit           — display unit, e.g. "seconds", "USD", "%" (optional)
#   min / max      — numeric bounds for int/float (optional)
#   options        — list[{value, label}] for enum (required when type=="enum")
#   verify_endpoint— path of a POST route that can validate the value (optional)
#   read_only      — when True, UI shows but does not edit (optional)
FIELD_METADATA: dict[str, dict] = {
    # ── Claude API ──────────────────────────────────────────────────────────
    "claude_api_key": {
        "label":           "Anthropic API key",
        "description":     "Get a key at console.anthropic.com. Stored in your OS keyring.",
        "type":            "secret",
        "group":           "api",
        "placeholder":     "sk-ant-…",
        "verify_endpoint": "/api/settings/verify_api_key",
    },
    "claude_model": {
        "label":       "Default Claude model",
        "description": "Used when the router escalates from a local model to Claude.",
        "type":        "string",
        "group":       "api",
    },
    "claude_prompt_caching": {
        "label":       "Anthropic prompt caching",
        "description": "Cache long prompt prefixes to cut latency and cost on repeat turns.",
        "type":        "bool",
        "group":       "api",
    },

    # ── Local models ────────────────────────────────────────────────────────
    "local_backend_mode": {
        "label":       "Local backend mode",
        "description": "Which local model stack the router uses.",
        "type":        "enum",
        "group":       "local_models",
        "options": [
            {"value": "auto",      "label": "Auto (probe Ollama, then LM Studio)"},
            {"value": "ollama",    "label": "Ollama only"},
            {"value": "lm_studio", "label": "LM Studio only"},
            {"value": "bundled",   "label": "Bundled llama.cpp"},
        ],
    },
    "ollama_url": {
        "label":       "Ollama URL",
        "description": "HTTP endpoint for the Ollama daemon.",
        "type":        "url",
        "group":       "local_models",
        "placeholder": "http://localhost:11434",
    },
    "lm_studio_url": {
        "label":       "LM Studio URL",
        "description": "HTTP endpoint for the LM Studio server.",
        "type":        "url",
        "group":       "local_models",
        "placeholder": "http://localhost:1234",
    },
    "local_probe_timeout_sec": {
        "label":       "Local probe timeout",
        "description": "How long to wait for each local backend when listing models.",
        "type":        "float",
        "group":       "local_models",
        "unit":        "seconds",
        "min":         0.5,
        "max":         30.0,
    },
    "default_local_model": {
        "label":       "Active local model",
        "description": "Model id the router uses when Smart Routing picks the local backend.",
        "type":        "string",
        "group":       "local_models",
    },
    "pinned_local_model": {
        "label":       "Pinned local model",
        "description": "When set, every chat turn is forced to this local model and Smart Routing is bypassed. Manage via the model picker in the chat header.",
        "type":        "string",
        "group":       "local_models",
        "read_only":   True,
    },
    "bundled_model_id": {
        "label":       "Bundled model",
        "description": "GGUF model id managed by the bundled llama.cpp server.",
        "type":        "string",
        "group":       "local_models",
        "read_only":   True,
    },

    # ── Token budget & cost ─────────────────────────────────────────────────
    "max_conversation_budget_usd": {
        "label":       "Per-conversation budget",
        "description": "Stop sending new turns once cumulative cost exceeds this.",
        "type":        "float",
        "group":       "budget",
        "unit":        "USD",
        "min":         0.0,
    },
    "budget_warning_threshold_pct": {
        "label":       "Budget warning threshold",
        "description": "Warn when usage exceeds this fraction of the budget.",
        "type":        "float",
        "group":       "budget",
        "unit":        "%",
        "min":         0.0,
        "max":         100.0,
    },

    # ── Local models (additional) ────────────────────────────────────────────
    "local_inference_timeout_sec": {
        "label":       "Inference timeout",
        "description": "Maximum wait for a response from any local model backend.",
        "type":        "int",
        "group":       "local_models",
        "unit":        "seconds",
        "min":         10,
        "max":         600,
    },
    "bundled_download_timeout_sec": {
        "label":       "Download timeout",
        "description": "HTTP timeout per request when fetching bundled GGUF model files.",
        "type":        "int",
        "group":       "local_models",
        "unit":        "seconds",
        "min":         10,
        "max":         300,
    },

    # ── Chat ─────────────────────────────────────────────────────────────────
    "system_prompt": {
        "label":       "System prompt",
        "description": "Default instructions prepended to every conversation.",
        "type":        "textarea",
        "group":       "chat",
    },
    "default_agent_id": {
        "label":       "Default agent",
        "description": (
            "Agent pre-selected when you click New conversation. "
            "Leave blank to start with smart routing instead."
        ),
        "type":        "enum",
        "group":       "chat",
        # Options are populated dynamically at manifest-build time from the
        # agents table — keeping a single empty default here so the renderer
        # has something to render even when the agent list is unreachable.
        "options":     [{"value": "", "label": "— Smart routing (no default agent) —"}],
    },

    # ── Smart routing ─────────────────────────────────────────────────────────
    "routing_enabled": {
        "label":       "Enable smart routing",
        "description": "Uncertainty-aware classifier routes easy turns to local models.",
        "type":        "bool",
        "group":       "routing",
    },
    "local_model_min_params": {
        "label":       "Minimum model size",
        "description": "Smallest local model the router will consider for non-trivial turns.",
        "type":        "enum",
        "group":       "routing",
        "options": [
            {"value": "3B",  "label": "3B"},
            {"value": "7B",  "label": "7B"},
            {"value": "13B", "label": "13B"},
            {"value": "34B", "label": "34B"},
            {"value": "70B", "label": "70B"},
        ],
    },

    # ── Appearance ────────────────────────────────────────────────────────────
    "theme": {
        "label":       "Theme",
        "description": "Color scheme for the application.",
        "type":        "enum",
        "group":       "appearance",
        "options": [
            {"value": "system", "label": "System default"},
            {"value": "light",  "label": "Light"},
            {"value": "dark",   "label": "Dark"},
        ],
    },
    "show_token_counts": {
        "label":       "Show token counts",
        "description": "Display approximate token count below the composer.",
        "type":        "bool",
        "group":       "appearance",
    },
    "show_cost_estimates": {
        "label":       "Show cost estimates",
        "description": "Show estimated cost next to token counts when a Claude model is pinned.",
        "type":        "bool",
        "group":       "appearance",
    },
    "start_tab": {
        "label":       "Start tab",
        "description": "Which tab opens when the app launches.",
        "type":        "enum",
        "group":       "appearance",
        "options": [
            {"value": "chat",     "label": "Chat"},
            {"value": "memory",   "label": "Memory"},
            {"value": "agents",   "label": "Agents"},
            {"value": "mcp",      "label": "Tool servers"},
            {"value": "settings", "label": "Settings"},
        ],
    },
    "timeline_variant": {
        "label":       "Thinking timeline style",
        "description": "Compact one-liner rows (default), or drillable rows you can expand to read the full reasoning (experimental).",
        "type":        "enum",
        "group":       "appearance",
        "options": [
            {"value": "compact",   "label": "Compact"},
            {"value": "drillable", "label": "Drillable (experimental)"},
        ],
    },

    # ── Updates ───────────────────────────────────────────────────────────────
    "update_mechanism": {
        "label":       "Update mechanism",
        "description": "How the app checks for and installs new versions.",
        "type":        "enum",
        "group":       "updates",
        "options": [
            {"value": "auto",   "label": "Automatic — download and install in the background"},
            {"value": "manual", "label": "Manual — notify me and open the download page"},
            {"value": "off",    "label": "Off — never check for updates"},
        ],
    },

    # ── Knowledge base ────────────────────────────────────────────────────────
    "rag_folder": {
        "label":       "Knowledge folder",
        "description": "Directory scanned for documents to index for retrieval.",
        "type":        "string",
        "group":       "rag",
        "placeholder": "/path/to/docs",
    },
    "rag_chunk_size": {
        "label":       "Chunk size",
        "description": "Number of characters per document chunk when indexing.",
        "type":        "int",
        "group":       "rag",
        "unit":        "chars",
        "min":         100,
        "max":         4000,
    },
    "rag_chunk_overlap": {
        "label":       "Chunk overlap",
        "description": "Overlap between adjacent chunks to preserve context at boundaries.",
        "type":        "int",
        "group":       "rag",
        "unit":        "chars",
        "min":         0,
        "max":         1000,
    },

    # ── Memory ────────────────────────────────────────────────────────────────
    "memory_write_gate_enabled": {
        "label":       "Write gate",
        "description": "Require explicit confirmation before writing new memories.",
        "type":        "bool",
        "group":       "memory",
    },
    "memory_history_cap": {
        "label":       "History cap",
        "description": "Maximum number of past turns the memory retriever considers.",
        "type":        "int",
        "group":       "memory",
        "unit":        "turns",
        "min":         5,
        "max":         200,
    },
    "memory_similarity_threshold": {
        "label":       "Similarity threshold",
        "description": "Minimum cosine similarity for a memory to be retrieved.",
        "type":        "float",
        "group":       "memory",
        "min":         0.0,
        "max":         1.0,
    },

    # ── Advanced ──────────────────────────────────────────────────────────────
    "goal_decomposition_enabled": {
        "label":       "Goal decomposition",
        "description": "Break complex requests into sub-tasks before executing.",
        "type":        "bool",
        "group":       "advanced",
    },
    "interleaved_reasoning_enabled": {
        "label":       "Interleaved reasoning",
        "description": "Run reasoning steps between tool calls for deeper analysis.",
        "type":        "bool",
        "group":       "advanced",
    },
    "knowledge_graph_enabled": {
        "label":       "Knowledge graph",
        "description": "Build and query a structured graph of entities and relationships.",
        "type":        "bool",
        "group":       "advanced",
    },
    "vision_enabled": {
        "label":       "Vision (image input)",
        "description": "Allow image attachments in chat messages.",
        "type":        "bool",
        "group":       "advanced",
    },
    "high_stakes_voting_enabled": {
        "label":       "High-stakes voting",
        "description": "Run three parallel samples and take a majority vote on high-stakes turns.",
        "type":        "bool",
        "group":       "advanced",
    },
    "escalation_channel_enabled": {
        "label":       "Escalation channel",
        "description": "Pause and ask for human approval when a sensitive action is detected.",
        "type":        "bool",
        "group":       "advanced",
    },
    "firewall_enabled": {
        "label":       "Prompt firewall",
        "description": "Scan every turn for prompt-injection patterns before processing.",
        "type":        "bool",
        "group":       "advanced",
    },
    "guardrails_enabled": {
        "label":       "Guardrails",
        "description": "Apply output safety checks after each assistant turn.",
        "type":        "bool",
        "group":       "advanced",
    },
    "debate_enabled": {
        "label":       "Adversarial debate",
        "description": "Run a critic pass that challenges the first response on high-stakes turns.",
        "type":        "bool",
        "group":       "advanced",
    },
    "debate_only_high_stakes": {
        "label":       "Debate on high-stakes turns only",
        "description": "Limit the debate step to turns classified as high-stakes.",
        "type":        "bool",
        "group":       "advanced",
    },
    "sliding_window_risk_enabled": {
        "label":       "Sliding-window risk ledger",
        "description": "Track cumulative risk across turns instead of resetting each turn.",
        "type":        "bool",
        "group":       "advanced",
    },
    "sliding_window_risk_minutes": {
        "label":       "Risk window duration",
        "description": "Prune ledger entries older than this when the sliding window is enabled.",
        "type":        "float",
        "group":       "advanced",
        "unit":        "minutes",
        "min":         1.0,
        "max":         60.0,
    },
    "model_canary_enabled": {
        "label":       "Model canary",
        "description": "Detect behavior drift when a new local model loads for the first time.",
        "type":        "bool",
        "group":       "advanced",
    },
    "reader_actor_split_enabled": {
        "label":       "Reader/Actor split",
        "description": "Separate retrieval and action into isolated pipeline stages (experimental).",
        "type":        "bool",
        "group":       "advanced",
    },
    "orchestrator_engine": {
        "label":       "Orchestrator engine",
        "description": "Control-flow implementation for the chat turn: legacy imperative or LangGraph StateGraph (experimental, same downstream services).",
        "type":        "enum",
        "group":       "advanced",
        "options":     ["legacy", "graph"],
    },
    "camel_enabled": {
        "label":       "CaMeL isolation",
        "description": "Quarantine retrieved content from the privileged LLM (experimental).",
        "type":        "bool",
        "group":       "advanced",
    },
    "studio_mode": {
        "label":       "Studio mode",
        "description": "Enable advanced multi-agent orchestration and parallel workers.",
        "type":        "bool",
        "group":       "advanced",
    },
    "qwen_thinking_global_budget_cap": {
        "label":       "Qwen3 thinking budget cap",
        "description": "Maximum thinking token budget for any single Qwen3 agent call.",
        "type":        "int",
        "group":       "advanced",
        "unit":        "tokens",
        "min":         512,
        "max":         32768,
    },
    "voice_transcribe_timeout_sec": {
        "label":       "Transcription timeout",
        "description": "Maximum wall time for a Whisper transcription. Increase on very slow CPUs.",
        "type":        "float",
        "group":       "advanced",
        "unit":        "seconds",
        "min":         30.0,
        "max":         3600.0,
    },
    "voice_synthesize_timeout_sec": {
        "label":       "Synthesis timeout",
        "description": "Maximum wall time for a Piper TTS run.",
        "type":        "float",
        "group":       "advanced",
        "unit":        "seconds",
        "min":         10.0,
        "max":         600.0,
    },
}


# Ordered list of group definitions for the manifest UI.
GROUPS_META: list[dict] = [
    {"id": "api",          "label": "Claude API",
     "description": "Authentication and default model for cloud inference."},
    {"id": "local_models", "label": "Local models",
     "description": "Where to find Ollama, LM Studio, and the bundled llama.cpp server."},
    {"id": "budget",       "label": "Token budget & cost",
     "description": "Per-conversation spending controls."},
    {"id": "chat",         "label": "Chat",
     "description": "System prompt and general chat behavior."},
    {"id": "routing",      "label": "Smart routing",
     "description": "Classifier that picks Claude vs local models per turn."},
    {"id": "appearance",   "label": "Appearance",
     "description": "Theme, token display, and other display preferences."},
    {"id": "updates",      "label": "Updates",
     "description": "How the app checks for and applies new versions."},
    {"id": "rag",          "label": "Knowledge base",
     "description": "Retrieval-augmented generation settings."},
    {"id": "memory",       "label": "Memory",
     "description": "How the agent stores and retrieves conversation memory."},
    {"id": "advanced",     "label": "Advanced",
     "description": "Feature flags and experimental capabilities."},
]


def is_secret_key(key: str) -> bool:
    """Return True if a setting's value should never leave the backend in clear."""
    return key in SECRET_KEYS
