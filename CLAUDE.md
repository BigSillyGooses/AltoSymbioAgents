# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

AltoSymbioAgents is a desktop app for building teams of AI agents that work
together locally. It chats with Claude (Anthropic API) and local models
(Ollama / LM Studio), builds vector-indexed knowledge bases (RAG), runs
multi-agent workflows, and generates HTML design artifacts.

## Architecture (three processes)

```
Electron main (desktop-shell/)  ── spawns ──►  Python FastAPI sidecar (backend/)
        │  IPC + bearer token                         │  REST + SSE on 127.0.0.1:<random>
        ▼                                              ▼
React renderer (desktop-ui/)  ──── REST/SSE ───►  services (orchestration, RAG, MCP, security)
```

- **backend/** — Python 3.12 FastAPI sidecar. Routes are thin
  (`routes/*.py`) and delegate to the `core/api/` facade (`core/api/__init__.py`),
  which composes domain sub-APIs (`core/api/rag.py`, `core/api/web.py`, …) that
  share state via `BaseAPI` passthrough. Domain logic lives in `services/`.
  Routers are registered in `backend/server.py` via the **`ROUTER_SPECS`** tuple
  (single source of truth — the TS codegen reads it too).
- **desktop-ui/** — React 19 + TypeScript + Zustand + Tailwind. The typed API
  client is `desktop-ui/api/client.ts`; SSE handling + global state in
  `stores/appStore.ts`. `api/generated.d.ts` is **auto-generated** (see below).
- **desktop-shell/** — Electron main + preload + sidecar bootstrap.

## Dev commands

```bash
# Backend tests (from backend/)
cd backend && python -m pytest tests/ -q

# Frontend tests + typecheck (from repo root)
npx vitest run
npm run typecheck

# Regenerate the API TS types after changing routes/models (REQUIRED — CI has a
# drift gate). Needs `npm ci` first so openapi-typescript is on PATH.
python build-scripts/generate_api_types.py
```

## Conventions that matter

- **Lean, wheels-only bundle.** `backend/requirements.txt` ships in the
  installer (~80 MB) and must install with `--only-binary=:all:` — no compile
  step, no heavy browser binaries. Dev/test-only deps go in
  `requirements-dev.txt`. Before adding a runtime dep, confirm it has prebuilt
  wheels for Windows/macOS/Linux.
- **Manifest-driven settings.** Add a setting to `SETTINGS_DEFAULTS` in
  `backend/core/settings.py`; add a `FIELD_METADATA` row to make it appear in
  the Settings UI automatically (label/description/group) — no frontend change.
- **Route → facade → service.** New endpoint = a `routes/x.py` handler calling
  `get_api(request).<method>()`, a `core/api/x.py` sub-API doing the work, a
  one-line delegator on the `API` facade, and a `ROUTER_SPECS` entry.
- **SSE events.** Background/long work emits events via `self._emit(event,
  payload)`; the renderer drains them through `api/sse.ts` into the store.
- **Feature-flag new behavior, default off**, and keep flag-off turns
  byte-identical (several tests assert this). Best-effort hooks wrap work in
  try/except so one failure never breaks a chat turn.
- **Fetched/untrusted content** must pass `input_sanitizer.scan_document`
  before indexing (indirect-prompt-injection defence).

## Testing notes

- Backend uses pytest with an `in_memory_db` fixture (`tests/conftest.py`).
- The vector store is tested against real sqlite-vec using a deterministic
  bag-of-words embedder (see `tests/test_trajectory_store.py`) so tests don't
  download the fastembed model.
- Prefer real fixtures over mocks. Network-touching tests use a real local HTTP
  fixture server (`local_web_server` in `conftest.py`), not mocked clients.

## Web research (recent addition)

`services/web_research.py` + `core/api/web.py` give the Researcher live web
access: fetch a public URL (curl_cffi + Scrapling parser), security-scan it,
and index it into RAG (`doc_type="web"`). An SSRF guard (`_validate_url`)
screens every resolved IP and re-checks redirects. Gated behind
`web_research_enabled` (default off); Playwright/`StealthyFetcher` is an opt-in,
lazily-imported path so the default bundle ships no browser.
