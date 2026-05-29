# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project loosely tracks [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] — 2026-05-17

### Added
- Tri-state update mechanism (`update_mechanism`: `off` / `auto` / `manual`).
  Manual mode polls the GitHub releases API and shows a "Download" banner
  that opens the release page — useful when auto-install fails because the
  installer is unsigned.
- Status-bar warning chip when the OS keyring is unavailable and the
  Anthropic API key falls back to plaintext in `settings.json`.
- GPU-offload detection for the bundled local model. The sidecar parses
  llama-server's `offloaded N/M layers to GPU` line on boot and writes
  `bundled_gpu_offload_failed` so the renderer can warn when the Vulkan
  build silently fell back to CPU.
- Pinned binary versions for llama.cpp, whisper.cpp, and piper in
  `build-scripts/bundled_versions.json`. Two clones of the same git
  commit now produce byte-identical sidecar bundles.
- Frontend + backend test gates in the release workflow. A red test fails
  the release before any installer ships.
- Release workflow appends the installer's SHA256 to the GH release body.
- `SECURITY.md` covering the threat model, implemented boundaries, and the
  unsigned-installer/keyring-fallback caveats.
- "Reproducible builds" section in `docs/DEVELOPMENT.md`.

### Changed
- Bundled llama-server now uses the Vulkan Windows x64 build (single binary
  covering NVIDIA / AMD / Intel) with `--n-gpu-layers 99` and `--ctx-size
  16384` so the local model is actually fast on real hardware.
- `dev/install.ps1` uses `npm ci` (matching CI) and reads the PyInstaller
  pin from `backend/requirements-build.txt` (shared with the release and
  windows-smoke workflows).
- README documents the unsigned-installer SmartScreen path and links to
  `SECURITY.md` and the reproducible-build instructions.
- In-app update check (both `auto` and `manual` mechanisms) now includes
  pre-releases. Previously `/releases/latest` was used, which silently
  skipped any tag with a `-test.N` suffix.

### Removed
- `auto_update_enabled` boolean. Replaced by `update_mechanism`.
- Broken `BENCHMARKS.md` link in the README footer (the bench artifact
  lives in CI; it isn't user-facing).

### Fixed
- Windows-smoke workflow now launches the sidecar with `--token` and
  `--user-data` so `READY` actually appears within the 30s window. The
  previous bare `Start-Process server.exe` failed argparse and timed out.
- `docs/architecture.md` references to `services/memory.py` (the file is a
  package) and `backend/core/labels.py` (the file does not exist).
- GitHub repo slug in README, docs, in-app updater URLs, electron-builder
  publish config, and `UpdateBanner` test fixtures. The previous lowercase
  `zasonic/altosybioagents` slug 404'd against the actual repo
  `zasonic/AltoSymbiosisAgents`, breaking the Releases link and in-app
  auto-update.

## [1.0.0-test.1] — 2026-05-13 (pre-release)

Initial public release. Feature set:

- Local-first desktop app: Electron shell + FastAPI sidecar on 127.0.0.1.
- Chat with Claude (Sonnet 4.6 default) and local models routed by
  hub_router; simple turns go local when an Ollama / LM Studio / bundled
  llama.cpp backend is available.
- First-run wizard: API-key verification, optional bundled-model download
  (Qwen3-4B-Instruct-Q4_K_M, ~2.5GB), local-backend detection.
- Document indexing (RAG) via fastembed + sqlite-vec; memory + fact store;
  knowledge-graph view; per-conversation budget cap.
- Specialist agents with per-agent tool/budget policy; high-stakes
  consensus voting on critical turns; escalation channel for Lynch-style
  replacement-threat / autonomy-reduction triggers.
- Voice in / out (Whisper.cpp STT, Piper TTS), opt-in.
- Auto-update via electron-updater (silent, never force-restarts).
