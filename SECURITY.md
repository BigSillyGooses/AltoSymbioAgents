# Security

This document describes the security posture of `altosybioagents` — what
the app defends against, what it doesn't, and how to report problems.

## Threat model

- **Single-user desktop app.** One human, one machine. No multi-tenant
  isolation, no privilege separation between local users.
- **Loopback-only sidecar.** The Python FastAPI process binds to
  `127.0.0.1` on a random free port. No LAN or WAN exposure.
- **Untrusted content** enters the app via two channels: documents the
  user explicitly attaches, and Claude responses (including content
  Claude transcribed from RAG / web fetches). Both are treated as
  potentially adversarial and routed through the firewall /
  input-sanitizer pipeline before they reach tool-execution code paths.

## Security boundaries implemented

### Electron shell

- `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`
  on every BrowserWindow.
- Content-Security-Policy locked to `'self' + http://127.0.0.1:*` in
  the renderer's `index.html`. No external script sources.
- All renderer-to-sidecar HTTP requests carry the bearer token via
  `webRequest.onBeforeSendHeaders` (main process), so the token never
  travels through renderer JavaScript and never lands in URL or log
  lines.
- `will-navigate` and `setWindowOpenHandler` block navigation away from
  the app shell. External `http(s)` links open via `shell.openExternal`
  in the user's default browser.
- DevTools are disabled in packaged builds.

### Sidecar

- Bearer-token authentication on every route. Tokens are compared with
  `secrets.compare_digest` to avoid timing-leak side channels.
- Subprocess spawns (llama-server, whisper-cli, piper) are argv-only —
  never `shell=True`, never a shell-interpolated string.
- Bundled model downloads are sha256-verified against the catalog
  written by `build-scripts/fetch_bundled_assets.py` before they're
  staged into `userData/`.
- Filesystem writes from chat exports are constrained to the user's
  home directory.

### Secrets

- The Anthropic API key is stored in the OS-native keyring (Windows
  Credential Manager, macOS Keychain, Linux SecretService).
- When the keyring is unavailable, the app falls back to plaintext in
  `settings.json`. The status bar shows a yellow "⚠ API key stored in
  plaintext" chip so the user knows what's in effect. This is a
  documented degraded mode, not a vulnerability.

### Binary supply chain

- Sidecar dependencies are pinned in `backend/requirements.txt`.
- PyInstaller is pinned in `backend/requirements-build.txt` and used by
  `dev/install.ps1`, the release workflow, and the windows-smoke
  workflow so all three produce comparable bundles.
- Native binaries (llama.cpp, whisper.cpp, piper) are pinned by tag in
  `build-scripts/bundled_versions.json`. Two clones of the same git
  commit produce byte-identical sidecar bundles; the release notes
  publish the installer's SHA256 so users can verify their download.

## What is NOT done

- **The installer is not code-signed.** Windows shows "Windows
  protected your PC" on first run. There is no Authenticode certificate
  attesting that the binary came from a known publisher.
- **No Python sandbox.** The sidecar runs with the user's full
  privileges. A code-execution bug in any of the sidecar's
  dependencies (Pillow, fastembed, anthropic, etc.) would land on
  the desktop user's account.
- **No formal pentest.** Boundaries described above are designed-in
  but have not been audited by an independent third party.

## Reporting a vulnerability

Please use the GitHub **Security advisory** flow:

> Repository → Security tab → Report a vulnerability

Do not open a public issue for sensitive reports. We'll acknowledge
within a few business days and coordinate a fix and disclosure.

## Verifying your installer

The release notes for each tag include the SHA256 of the published
`altosybioagents-Setup-<version>.exe`. To verify your download:

```pwsh
Get-FileHash altosybioagents-Setup-1.0.0.exe -Algorithm SHA256
```

The hash should match the release notes. If it doesn't, please open an
issue (or a security advisory if you suspect tampering rather than a
build-pipeline regression).

For full source-build verification, see
[docs/DEVELOPMENT.md → Reproducible builds](docs/DEVELOPMENT.md).
