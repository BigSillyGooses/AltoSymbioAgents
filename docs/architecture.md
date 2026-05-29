# Architecture

Three processes, one user-facing app.

## Components

- **Electron main** (`desktop-shell/main.ts`) — opens the window,
  spawns the sidecar, brokers IPC, injects the bearer token on every
  renderer → sidecar request.
- **Renderer** (`desktop-ui/`) — React 19 + Zustand. Talks to the
  sidecar over REST and Server-Sent Events.
- **Python sidecar** (`backend/server.py`) — FastAPI, picks a random
  free port at startup and writes `PORT=<n>` to stdout. The brain of
  the app: chat orchestration, routing, memory, agents, MCP.

## Process boundaries

1. Electron main launches; spawns the sidecar via
   `desktop-shell/sidecar.ts`. The sidecar prints its port + bearer
   token on stdout.
2. Main captures both, exposes them to the renderer over IPC
   (`sidecar:get-info`).
3. Renderer (`desktop-ui/api/client.ts`) fetches `http://127.0.0.1:<port>`
   for REST and opens an SSE stream for live updates.
4. Main injects `Authorization: Bearer <token>` on every renderer →
   sidecar request via `session.webRequest.onBeforeSendHeaders`.

## Backend services

- `services/chat_orchestrator.py` — the per-turn chat loop.
- `services/hub_router.py` — single boundary for worker selection;
  scores agents by skill match, falls back to Qwen3 /no_think for
  ambiguous routes.
- `services/security_engine.py` — quarantine, deterministic rule
  enforcement, risk ledger.
- `services/memory/` — buffer, fact store, RAG retrieval (package).
- `services/governance.py` — per-agent tool/budget policy.
- `services/qwen_thinking.py` — Qwen3 hybrid /think + /no_think paths.

## Data layout

`backend/core/paths.py` is the single source of truth for user-data
paths:

- Settings JSON: `user_dir() / "settings.json"`
- SQLite DB: `user_dir() / "myai.db"`
- API key: OS keyring under service name `altosybioagents`

`user_dir()` reads the `MYAI_USER_DATA` environment variable, which
`backend/server.py` populates from the `--user-data <path>` CLI arg.
`desktop-shell/sidecar.ts` forwards `app.getPath("userData")` into that
arg, so the sidecar and the Electron shell share a single data root:
`%APPDATA%/altosybioagents` on Windows,
`~/Library/Application Support/altosybioagents` on macOS, and
`~/.config/altosybioagents` on Linux. Standalone runs (pytest, headless
`python server.py` without `--user-data`) fall back to platformdirs;
`paths.migrate_to_unified_userdata()` sweeps any pre-existing
platformdirs data into the unified directory on first boot after upgrade.

## Schema

`backend/db.py` is the single source of truth for SQLite. All schema
changes go through `_MIGRATIONS` with the `schema_migrations` table
tracking applied versions. Vector tables use sqlite-vec with
`vec_documents` / `vec_memories` virtual tables and `vec_*_map`
mapping tables that join on opaque IDs to the source rows.

## Build

- `npm run dev` — electron-vite dev mode + sidecar in subprocess.
- `npm run build` — produces `out/main`, `out/preload`, `out/renderer`.
- `npm run dist` — chains build + electron-builder for an NSIS Windows
  installer. The installer ships only the Electron shell + the backend
  source tree (as `resources/sidecar/`); no Python, no PyInstaller exe,
  no engine binaries.

The Python sidecar is **installed on the user's machine at first launch**
via the BootstrapWizard, not at build time. See `desktop-shell/bootstrap/`
for the three-stage installer (`miniconda.ts` → `sidecar_venv.ts` →
`waitForSidecarReady`) and `backend/pyproject.toml` for the
`[project.scripts] altosymbiosis-server = "server:main"` entry point that
pip generates as `Scripts/altosymbiosis-server.exe` inside the per-app venv.

## User-facing labels

`desktop-ui/i18n/en.json` maps internal names (table names, service
names) to display strings. The internal names stay frozen so existing
user databases keep working; only the display strings travel out to
the UI.
