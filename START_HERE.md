# Start here

You have two options, depending on what you want to do.

## I just want to use the app

Download the installer from the
[GitHub Releases](https://github.com/zasonic/AltoSymbiosisAgents/releases)
page and double-click it.

- Windows x64 only in v1.0.0. macOS / Linux installers are tracked for
  a future release.
- The installer is **not code-signed** — Windows will show "Windows
  protected your PC" on first run. Click **More info → Run anyway**.
- First launch downloads ~600 MB (Miniconda + Python deps). Allow ~5
  minutes on a typical connection. Subsequent launches work offline.

## I want to run from source (developers)

### Windows

Double-click **`START_HERE.bat`** in this folder.

The script installs Node dependencies (one-time, ~2 minutes) and then
starts the Electron shell. An app window opens automatically.

Requires [Node 20 or newer](https://nodejs.org/). Python is **not**
required — the bootstrap inside the app downloads its own Python
environment on first launch.

### macOS / Linux

```sh
npm install
npm run dev
```

Same Node 20+ requirement. macOS / Linux installers aren't shipped yet,
but `npm run dev` works for development.

## After the app starts

1. Watch the **status bar at the bottom** of the window. On first launch
   it shows bootstrap download progress (Miniconda + Python deps).
2. Once it says **Ready**, open **Settings** and paste your
   [Anthropic API key](https://console.anthropic.com/settings/keys).
3. Go to the **Chat** tab and click **+ New conversation** in the left
   sidebar — the message box only accepts typing once a conversation is
   selected.

## Something not working?

- App data + logs live in `%APPDATA%\altosybioagents\` on Windows.
  Inside the app: **Settings → Diagnostics → Open data folder**.
- Troubleshooting: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
- Architecture overview: [README.md](README.md).
