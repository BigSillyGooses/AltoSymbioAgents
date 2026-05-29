# Development

Setup, daily workflow, and conventions.

## First-time setup (any OS)

1. Install **Node 20+**.
2. Install JS deps:
   ```
   npm install
   ```

That's all `npm run dev` needs. Python is **not** required to run the
Electron app from source — the bootstrap install (Miniconda + sidecar
venv) happens inside the running app the first time you launch it, see
`desktop-shell/bootstrap/`.

If you want to run the backend pytest suite directly (outside the
electron-app bootstrap), set up a Python 3.12 venv yourself:
```
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cd ..
```
Windows devs can run `dev\dev.bat` instead of `npm install && npm run dev`.

## Layout

```
desktop-ui/        React renderer (Vite, Tailwind)
desktop-shell/     Electron main + preload + sidecar manager
desktop-shell/bootstrap/  First-launch Miniconda + venv installer
backend/           Python FastAPI sidecar (pip-installable package)
branding/          App icon
build-scripts/     npm-script helpers (benchmarks + codegen)
dev/               Developer entry scripts (dev.bat, build-installer.bat)
legacy/            Pre-Pinokio PyInstaller spec, retained for reference
```

Entry points:
- Renderer: `desktop-ui/main.tsx` → `desktop-ui/App.tsx`
- Electron main: `desktop-shell/main.ts`
- Sidecar: `backend/server.py`

## Daily dev loop

```
npm run dev           # electron-vite dev + sidecar; renderer hot-reloads
npm run typecheck     # tsc on both desktop-shell and desktop-ui
npm run test:frontend # vitest run
cd backend && python -m pytest -q
```

The renderer hot-reloads on save. The Electron main process and
sidecar restart automatically when their source files change.

## Building

```
npm run build            # electron-vite production build into out/
npm run dist             # build + electron-builder NSIS
```

On Windows, `dev\build-installer.bat` chains `npm install + npm run build
+ electron-builder --win` and produces `dist/altosybioagents-Setup-<version>.exe`.
The installer no longer bundles Python — bootstrap (Miniconda + venv) runs
on first launch of the INSTALLED app. Test on a clean VM (no Python, no
Node) to confirm the end-to-end first-run flow.

## Reproducible builds

The installer is intended to be reproducible: anyone with a Windows box,
Node 20+, and Python 3.12+ can build the same `.exe` from the same git
commit. To verify a release you downloaded matches the one that was tagged:

```
git clone https://github.com/zasonic/AltoSymbiosisAgents.git
cd AltoSymbiosisAgents
git checkout v<x.y.z>          # the tag for the release you have
dev\build-installer.bat        # builds dist\altosybioagents-Setup-<x.y.z>.exe
Get-FileHash dist\*.exe -Algorithm SHA256
```

The release notes for the matching tag list the SHA256 of the published
installer. The hash from `Get-FileHash` should be identical. If it isn't,
please open an issue — either there is a non-deterministic input we missed
(file me one as a bug) or the release was published from a different
source state (file me one as a security concern).

What's pinned to make this work:
- JS deps in `package-lock.json` (the build uses `npm ci`).
- Python deps in `backend/requirements.txt` (mirrored into
  `backend/pyproject.toml`'s `[project] dependencies`).
- Miniconda URL + SHA256 in `desktop-shell/bootstrap/miniconda.ts`
  (`MINICONDA_URL` and `MINICONDA_SHA256` constants — re-verified from
  https://repo.anaconda.com/miniconda/ at pin time).
- llama.cpp / whisper.cpp / piper binaries: bundling deferred to the
  follow-up engine-download branch; not in this release's installer.

Bumping any of those means editing the file in a commit. Never re-tag a
release: the whole point of the pin is that `git checkout v1.0.0` today
fetches the same upstream binaries it did the day v1.0.0 was published.

## Code style

- TypeScript: strict mode; no `any` unless commented why.
- Python: type hints on all public functions; ruff-clean.
- Comments explain WHY, not WHAT.
- Comments rot when the code moves; prefer good names over comments.

## Pull requests

Push branches as `claude/<short-description>` (or your own prefix).
One concern per PR; commit messages in the form `<area>: <summary>`.
CI runs `npm run typecheck`, `npm run test:frontend`, and the backend
pytest suite.

## Schema and userData invariants

These are hard rules — breaking any of them breaks existing user
installs:

- `backend/core/paths.py` paths must not be renamed.
- All schema changes go through `_MIGRATIONS` in `backend/db.py` with
  a new version string and the `schema_migrations` table.
- Settings keys in `backend/core/settings.py` stay frozen.
- `electron-builder.yml` `extraResources` must keep resolving.
- `electron.vite.config.ts` `@/` alias must keep resolving to
  `desktop-ui/`.
- `backend/pyproject.toml` `[tool.setuptools.packages.find]` must keep
  including `core*` / `routes*` / `services*`; data files
  (`core/templates/*`, `core/config/*`) must stay in
  `[tool.setuptools.package-data]`.

## Security benchmarks (AgentDojo)

`.github/workflows/security-bench.yml` runs the four published AgentDojo
suites (workspace, slack, banking, travel) against the security stack
weekly (Mondays 06:00 UTC) and on manual `workflow_dispatch`. It commits
the per-suite `benchmarks/<suite>.json` files plus a regenerated
[BENCHMARKS.md](../BENCHMARKS.md) back to `main` with `[skip ci]`, and
fails the build if any suite's ASR exceeds the ceiling configured in
[`benchmarks/thresholds.json`](../benchmarks/thresholds.json).

Local reproduction (Windows):

```
dev\run-bench.bat
```

Or one suite at a time:

```
pip install -r backend\requirements-bench.txt
python -m backend.tests.agentdojo.run_suites --suite workspace --output benchmarks\workspace.json
python build-scripts\generate_benchmarks_md.py
```

Bench-only deps live in `backend/requirements-bench.txt`. They are NEVER
imported at runtime — `backend/pytest.ini` excludes
`backend/tests/agentdojo/` from default collection, so a regular
`pytest tests/` run still works on machines that have not installed the
bench deps.

### Required GitHub Actions secret

Add one secret under **Settings → Secrets and variables → Actions** on
the GitHub repository:

| Secret | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Used by the Reader pass and the Actor's Anthropic LLM during the bench. |

That is the only external setup. The workflow commits results back via
the default `GITHUB_TOKEN` that ships with every repository — the
workflow grants it `contents: write`, and the `[skip ci]` marker on the
commit message prevents retrigger loops. No personal access token, no
deploy key, no fork required. If the repo owner declines to add
`ANTHROPIC_API_KEY`, the workflow still runs but every suite step fails
with a clear "ANTHROPIC_API_KEY env var is required" message; the local
`dev\run-bench.bat` path is unaffected.

### Tightening the threshold

Edit `benchmarks/thresholds.json`. The `max_asr_pct` per suite is the
hard ceiling; the `baseline_asr_pct` is the Hackett et al. (ACL 2025)
monolithic reference, only used for the rendered table.
