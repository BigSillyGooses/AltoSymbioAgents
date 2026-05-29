"""
services/local_client.py

Unified client for Ollama and LM Studio.
Mirrors ClaudeClient's interface so the router can swap between them.

Fix applied:
  - All requests.post calls wrapped in explicit try/except with graceful
    fallback strings rather than letting KeyError / JSONDecodeError / network
    exceptions propagate unhandled to ChatOrchestrator.
"""

import json
import logging
import re
import requests
from core.settings import Settings
from services.llm_interface import LLMClient

# Phase 3: Qwen3-30B-A3B detection. Matches LM Studio's typical id forms
# such as "qwen3-30b-a3b", "Qwen/Qwen3-30B-A3B-Instruct", "qwen3-30b-a3b-q4_k_m".
_QWEN3_30B_A3B_ID = re.compile(r"qwen3.*30b.*a3b", re.IGNORECASE)

log = logging.getLogger("altosybioagents.local")

_FALLBACK = "[Local model unavailable — no response]"

# Common GGUF quantisation tags. We surface the tag in the model browser so
# users can tell q4 from q8 without parsing filenames themselves. Matched
# case-insensitively against the filename stem.
_QUANT_RE = re.compile(
    r"(q\d_(?:k|0|1)(?:_[a-z])?|q\d+|iq\d_(?:s|m|l|xs|xss|xxs|nl)|f16|fp16|bf16|f32|fp32)",
    re.IGNORECASE,
)


def _quant_from_filename(name: str) -> str | None:
    """Extract a quantisation tag from a GGUF filename, if present.

    Example: ``"Qwen2.5-7B-Instruct-Q4_K_M.gguf" -> "q4_k_m"``. Returns
    ``None`` when no recognised tag is found.
    """
    m = _QUANT_RE.search(name)
    return m.group(0).lower() if m else None


def _short_error(exc: BaseException) -> str:
    """Render an exception as a short, user-readable string for status badges.

    Strips the long stack-trace context; collapses ``ConnectionRefusedError``
    into a friendlier phrasing so the empty-state hint reads as guidance
    rather than a stack trace.
    """
    name = type(exc).__name__
    if any(s in name for s in ("ConnectionError", "ConnectionRefused", "NewConnectionError")):
        return "Not reachable"
    if "Timeout" in name:
        return "Timed out"
    msg = str(exc) or name
    # Cap to keep status pills compact.
    return msg if len(msg) <= 120 else msg[:117] + "…"

# QLPT Stage 1: Ollama added native logprobs to /api/chat in v0.12.11
# (Nov 2025). The /v1/chat/completions compatibility layer still drops
# the field — see ollama/ollama#16117. When the unified path needs
# logprobs for the Ollama backend we hit native /api/chat with
# ``"logprobs": true, "top_logprobs": 1``; LM Studio / bundled stay on
# the OpenAI-compatible path because LM Studio's compat layer surfaces
# logprobs correctly.
OLLAMA_MIN_LOGPROBS_VERSION = "0.12.11"


class LocalVisionUnavailable(RuntimeError):
    """Raised when the active local model can't see images.

    The orchestrator catches this and surfaces a friendly error to the
    user that names a vision-capable model they could switch to. The
    family list comes from the ``vision_local_models`` setting.
    """

    def __init__(self, active_model: str, families: list[str]):
        self.active_model = active_model
        self.families = list(families)
        super().__init__(
            f"Local model '{active_model or '(none)'}' can't see images. "
            f"Switch to one of: {', '.join(self.families) or '(no families configured)'}"
        )


