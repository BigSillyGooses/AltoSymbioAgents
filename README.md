# altosybioagents

Build AI teams that work together on your desktop. Chat with Claude and
local models, create specialist agents, index your files, and keep
everything on your machine.

## Install

Download the latest installer from
[Releases](https://github.com/zasonic/AltoSymbiosisAgents/releases) and double-click.
No system Python or Node is required — on first launch the app downloads
Miniconda and creates its own Python environment under `%APPDATA%`.

**First launch requires internet** to download ~600 MB (Miniconda + Python
dependencies). The download takes about 5 minutes on a typical connection.
Subsequent launches work offline.

**Windows x64 only** in this release. macOS and Linux installers are
tracked for a future release. macOS / Linux developers can still run from
source — see [Run](#run) below.

You will need:
- An [Anthropic API key](https://console.anthropic.com/settings/keys).
- [Ollama](https://ollama.com/download) or [LM Studio](https://lmstudio.ai/)
  for local models (optional but recommended — keeps simple messages free).
  Local engines bundled with the installer return in the next release.

## Trust

The installer is **not code-signed**. Windows shows "Windows protected
your PC" on first run — click **More info → Run anyway**. Subsequent
launches go straight to the app.

If you're a cautious user:

- Source is here on GitHub. License: [MIT](LICENSE).
- Security boundaries, threat model, and reporting: [SECURITY.md](SECURITY.md).
- Build the installer from this commit and check the SHA256 matches the
  one in the release notes: [docs/DEVELOPMENT.md → Reproducible builds](docs/DEVELOPMENT.md).

## Run

Open the app, paste your API key in Settings, and start chatting. Messages
route automatically: simple turns go to a local model when one is available,
complex turns go to Claude.

For developers: install [Node 20+](https://nodejs.org/) and clone the repo.
On Windows, double-click **`START_HERE.bat`** at the repo root; on macOS /
Linux, run `npm install && npm run dev`. See
[`START_HERE.md`](START_HERE.md) for a one-page getting-started summary.
Python is **not** required to run `npm run dev` — the bootstrap install
(Miniconda + sidecar venv) happens inside the running Electron app the
first time you launch it. To run the backend pytest suite directly, set
up a Python 3.12 venv at `backend/.venv` and `pip install -r
backend/requirements.txt`.

```
START_HERE.bat               # install + dev (Windows, double-clickable)
npm install && npm run dev   # install + dev (macOS / Linux, dev only)
dev\build-installer.bat      # produce NSIS installer (Windows)
```

## Where things live

All app data lives under a single per-user directory:

- `%APPDATA%/altosybioagents/` on Windows
- `~/Library/Application Support/altosybioagents/` on macOS
- `~/.config/altosybioagents/` on Linux

It holds the bundled Python environment (`bin/miniconda/`,
`bin/sidecar-venv/`), the settings file (`settings.json`), the SQLite
database (`myai.db`), and all logs (`main.log`, `bootstrap.log`,
`sidecar.log`, `app.log`). To open the folder from inside the app:
**Settings → Diagnostics → Open data folder**.

(Older installs that wrote sidecar data to the platformdirs path
— `%LOCALAPPDATA%/altosybioagents/altosybioagents/` on Windows,
`~/.local/share/altosybioagents/` on Linux — are migrated the first
time the sidecar starts after upgrading.)

The **API key** lives in the OS keyring (Credential Manager / Keychain /
SecretService), never on disk. If the keyring is unavailable the app
falls back to plaintext in `settings.json` and shows a warning chip in
the status bar.

Source layout:

- `desktop-ui/` — React 19 renderer (UI, stores, panels).
- `desktop-shell/` — Electron main + preload, plus `bootstrap/` for the
  first-launch Miniconda + venv installer.
- `backend/` — Python FastAPI sidecar (pip-installable package). Bound
  to `127.0.0.1` on an OS-assigned port.
- `branding/` — app icon.
- `build-scripts/` — Python helpers used at release time (asset fetch,
  API type generation, benchmark report).
- `dev/` — developer scripts (`dev.bat`, `build-installer.bat`,
  bundle-size / dead-code checks, vitest setup).
- `legacy/` — pre-Pinokio PyInstaller spec, retained for reference.

Deeper docs:
- [docs/USER-GUIDE.md](docs/USER-GUIDE.md) — features and how to use them.
- [docs/architecture.md](docs/architecture.md) — services, schemas, IPC.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — dev setup and build flow.
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — common errors.
- [docs/FAQ.md](docs/FAQ.md) — common questions.
- [docs/legacy.md](docs/legacy.md) — pre-v6 code lives on the legacy/v5 branch.
- [docs/code-audit-2026-05.md](docs/code-audit-2026-05.md) — historical
  code audit (all findings already fixed).
- [CHANGELOG.md](CHANGELOG.md) — user-facing changes per release.

MIT — see [LICENSE](LICENSE).