class LocalClient(LLMClient):
    def __init__(self, settings: Settings):
        self._settings = settings
        # Phase 9: BundledServer is wired in by core/api/__init__.py at startup.
        # Without it set, bundled-mode requests still try the LM Studio shape
        # at the persisted port (read from paths.bundled_server_port_file)
        # so that a sidecar restart doesn't lose the connection.
        self._bundled_server: object | None = None
        # QLPT Stage 1: Ollama logprobs support is gated by server version.
        # Probe /api/version once per session and cache the result so every
        # unified-path call doesn't pay the round trip. None = not probed yet.
        self._ollama_logprobs_ok: bool | None = None
        self._ollama_logprobs_warned: bool = False

    def _inference_timeout(self) -> int:
        try:
            return int(self._settings.get("local_inference_timeout_sec", 120))
        except (TypeError, ValueError):
            return 120

    def attach_bundled_server(self, bundled_server: object) -> None:
        """Late-bind the BundledServer handle.

        Called by the API container after both services are constructed; lets
        bundled-mode requests resolve the live llama-server port without the
        LocalClient holding a forward reference at import time.
        """
        self._bundled_server = bundled_server

    def _bundled_port(self) -> int | None:
        """Return the live bundled-server port, or None if not running."""
        bs = self._bundled_server
        if bs is not None:
            try:
                if bs.is_running():
                    return bs.port()
            except Exception:  # noqa: BLE001
                pass
        # Fallback: read the persisted port file. Used when the wizard
        # started the server in a previous process and we just respawned.
        try:
            from core import paths as _paths  # noqa: PLC0415
            port_file = _paths.bundled_server_port_file()
            if port_file.exists():
                txt = port_file.read_text(encoding="utf-8").strip()
                if txt.isdigit():
                    return int(txt)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _url(self, backend: str | None = None) -> str:
        b = backend or self._effective_backend()
        if b == "ollama":
            return self._settings.get("ollama_url", "http://localhost:11434")
        if b == "bundled":
            port = self._bundled_port()
            if port is None:
                # Force callers down their existing failure path; an
                # unreachable URL surfaces the same way as a stopped backend.
                return "http://127.0.0.1:0"
            return f"http://127.0.0.1:{port}"
        return self._settings.get("lm_studio_url", "http://localhost:1234")

    def _effective_backend(self) -> str:
        """Resolve the active backend name from local_backend_mode + legacy
        default_local_backend.

        ``local_backend_mode == "auto"`` (or unset) preserves the historical
        single-knob behaviour: read ``default_local_backend``. Any explicit
        mode wins over auto so the user can pin a backend without touching
        the legacy setting.
        """
        mode = (self._settings.get("local_backend_mode", "auto") or "auto").strip()
        if mode in ("ollama", "lm_studio", "bundled"):
            return mode
        return self._settings.get("default_local_backend", "ollama") or "ollama"

    def _backend(self, backend: str | None = None) -> str:
        return backend or self._effective_backend()

    def _backend_for_model(self, model: str | None) -> str:
        """Pick the local backend that owns ``model``.

        When the user pins a bundled GGUF id in the chat-header model picker
        (via ``pinned_local_model``) the orchestrator forwards the model name
        without changing ``local_backend_mode``. Without this hook, the call
        falls through to whatever ``_effective_backend()`` returns (typically
        Ollama in auto mode) and 404s because Ollama doesn't have that
        model. So: check the ``bundled_models`` table first; if the id is a
        known bundled GGUF, route the call to the bundled server even when
        the global backend mode is set to something else.

        Falls back to the user-configured backend when ``model`` is missing
        or unknown to the bundled catalog — preserving existing behavior for
        Ollama / LM Studio pins and the auto path.
        """
        if model:
            try:
                from db import fetchone as _fetchone
                row = _fetchone(
                    "SELECT model_id FROM bundled_models WHERE model_id = ?",
                    (model,),
                )
                if row:
                    return "bundled"
            except Exception:
                # DB unavailable; fall back to the user-configured backend.
                pass
        return self._effective_backend()

    def _ensure_bundled_running(self, model: str | None) -> None:
        """Autostart the bundled llama-server when a bundled model is requested.

        The persistent autostart in ``Api.maybe_autostart_bundled_server``
        only fires on sidecar boot and only when ``local_backend_mode ==
        "bundled"``. The chat-header model picker (Phase 9 → Layer-C2 pin
        flow) writes ``pinned_local_model`` to a bundled GGUF id without
        touching the mode, so a previously-downloaded model still needs to
        be lazily spun up on the first chat turn. Best-effort: failures
        leave the existing 0-port URL path to surface the error to chat.
        """
        if not model:
            return
        bs = self._bundled_server
        if bs is None:
            return
        try:
            if bs.is_running() and bs.model_id() == model:
                return
        except Exception:
            return
        try:
            from db import fetchone as _fetchone
            row = _fetchone(
                "SELECT file_path FROM bundled_models WHERE model_id = ?",
                (model,),
            )
        except Exception:
            return
        if not row:
            return
        try:
            bs.start(row["file_path"], model_id=model)
        except Exception as exc:  # noqa: BLE001
            log.warning("bundled lazy-start failed for %s: %s", model, exc)

    def is_available(self, backend: str | None = None) -> bool:
        """Check if a local model backend is reachable."""
        try:
            b = self._backend(backend)
            url = self._url(b)
            endpoint = "/api/tags" if b == "ollama" else "/v1/models"
            return requests.get(url + endpoint, timeout=2).status_code == 200
        except Exception:
            return False

    def list_models(self, backend: str | None = None) -> list[str]:
        """Return available model names."""
        b = self._backend(backend)
        url = self._url(b)
        try:
            if b == "ollama":
                r = requests.get(url + "/api/tags", timeout=5)
                r.raise_for_status()
                return [m["name"] for m in r.json().get("models", [])]
            else:
                r = requests.get(url + "/v1/models", timeout=5)
                r.raise_for_status()
                return [m["id"] for m in r.json().get("data", [])]
        except Exception as exc:
            log.warning(f"list_models failed for backend '{b}': {exc}")
            return []

    def list_local_models(self) -> list[dict]:
        """Return installed local models across all configured local backends.

        Backward-compatible: callers that only need the flat row list still
        work. New callers should prefer ``list_local_sources()`` which also
        returns per-backend reachability so the UI can show *why* a backend
        contributed zero rows.
        """
        return self.list_local_sources()["models"]

    def list_local_sources(self) -> dict:
        """Return local models plus per-backend probe status.

        Shape::

            {
              "models":  [LocalModelRow, ...],
              "sources": [
                {"backend": "ollama",    "url": "...", "ok": bool,
                 "error": str|None, "count": int},
                {"backend": "lm_studio", ...},
                {"backend": "bundled",   "url": str|None, "ok": bool,
                 "error": str|None, "count": int},
              ],
            }

        Each ``LocalModelRow`` is::

            {"id": str, "size_bytes": int|None, "context_length": int|None,
             "quantization": str|None,
             "backend": "ollama"|"lm_studio"|"bundled",
             "loaded": bool}

        Ollama and LM Studio probes run in parallel so the slow case
        (both backends unreachable) costs one timeout instead of two.
        The bundled-models scan reads the filesystem and is essentially
        free, so it runs inline.
        """
        from concurrent.futures import ThreadPoolExecutor

        active = self._settings.get("default_local_model", "") or ""
        # Clamp to a sensible range so a corrupt settings value can't hang
        # the UI for minutes or shorten the probe below what loopback needs.
        try:
            timeout = float(self._settings.get("local_probe_timeout_sec", 2.0))
        except (TypeError, ValueError):
            timeout = 2.0
        timeout = max(0.5, min(timeout, 30.0))

        ollama_url = self._settings.get("ollama_url", "http://localhost:11434")
        lm_url = self._settings.get("lm_studio_url", "http://localhost:1234")

        # Probe both HTTP backends in parallel — total wait time is bounded
        # by the slowest probe rather than the sum of both.
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_ollama = pool.submit(self._probe_ollama, ollama_url, timeout, active)
            f_lm = pool.submit(self._probe_lm_studio, lm_url, timeout, active)
            ollama_models, ollama_err = f_ollama.result()
            lm_models, lm_err = f_lm.result()

        bundled_models, bundled_err, bundled_url = self._scan_bundled(active)

        return {
            "models": [*ollama_models, *lm_models, *bundled_models],
            "sources": [
                {
                    "backend": "ollama",
                    "url":     ollama_url,
                    "ok":      ollama_err is None,
                    "error":   ollama_err,
                    "count":   len(ollama_models),
                },
                {
                    "backend": "lm_studio",
                    "url":     lm_url,
                    "ok":      lm_err is None,
                    "error":   lm_err,
                    "count":   len(lm_models),
                },
                {
                    "backend": "bundled",
                    "url":     bundled_url,
                    "ok":      bundled_err is None,
                    "error":   bundled_err,
                    "count":   len(bundled_models),
                },
            ],
        }

    @staticmethod
    def _probe_ollama(url: str, timeout: float, active: str) -> tuple[list[dict], str | None]:
        """Return (rows, error). error is None on success; a short human
        string otherwise so the UI can surface it inline."""
        try:
            r = requests.get(url + "/api/tags", timeout=timeout)
            r.raise_for_status()
        except Exception as exc:
            log.warning("list_local_models: ollama probe failed: %s", exc)
            return [], _short_error(exc)
        rows: list[dict] = []
        for m in r.json().get("models", []) or []:
            details = m.get("details") or {}
            mid = m.get("name", "")
            rows.append({
                "id":             mid,
                "size_bytes":     m.get("size"),
                "context_length": None,
                "quantization":   details.get("quantization_level") or None,
                "backend":        "ollama",
                "loaded":         bool(mid) and mid == active,
            })
        return rows, None

    @staticmethod
    def _probe_lm_studio(url: str, timeout: float, active: str) -> tuple[list[dict], str | None]:
        try:
            r = requests.get(url + "/v1/models", timeout=timeout)
            r.raise_for_status()
        except Exception as exc:
            log.warning("list_local_models: lm_studio probe failed: %s", exc)
            return [], _short_error(exc)
        rows: list[dict] = []
        for m in r.json().get("data", []) or []:
            mid = m.get("id", "")
            ctx = m.get("context_length") or m.get("max_context_length")
            rows.append({
                "id":             mid,
                "size_bytes":     m.get("size") if isinstance(m.get("size"), int) else None,
                "context_length": ctx if isinstance(ctx, int) else None,
                "quantization":   m.get("quantization") or None,
                "backend":        "lm_studio",
                "loaded":         bool(mid) and mid == active,
            })
        return rows, None

    def _scan_bundled(self, active: str) -> tuple[list[dict], str | None, str | None]:
        """Scan the bundled-models directory for GGUF files.

        Returns (rows, error, url). ``url`` describes the bundled server
        endpoint (or the on-disk path when the server isn't running) so the
        UI can show users where the bundled models came from. The bundled
        downloader stages files into ``paths.bundled_models_dir()``; until
        this scan existed, those files never appeared in the model list
        even after a successful download.
        """
        rows: list[dict] = []
        try:
            from core import paths as _paths
            models_dir = _paths.bundled_models_dir()
        except Exception as exc:  # noqa: BLE001
            return rows, _short_error(exc), None

        # The "url" we report for the bundled source is the live llama-server
        # endpoint when it's running, else the on-disk path. Both are useful
        # for the UI's "where did this come from?" hint.
        port = self._bundled_port()
        url = f"http://127.0.0.1:{port}" if port else str(models_dir)

        try:
            gguf_files = sorted(models_dir.glob("*.gguf"))
        except Exception as exc:  # noqa: BLE001
            return rows, _short_error(exc), url

        for path in gguf_files:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            mid = path.stem
            rows.append({
                "id":             mid,
                "size_bytes":     size,
                "context_length": None,
                "quantization":   _quant_from_filename(path.name),
                "backend":        "bundled",
                "loaded":         bool(mid) and mid == active,
            })

        return rows, None, url

    def list_models_detailed(self, backend: str | None = None) -> list[dict]:
        """Return available models as list of {id, raw} dicts.

        Phase 3 needs structured model info for Qwen3 detection while keeping
        ``list_models()`` (string list) backward-compatible for existing
        callers that just need names.
        """
        b = self._backend(backend)
        url = self._url(b)
        try:
            if b == "ollama":
                r = requests.get(url + "/api/tags", timeout=5)
                r.raise_for_status()
                return [{"id": m["name"], "raw": m} for m in r.json().get("models", [])]
            r = requests.get(url + "/v1/models", timeout=5)
            r.raise_for_status()
            return [{"id": m["id"], "raw": m} for m in r.json().get("data", [])]
        except Exception as exc:
            log.warning(f"list_models_detailed failed for backend '{b}': {exc}")
            return []

    def detect_qwen3_30b_a3b(self, backend: str | None = None) -> dict:
        """Probe LM Studio (or Ollama) for a Qwen3-30B-A3B GGUF.

        Returns ``{"detected": bool, "model_id": str, "fallback_reason": str}``.

        - On hit: ``model_id`` is the matching id; ``fallback_reason`` is empty.
        - On miss with other models present: ``model_id`` is the first available
          model id; ``fallback_reason`` is a plain-English notice the UI can
          display verbatim.
        - On no backend reachable: empty ``model_id`` and a plain-English reason.

        Whenever a non-empty ``model_id`` is identified (detected or fallback)
        this fires the Phase 5 behavior-drift canary in a daemon thread.
        """
        models = self.list_models_detailed(backend)
        for m in models:
            if _QWEN3_30B_A3B_ID.search(str(m.get("id", ""))):
                self.signal_model_loaded(m["id"])
                return {
                    "detected":        True,
                    "model_id":        m["id"],
                    "fallback_reason": "",
                }
        if not models:
            return {
                "detected":        False,
                "model_id":        "",
                "fallback_reason": (
                    "No local model server is reachable. Start LM Studio (or "
                    "Ollama), load a model, then come back to this screen."
                ),
            }
        fallback = models[0]["id"]
        self.signal_model_loaded(fallback)
        return {
            "detected":        False,
            "model_id":        fallback,
            "fallback_reason": (
                f"Qwen3-30B-A3B not detected — falling back to '{fallback}'. "
                "Hybrid thinking will use a single budget cap; install a "
                "Qwen3-30B-A3B GGUF in LM Studio for the recommended setup."
            ),
        }

    def is_vision_model(self, model_id: str) -> bool:
        """Return True when ``model_id`` matches a vision-capable family.

        Compares the id against the ``vision_local_models`` setting using
        a case-insensitive prefix match. Empty ids and empty family lists
        return False.
        """
        if not model_id:
            return False
        families = self._settings.get("vision_local_models", []) or []
        if not isinstance(families, list):
            return False
        mid = str(model_id).strip().lower()
        for fam in families:
            if not fam:
                continue
            if mid.startswith(str(fam).strip().lower()):
                return True
        return False

    def chat_with_images(
        self,
        system: str,
        messages: list,
        images_b64: list[str],
        model: str | None = None,
        max_tokens: int = 2048,
    ) -> str:
        """Single-shot chat that attaches base64 images to the last user turn.

        Uses Ollama's ``/api/chat`` ``images`` field (a list of base64
        strings on the user message). Raises ``LocalVisionUnavailable``
        when the active model isn't vision-capable.

        ``messages`` is a list of {"role", "content"} dicts in the same
        shape as ``chat_multi_turn``. The images are attached to the last
        user message; if the list is empty or has no user message, this
        falls through to a plain text chat.
        """
        active = model or self._settings.get("default_local_model", "")
        families = self._settings.get("vision_local_models", []) or []
        if not self.is_vision_model(active):
            raise LocalVisionUnavailable(active, list(families))

        b = self._backend()
        url = self._url(b)
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        # Attach images to the last user message. Ollama's /api/chat
        # accepts ``images: [<base64>, ...]`` on a message.
        if images_b64 and msgs:
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "user":
                    # Strip data-uri prefixes if present.
                    cleaned = [
                        s.split(",", 1)[1] if isinstance(s, str) and s.startswith("data:")
                        else s
                        for s in images_b64
                    ]
                    msgs[i] = {**msgs[i], "images": cleaned}
                    break

        payload = {
            "model": active, "messages": msgs,
            "max_tokens": max_tokens, "stream": False,
        }
        try:
            if b == "ollama":
                r = requests.post(url + "/api/chat", json=payload, timeout=self._inference_timeout())
                r.raise_for_status()
                return r.json().get("message", {}).get("content", _FALLBACK)
            # LM Studio / bundled don't speak Ollama's image protocol.
            # Fall back to a plain chat so the user gets *something*.
            log.warning(
                "chat_with_images: backend %s does not support images; "
                "falling back to text-only.", b,
            )
            r = requests.post(url + "/v1/chat/completions", json=payload, timeout=self._inference_timeout())
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning("LocalClient.chat_with_images failed: %s", exc)
            return _FALLBACK

    def signal_model_loaded(self, model_id: str) -> None:
        """Notify the behavior-drift canary that ``model_id`` has been loaded.

        Best-effort: any failure (including a missing canary module on a
        partial install) is swallowed at warning level so the local-model
        load path can never raise into the request handler.
        """
        if not model_id:
            return
        try:
            from services import model_canary  # noqa: PLC0415
            model_canary.signal_model_loaded(self, model_id, self._settings)
        except Exception as exc:
            log.warning("signal_model_loaded(%s) failed: %s", model_id, exc)

    def chat(self, system: str, user_message: str, model: str | None = None,
             max_tokens: int = 2048) -> str:
        """Single-turn chat. Returns response text, or a fallback string on error."""
        model = model or self._settings.get("default_local_model", "")
        b = self._backend_for_model(model)
        if b == "bundled":
            self._ensure_bundled_running(model)
        url = self._url(b)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_message})
        payload = {"model": model, "messages": messages,
                   "max_tokens": max_tokens, "stream": False}
        try:
            if b == "ollama":
                r = requests.post(url + "/api/chat", json=payload, timeout=self._inference_timeout())
                r.raise_for_status()
                return r.json().get("message", {}).get("content", _FALLBACK)
            else:
                r = requests.post(url + "/v1/chat/completions", json=payload, timeout=self._inference_timeout())
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning(f"LocalClient.chat failed: {exc}")
            return _FALLBACK

    def chat_multi_turn(self, system: str, messages: list, model: str | None = None,
                        max_tokens: int = 2048) -> str:
        """Multi-turn chat. Returns response text, or a fallback string on error."""
        model = model or self._settings.get("default_local_model", "")
        b = self._backend_for_model(model)
        if b == "bundled":
            self._ensure_bundled_running(model)
        url = self._url(b)
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        payload = {"model": model, "messages": msgs,
                   "max_tokens": max_tokens, "stream": False}
        try:
            if b == "ollama":
                r = requests.post(url + "/api/chat", json=payload, timeout=self._inference_timeout())
                r.raise_for_status()
                return r.json().get("message", {}).get("content", _FALLBACK)
            else:
                r = requests.post(url + "/v1/chat/completions", json=payload, timeout=self._inference_timeout())
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning(f"LocalClient.chat_multi_turn failed: {exc}")
            return _FALLBACK

    def stream_multi_turn(self, system: str, messages: list, on_token,
                          model: str | None = None,
                          max_tokens: int = 2048) -> tuple[str, object]:
        """Streaming multi-turn chat. Calls on_token per chunk.

        Returns ``(full_text, usage)`` to mirror ``ClaudeClient.stream_multi_turn``;
        local backends do not report usage, so the second element is always None.
        Falls back to non-streaming on any error rather than crashing.
        """
        model = model or self._settings.get("default_local_model", "")
        b = self._backend_for_model(model)
        if b == "bundled":
            self._ensure_bundled_running(model)
        url = self._url(b)
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        payload = {"model": model, "messages": msgs,
                   "max_tokens": max_tokens, "stream": True}
        endpoint = "/api/chat" if b == "ollama" else "/v1/chat/completions"
        full = ""
        try:
            with requests.post(url + endpoint, json=payload, stream=True, timeout=self._inference_timeout()) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    raw = line.decode("utf-8")
                    if raw.startswith("data: "):
                        raw = raw[6:]
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                        text = (
                            chunk.get("message", {}).get("content", "")
                            if b == "ollama"
                            else chunk["choices"][0]["delta"].get("content", "")
                        )
                        if text:
                            on_token(text)
                            full += text
                    except Exception:
                        continue
        except Exception as exc:
            log.warning(f"LocalClient.stream_multi_turn failed: {exc}. Falling back to non-streaming.")
            # Fallback: try non-streaming so the user still gets a response
            full = self.chat_multi_turn(system, messages, model=model, max_tokens=max_tokens)
            if full and full != _FALLBACK:
                on_token(full)
        return full, None

    # ── Logprob helpers (QLPT Stage 1) ──────────────────────────────────────

    @staticmethod
    def _parse_lm_studio_logprobs(body: dict) -> list[float] | None:
        """Extract per-token logprobs from an LM Studio (OpenAI-compat) body.

        Returns None when the field is absent or malformed. The OpenAI
        shape is ``choices[0].logprobs.content[*].logprob``.
        """
        try:
            content = body["choices"][0]["logprobs"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        if not isinstance(content, list) or not content:
            return None
        out: list[float] = []
        for item in content:
            lp = item.get("logprob") if isinstance(item, dict) else None
            if isinstance(lp, bool) or not isinstance(lp, (int, float)):
                return None
            out.append(float(lp))
        return out or None

    @staticmethod
    def _parse_ollama_logprobs(body: dict) -> list[float] | None:
        """Extract per-token logprobs from an Ollama native /api/chat body.

        The native shape (since v0.12.11) is a top-level ``logprobs`` list
        of ``{"token": str, "logprob": float, ...}`` entries. Returns None
        when absent or malformed.
        """
        entries = body.get("logprobs") if isinstance(body, dict) else None
        if not isinstance(entries, list) or not entries:
            return None
        out: list[float] = []
        for item in entries:
            lp = item.get("logprob") if isinstance(item, dict) else None
            if isinstance(lp, bool) or not isinstance(lp, (int, float)):
                return None
            out.append(float(lp))
        return out or None

    @staticmethod
    def _version_at_least(actual: str, minimum: str) -> bool:
        """Compare dotted version strings as integer tuples.

        Falls back to False on any parse failure. Pre-release / build
        suffixes (e.g. "0.12.11-rc1") are stripped at the first non-digit
        component so a forward-compat release still satisfies the gate.
        """
        def parse(v: str) -> tuple[int, ...]:
            parts: list[int] = []
            for chunk in str(v).split("."):
                digits = ""
                for ch in chunk:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if not digits:
                    return tuple(parts)
                parts.append(int(digits))
            return tuple(parts)

        try:
            return parse(actual) >= parse(minimum)
        except Exception:
            return False

    def _ollama_supports_logprobs(self) -> bool:
        """Probe Ollama /api/version once and cache the result.

        Returns False on probe failure or when the running Ollama is
        older than OLLAMA_MIN_LOGPROBS_VERSION. Emits a one-time WARNING
        on the first negative result so users notice why margin-proxy is
        silently falling back to self-score.
        """
        cached = self._ollama_logprobs_ok
        if cached is not None:
            return cached
        ok = False
        try:
            url = self._url("ollama")
            r = requests.get(url + "/api/version", timeout=2)
            if r.status_code == 200:
                version = (r.json() or {}).get("version", "") or ""
                ok = self._version_at_least(version, OLLAMA_MIN_LOGPROBS_VERSION)
        except Exception as exc:
            log.debug("Ollama version probe failed: %s", exc)
            ok = False
        self._ollama_logprobs_ok = ok
        if not ok and not self._ollama_logprobs_warned:
            log.warning(
                "Ollama does not support native logprobs (need >= %s). "
                "Margin-proxy quality scorer will fall back to self-score "
                "for the Ollama backend in this session.",
                OLLAMA_MIN_LOGPROBS_VERSION,
            )
            self._ollama_logprobs_warned = True
        return ok

    # ── LLMClient interface ─────────────────────────────────────────────────

    def chat_unified(self, system, messages, max_tokens=4096):
        """Non-streaming unified chat; returns dict with optional logprobs.

        Backend-aware: LM Studio / bundled use the OpenAI-compat endpoint
        with ``logprobs=True, top_logprobs=1``. Ollama uses native
        ``/api/chat`` with the same options, but only when the server is
        new enough (see _ollama_supports_logprobs). On any failure the
        method falls back to plain ``chat_multi_turn`` and returns
        ``logprobs=None`` so the escalation ladder can drop to self-score.
        """
        # Respect the chat-header pin (Phase 9 → C carry-over): pinning a
        # bundled GGUF should route this through the bundled server too,
        # not whatever ``local_backend_mode`` says globally.
        pinned = self._settings.get("pinned_local_model", "") or ""
        model = pinned or (self._settings.get("default_local_model", "") or "")
        b = self._backend_for_model(model)
        if b == "bundled":
            self._ensure_bundled_running(model)
        url = self._url(b)
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        if b == "ollama":
            if not self._ollama_supports_logprobs():
                text = self.chat_multi_turn(system, messages, max_tokens=max_tokens)
                return {
                    "text": text or "", "input_tokens": 0,
                    "output_tokens": 0, "logprobs": None,
                }
            payload = {
                "model": model, "messages": msgs,
                "stream": False,
                "logprobs": True, "top_logprobs": 1,
                "options": {"num_predict": max_tokens},
            }
            try:
                r = requests.post(url + "/api/chat", json=payload, timeout=self._inference_timeout())
                r.raise_for_status()
                body = r.json()
                text = body.get("message", {}).get("content", _FALLBACK)
                return {
                    "text": text or "", "input_tokens": 0, "output_tokens": 0,
                    "logprobs": self._parse_ollama_logprobs(body),
                }
            except Exception as exc:
                log.warning("LocalClient.chat_unified (ollama+logprobs) failed: %s", exc)
                return {
                    "text": _FALLBACK, "input_tokens": 0,
                    "output_tokens": 0, "logprobs": None,
                }

        # OpenAI-compatible (LM Studio, bundled llama-server).
        payload = {
            "model": model, "messages": msgs,
            "max_tokens": max_tokens, "stream": False,
            "logprobs": True, "top_logprobs": 1,
        }
        try:
            r = requests.post(url + "/v1/chat/completions", json=payload, timeout=self._inference_timeout())
            r.raise_for_status()
            body = r.json()
            text = body["choices"][0]["message"]["content"]
            return {
                "text": text or "", "input_tokens": 0, "output_tokens": 0,
                "logprobs": self._parse_lm_studio_logprobs(body),
            }
        except Exception as exc:
            log.warning("LocalClient.chat_unified (lm_studio+logprobs) failed: %s", exc)
            return {
                "text": _FALLBACK, "input_tokens": 0,
                "output_tokens": 0, "logprobs": None,
            }

    def stream_unified(self, system, messages, on_token, max_tokens=4096):
        """Streaming unified chat; accumulates per-chunk logprobs.

        Same backend-aware policy as chat_unified. On stream failure we
        fall back to non-streaming via stream_multi_turn and return
        ``logprobs=None`` because we cannot reliably reconstruct them
        from a half-broken stream.
        """
        # Respect the chat-header pin (Phase 9 → C carry-over): pinning a
        # bundled GGUF should route this through the bundled server too,
        # not whatever ``local_backend_mode`` says globally.
        pinned = self._settings.get("pinned_local_model", "") or ""
        model = pinned or (self._settings.get("default_local_model", "") or "")
        b = self._backend_for_model(model)
        if b == "bundled":
            self._ensure_bundled_running(model)
        url = self._url(b)
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        ollama_native = (b == "ollama") and self._ollama_supports_logprobs()
        if b == "ollama" and not ollama_native:
            text, _usage = self.stream_multi_turn(
                system, messages, on_token, max_tokens=max_tokens,
            )
            return {
                "text": text or "", "input_tokens": 0,
                "output_tokens": 0, "logprobs": None,
            }

        if ollama_native:
            endpoint = "/api/chat"
            payload = {
                "model": model, "messages": msgs,
                "stream": True,
                "logprobs": True, "top_logprobs": 1,
                "options": {"num_predict": max_tokens},
            }
        else:
            endpoint = "/v1/chat/completions"
            payload = {
                "model": model, "messages": msgs,
                "max_tokens": max_tokens, "stream": True,
                "logprobs": True, "top_logprobs": 1,
            }

        full = ""
        logprobs: list[float] = []
        try:
            with requests.post(
                url + endpoint, json=payload, stream=True, timeout=self._inference_timeout(),
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    raw = line.decode("utf-8")
                    if raw.startswith("data: "):
                        raw = raw[6:]
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except Exception:
                        continue
                    try:
                        if ollama_native:
                            text = chunk.get("message", {}).get("content", "")
                            chunk_lp = self._parse_ollama_logprobs(chunk)
                        else:
                            choice = chunk["choices"][0]
                            text = choice.get("delta", {}).get("content", "") or ""
                            chunk_lp = self._parse_lm_studio_logprobs({
                                "choices": [{"logprobs": choice.get("logprobs")}]
                            })
                        if text:
                            on_token(text)
                            full += text
                        if chunk_lp:
                            logprobs.extend(chunk_lp)
                    except Exception:
                        continue
        except Exception as exc:
            log.warning(
                "LocalClient.stream_unified (%s+logprobs) failed: %s. Falling back.",
                b, exc,
            )
            text, _usage = self.stream_multi_turn(
                system, messages, on_token, max_tokens=max_tokens,
            )
            return {
                "text": text or "", "input_tokens": 0,
                "output_tokens": 0, "logprobs": None,
            }

        return {
            "text": full, "input_tokens": 0, "output_tokens": 0,
            "logprobs": logprobs if logprobs else None,
        }

    def client_name(self) -> str:
        return self._settings.get("default_local_model", "local")
