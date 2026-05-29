// desktop-shell/main.ts — Electron main process entrypoint.
//
// Lifecycle:
//   1. App ready → spawn sidecar (SidecarManager) → open BrowserWindow
//   2. Wire IPC: sidecar:get-info, sidecar:restart, dialog:*, shell:*, updater:*
//   3. On quit → POST /shutdown to sidecar → kill if grace period elapses
//
// Security: contextIsolation:true, nodeIntegration:false, sandbox:true.
// All network is 127.0.0.1 — see CSP in index.html.

import { app, BrowserWindow, dialog, ipcMain, Menu, session, shell } from "electron";
import { autoUpdater } from "electron-updater";
import { createWriteStream, existsSync, mkdirSync, statSync, renameSync, WriteStream } from "node:fs";
import { rm, writeFile } from "node:fs/promises";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  getBinRoot,
  getMinicondaPython,
  getResourceDir,
  getSidecarEntryPoint,
  isBootstrapped,
  SidecarBootError,
  waitForSidecarReady,
} from "./bootstrap/bin_manager";
import { downloadAndInstallMiniconda } from "./bootstrap/miniconda";
import { createSidecarVenv } from "./bootstrap/sidecar_venv";
import { SidecarManager } from "./sidecar";

// Only meaningful in development — `resolveSpawnArgs` consults this to find
// the source-tree venv. In a packaged build the path resolves to a location
// inside the asar archive that doesn't exist on disk, so the SidecarManager
// only reads it from its `else (app.isPackaged)` branch.
const PROJECT_ROOT = app.isPackaged
  ? app.getAppPath()
  : fileURLToPath(new URL("../..", import.meta.url));

let mainWindow: BrowserWindow | null = null;
let sidecar: SidecarManager | null = null;
let mainLogStream: WriteStream | null = null;
let bootstrapLogStream: WriteStream | null = null;
let updaterTimer: NodeJS.Timeout | null = null;
// Cached result of bootstrap/bin_manager.isBootstrapped(), populated once in
// whenReady() and refreshed via the app:recheck-bootstrap IPC handler. The
// BootstrapWizard relies on this to drive its overlay; sidecar boot is gated
// on it too (we don't start the FastAPI server until the venv is in place).
let bootstrappedCache: boolean | null = null;

const MAIN_LOG_MAX_BYTES = 10 * 1024 * 1024; // 10MB
const BOOTSTRAP_LOG_MAX_BYTES = 10 * 1024 * 1024; // 10MB — mirrors sidecar.log

function logToFile(text: string): void {
  try {
    mainLogStream?.write(text);
  } catch {
    /* ignore */
  }
}

function sendToRenderer(channel: string, payload?: unknown): void {
  // Optional chaining on mainWindow only catches the null case, not the
  // destruction case: during app quit, mainWindow remains non-null for a
  // brief window after webContents has been torn down. Late SidecarManager
  // 'status' / autoUpdater events that arrive in that window blow up with
  // `TypeError: Object has been destroyed`. Guard both layers.
  try {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    const wc = mainWindow.webContents;
    if (!wc || wc.isDestroyed()) return;
    wc.send(channel, payload);
  } catch {
    /* Renderer torn down between the isDestroyed check and the send call. */
  }
}

function bootstrapLog(text: string): void {
  // Bootstrap-specific log so the wizard's "Open log folder" button has a
  // dedicated file to point users at. Also mirrored into main.log via
  // logToFile so the full session timeline stays in one place.
  try {
    bootstrapLogStream?.write(text);
  } catch {
    /* ignore */
  }
  logToFile(text);
}

// Per-handler call timestamps (epoch ms) for the IPC sliding-window rate limiter.
const _rateLimitWindows = new Map<string, number[]>();

function rateLimit(
  name: string,
  maxPerSecond: number,
  handler: (...args: unknown[]) => unknown,
): (...args: unknown[]) => unknown {
  return (...args: unknown[]) => {
    const now = Date.now();
    const windowStart = now - 1000;
    let calls = _rateLimitWindows.get(name);
    if (!calls) {
      calls = [];
      _rateLimitWindows.set(name, calls);
    }
    // Drop timestamps that have aged out of the 1s window.
    while (calls.length > 0 && calls[0] < windowStart) calls.shift();
    if (calls.length >= maxPerSecond) {
      const message = `rate limit exceeded: ${name}`;
      logToFile(`ipc rate limit: ${name} (${calls.length}/${maxPerSecond} in 1s)\n`);
      throw new Error(message);
    }
    calls.push(now);
    return handler(...args);
  };
}

function bootMainLog(userDataDir: string): void {
  if (!existsSync(userDataDir)) mkdirSync(userDataDir, { recursive: true });
  const path = join(userDataDir, "main.log");
  try {
    if (existsSync(path) && statSync(path).size > MAIN_LOG_MAX_BYTES) {
      try {
        renameSync(path, `${path}.1`);
      } catch {
        /* ignore */
      }
    }
  } catch {
    /* ignore */
  }
  mainLogStream = createWriteStream(path, { flags: "a" });
  logToFile(`\n=== main starting at ${new Date().toISOString()} ===\n`);
}

function bootBootstrapLog(userDataDir: string): void {
  // Mirrors bootMainLog's rotation logic for the bootstrap-specific log file.
  // Wizard's "Open log folder" action surfaces this path to users so they
  // have a single file to read when reporting install failures.
  if (!existsSync(userDataDir)) mkdirSync(userDataDir, { recursive: true });
  const path = join(userDataDir, "bootstrap.log");
  try {
    if (existsSync(path) && statSync(path).size > BOOTSTRAP_LOG_MAX_BYTES) {
      try {
        renameSync(path, `${path}.1`);
      } catch {
        /* ignore */
      }
    }
  } catch {
    /* ignore */
  }
  bootstrapLogStream = createWriteStream(path, { flags: "a" });
  bootstrapLog(`\n=== bootstrap log opened at ${new Date().toISOString()} ===\n`);
}

/**
 * Convert a thrown error into a labeled `bootstrap:progress` payload and
 * forward to the wizard. Recognises the labeled error classes from
 * miniconda.ts / sidecar_venv.ts / bin_manager.ts so the wizard can branch
 * its retry copy on `error.label`; anything else surfaces as a generic
 * "BootstrapError" with the raw message.
 */
function sendBootstrapError(
  send: (channel: string, payload?: unknown) => void,
  step: number,
  err: unknown,
  sidecarLogPath: string,
): { ok: false; error: { label: string; cause: string } } {
  const label =
    err instanceof Error && "label" in err && typeof (err as { label: unknown }).label === "string"
      ? ((err as { label: string }).label)
      : "BootstrapError";
  const cause = err instanceof Error ? err.message : String(err);
  // logPath: SidecarBootError points users at sidecar.log (the failure
  // happened post-spawn, so uvicorn's stderr is captured there); every
  // other phase points at bootstrap.log.
  const logPath = err instanceof SidecarBootError
    ? sidecarLogPath
    : join(app.getPath("userData"), "bootstrap.log");
  bootstrapLog(`step${step} FAILED: [${label}] ${cause}\n`);
  send("bootstrap:progress", { step, error: { label, cause, logPath } });
  return { ok: false, error: { label, cause } };
}

function wireSidecarAuthHeader(targetSession: Electron.Session): void {
  // Inject Authorization: Bearer <token> on every renderer request to the
  // sidecar so EventSource (which can't set headers) and stray fetch() calls
  // don't have to ship the token in URLs or log lines. The token lives in
  // the main process; the renderer never needs to see it.
  targetSession.webRequest.onBeforeSendHeaders(
    { urls: ["http://127.0.0.1:*/*"] },
    (details, callback) => {
      const info = sidecar?.getInfo();
      if (info) {
        try {
          if (new URL(details.url).port === String(info.port)) {
            details.requestHeaders["Authorization"] = `Bearer ${info.token}`;
          }
        } catch {
          // Malformed URL — pass through without injecting auth rather than
          // crashing the request listener.
        }
      }
      callback({ requestHeaders: details.requestHeaders });
    },
  );
}

async function createWindow(): Promise<void> {
  wireSidecarAuthHeader(session.defaultSession);

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 1024,
    minHeight: 660,
    backgroundColor: "#0a0a0c",
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(__dirname, "../preload/index.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      // Block the F12 / Ctrl+Shift+I shortcut in packaged builds so a
      // mis-click can't surface internals to end users. Devs running
      // `npm run dev` keep DevTools.
      devTools: !app.isPackaged,
    },
  });

  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) {
      shell.openExternal(url).catch(() => {});
    }
    return { action: "deny" };
  });

  // Block in-window navigation away from the app shell. Without this,
  // a stray <a target="_self"> or `window.location = "https://evil"` —
  // whether from a renderer bug or XSS — could replace the app UI with
  // remote content. Allow only the dev-server URL and the packaged
  // file:// renderer; everything else opens externally instead.
  mainWindow.webContents.on("will-navigate", (event, url) => {
    const dev = process.env.ELECTRON_RENDERER_URL;
    if (dev && url.startsWith(dev)) return;
    if (url.startsWith("file://")) return;
    event.preventDefault();
    if (/^https?:\/\//i.test(url)) {
      shell.openExternal(url).catch(() => {});
    }
  });

  if (process.env.ELECTRON_RENDERER_URL) {
    await mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL);
  } else {
    await mainWindow.loadFile(join(__dirname, "../renderer/index.html"));
  }
}

function wireIpc(): void {
  ipcMain.handle("sidecar:get-info", () => sidecar?.getInfo() ?? null);
  ipcMain.handle(
    "sidecar:restart",
    rateLimit("sidecar:restart", 2, async () => {
      if (!sidecar) throw new Error("Sidecar manager not initialized");
      return sidecar.restart();
    }),
  );

  ipcMain.handle(
    "dialog:select-folder",
    rateLimit("dialog:select-folder", 4, async () => {
      if (!mainWindow) return null;
      const result = await dialog.showOpenDialog(mainWindow, {
        properties: ["openDirectory"],
      });
      if (result.canceled || result.filePaths.length === 0) return null;
      return result.filePaths[0];
    }),
  );

  ipcMain.handle(
    "dialog:select-files",
    rateLimit("dialog:select-files", 4, (async (_e, filters?: { name: string; extensions: string[] }[]) => {
      if (!mainWindow) return [];
      const result = await dialog.showOpenDialog(mainWindow, {
        properties: ["openFile", "multiSelections"],
        filters: filters ?? [
          {
            name: "Documents",
            extensions: [
              "txt", "md", "pdf", "py", "js", "json", "csv", "html", "css",
              "ts", "jsx", "tsx", "yaml", "yml", "toml", "xml", "sql", "sh",
              "bat", "ps1", "rs", "go", "java", "c", "cpp", "h", "rb",
            ],
          },
          { name: "All Files", extensions: ["*"] },
        ],
      });
      if (result.canceled) return [];
      return result.filePaths;
    }) as (...args: unknown[]) => unknown),
  );

  ipcMain.handle(
    "dialog:save-file",
    rateLimit("dialog:save-file", 4, (async (_e, { suggestedName, content }: { suggestedName: string; content: string }) => {
      if (!mainWindow) return { ok: false, error: "no window" };
      // Strip any path components from the renderer-supplied name so a
      // compromised renderer can't pre-fill the dialog with /etc/passwd or
      // C:\Windows\System32\... and trick the user into clicking Save.
      const safeName = basename(typeof suggestedName === "string" ? suggestedName : "");
      if (!safeName || safeName === "." || safeName === "..") {
        return { ok: false, error: "invalid name" };
      }
      const SAVE_FILE_MAX_BYTES = 50 * 1024 * 1024; // 50 MiB
      if (typeof content !== "string") {
        return { ok: false, error: "content too large" };
      }
      // Cap on the encoded byte length, not String.length, so non-ASCII
      // content can't sneak past the limit at ~2× its UTF-16 size.
      if (Buffer.byteLength(content, "utf-8") > SAVE_FILE_MAX_BYTES) {
        return { ok: false, error: "content too large" };
      }
      const result = await dialog.showSaveDialog(mainWindow, {
        defaultPath: safeName,
      });
      if (result.canceled || !result.filePath) return { ok: false, cancelled: true };
      try {
        await writeFile(result.filePath, content, "utf-8");
        return { ok: true, path: result.filePath };
      } catch (err) {
        return { ok: false, error: err instanceof Error ? err.message : String(err) };
      }
    }) as (...args: unknown[]) => unknown),
  );

  ipcMain.handle(
    "export:pdf",
    rateLimit("export:pdf", 4, (async (_e, payload: unknown) => {
      // Render the renderer-supplied HTML in a hidden BrowserWindow, then
      // hand the bytes off to a native Save dialog. printToPDF runs entirely
      // in-process — no headless Chrome spawn, no third-party dependency.
      if (!mainWindow) return { ok: false, error: "no window" };
      const data = (payload ?? {}) as { html?: unknown; suggestedName?: unknown };
      const html = typeof data.html === "string" ? data.html : "";
      const rawName =
        typeof data.suggestedName === "string" ? data.suggestedName : "";
      // Same defensive cleanup as dialog:save-file — strip any path
      // components and reject bare "."/".." so a compromised renderer can't
      // pre-fill the dialog with /etc/anything.
      const safeName = basename(rawName) || "conversation.pdf";
      if (safeName === "." || safeName === "..") {
        return { ok: false, error: "invalid name" };
      }
      // 25 MiB cap — a 25 MiB string is already an absurd HTML payload, and
      // the cap stops a runaway renderer from feeding us multi-GB documents.
      const HTML_MAX_BYTES = 25 * 1024 * 1024;
      if (Buffer.byteLength(html, "utf-8") > HTML_MAX_BYTES) {
        return { ok: false, error: "html too large" };
      }
      if (!html) return { ok: false, error: "empty html" };

      const win = new BrowserWindow({
        show: false,
        webPreferences: {
          // Hidden window only renders the HTML we just built — no preload,
          // no Node integration, no remote loading. Sandbox keeps it safe
          // even if the HTML contained a script tag.
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
          javascript: false,
        },
      });
      try {
        await win.loadURL(
          "data:text/html;charset=utf-8," + encodeURIComponent(html),
        );
        const pdfBytes = await win.webContents.printToPDF({
          printBackground: true,
          pageSize: "A4",
        });
        const result = await dialog.showSaveDialog(mainWindow, {
          defaultPath: safeName,
          filters: [{ name: "PDF", extensions: ["pdf"] }],
        });
        if (result.canceled || !result.filePath) {
          return { ok: false, cancelled: true };
        }
        await writeFile(result.filePath, pdfBytes);
        return { ok: true, path: result.filePath };
      } catch (err) {
        return {
          ok: false,
          error: err instanceof Error ? err.message : String(err),
        };
      } finally {
        if (!win.isDestroyed()) win.destroy();
      }
    }) as (...args: unknown[]) => unknown),
  );

  ipcMain.handle("shell:open-external", async (_e, url: string) => {
    if (typeof url !== "string") return;
    if (!/^https?:\/\//i.test(url)) return;
    await shell.openExternal(url);
  });

  ipcMain.handle("app:version", () => app.getVersion());
  ipcMain.handle("app:user-data-path", () => app.getPath("userData"));

  ipcMain.handle("app:is-bootstrapped", async () => {
    if (bootstrappedCache == null) {
      bootstrappedCache = await isBootstrapped();
    }
    return bootstrappedCache;
  });
  ipcMain.handle("app:recheck-bootstrap", async () => {
    bootstrappedCache = await isBootstrapped();
    return bootstrappedCache;
  });
  ipcMain.handle("app:platform", () => process.platform);

  // ── Bootstrap orchestrator ────────────────────────────────────────────────
  //
  // Single end-to-end install flow driving the BootstrapWizard. Three phases,
  // each skipped if its terminal artifact already exists so a Retry after a
  // SidecarBootError doesn't redo the 600 MB Miniconda download.
  //
  //   step 1 "Downloading Python"     — skipped if bin/miniconda/python.exe exists
  //   step 2 "Setting up environment" — skipped if bin/sidecar-venv/Scripts/altosymbiosis-server.exe exists
  //   step 3 "Almost done"            — always runs; spawn sidecar + waitForSidecarReady(30s)
  //
  // Progress streams via bootstrap:progress (server→renderer):
  //   { step, pct, phase?, message? }                                  during work
  //   { step, pct: 100 } then bootstrap:done                           on success
  //   { step, error: { label, cause, logPath } }                       on failure
  //
  // The renderer flips Zustand `bootstrapped` to true only on bootstrap:done
  // so failure modes 1-3 (spawn failure, post-PORT app-init crash, slow cold
  // start) stay in the wizard error card instead of leaking to the chat UI.
  ipcMain.handle("bootstrap:start", async () => {
    const send = sendToRenderer;
    const sourceDir = getResourceDir("sidecar");
    const userDataDir = app.getPath("userData");
    const sidecarLogPath = join(userDataDir, "sidecar.log");
    bootstrapLog(`bootstrap:start invoked at ${new Date().toISOString()}\n`);

    // ── Step 1: Miniconda ──
    if (existsSync(getMinicondaPython())) {
      bootstrapLog("step1: miniconda already installed, skipping\n");
      send("bootstrap:progress", { step: 1, pct: 100, phase: "skipped" });
    } else {
      send("bootstrap:progress", { step: 1, pct: 0, phase: "download" });
      try {
        await downloadAndInstallMiniconda(
          join(getBinRoot(), "miniconda"),
          (pct, phase) => {
            send("bootstrap:progress", { step: 1, pct, phase });
            if (pct % 25 === 0) {
              bootstrapLog(`step1 ${phase}: ${pct}%\n`);
            }
          },
        );
      } catch (err) {
        return sendBootstrapError(send, 1, err, sidecarLogPath);
      }
      send("bootstrap:progress", { step: 1, pct: 100 });
    }

    // ── Step 2: Sidecar venv ──
    if (existsSync(getSidecarEntryPoint())) {
      bootstrapLog("step2: sidecar-venv entry point exists, skipping\n");
      send("bootstrap:progress", { step: 2, pct: 100, phase: "skipped" });
    } else {
      send("bootstrap:progress", { step: 2, pct: 0, phase: "venv" });
      try {
        await createSidecarVenv(sourceDir, (subPct, phase, message) => {
          // Interpolate the three sub-phases (venv / pip-upgrade / pip)
          // into step 2's 0..99 range; pip dominates so it gets half.
          let mapped = 0;
          if (phase === "venv") mapped = Math.floor(subPct * 0.33);
          else if (phase === "pip-upgrade") mapped = 33 + Math.floor(subPct * 0.17);
          else mapped = 50 + Math.floor(subPct * 0.49);
          // subPct < 0 means "indeterminate" (pip's --quiet fallback);
          // forward as-is so the wizard can show a spinner.
          send("bootstrap:progress", {
            step: 2,
            pct: subPct < 0 ? subPct : mapped,
            phase,
            message,
          });
          bootstrapLog(
            `step2 ${phase}: ${subPct}% (mapped=${mapped})${message ? ` — ${message}` : ""}\n`,
          );
        });
      } catch (err) {
        return sendBootstrapError(send, 2, err, sidecarLogPath);
      }
      send("bootstrap:progress", { step: 2, pct: 100 });
    }

    // ── Step 3: Boot sidecar + waitForSidecarReady ──
    send("bootstrap:progress", { step: 3, pct: 50, phase: "sidecar-boot" });
    bootstrapLog("step3: starting sidecar and waiting for /health 200\n");
    try {
      const mgr = startSidecar(userDataDir);
      await waitForSidecarReady(mgr, 30_000);
    } catch (err) {
      return sendBootstrapError(send, 3, err, sidecarLogPath);
    }

    // ── Success ──
    bootstrappedCache = true;
    send("bootstrap:progress", { step: 3, pct: 100 });
    send("bootstrap:done");
    bootstrapLog(`bootstrap:done at ${new Date().toISOString()}\n`);
    return { ok: true };
  });

  // Recursive delete of <userData>/bin so the user can wipe a half-installed
  // tree and start over. Force-resets the cached isBootstrapped flag too.
  ipcMain.handle("bootstrap:reset-bin", async () => {
    const binRoot = getBinRoot();
    bootstrapLog(`bootstrap:reset-bin → rm -rf ${binRoot}\n`);
    // Stop the sidecar first if it's running — otherwise file locks on
    // Windows will keep the entry-point .exe pinned and the rm will fail.
    if (sidecar) {
      try {
        await sidecar.stop();
      } catch {
        /* best-effort */
      }
      sidecar = null;
    }
    await rm(binRoot, { recursive: true, force: true });
    bootstrappedCache = false;
    return { ok: true, removed: binRoot };
  });

  // Opens the userData dir in the OS file explorer. Bootstrap.log and
  // sidecar.log both live there, so the user can hand them off to support
  // without having to navigate APPDATA themselves.
  ipcMain.handle("bootstrap:open-logs", async () => {
    const userDataDir = app.getPath("userData");
    await shell.openPath(userDataDir);
    return { ok: true, path: userDataDir };
  });

  ipcMain.handle(
    "update:install-now",
    rateLimit("update:install-now", 1, async () => {
      // The user already confirmed by clicking "Restart now" in the
      // UpdateBanner — no native dialog here, just shut down cleanly
      // and let electron-updater swap the binaries. Stop the sidecar
      // BEFORE quitAndInstall: NSIS on Windows can't replace files that
      // are still open (server.exe), so an active sidecar turns the
      // install into a "file in use" failure. before-quit sees
      // sidecar=null afterwards and skips its own teardown.
      if (sidecar) {
        try {
          await sidecar.stop();
        } catch {
          /* best-effort */
        }
        sidecar = null;
      }
      autoUpdater.quitAndInstall(false, true);
      return { ok: true };
    }),
  );
}

interface SidecarHttpInfo {
  port: number;
  token: string;
}

type UpdateMechanism = "off" | "auto" | "manual";

async function fetchUpdateMechanism(info: SidecarHttpInfo): Promise<UpdateMechanism> {
  // Pulled in main rather than via wireSidecarAuthHeader: that hook only
  // augments the renderer session, not main-process fetch. Keep this
  // best-effort — a hung sidecar must not block update polling forever,
  // so a 2s timeout returns the "auto" fallback instead.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2_000);
  try {
    const res = await fetch(
      `http://127.0.0.1:${info.port}/api/settings/get?key=update_mechanism`,
      {
        headers: { Authorization: `Bearer ${info.token}` },
        signal: controller.signal,
      },
    );
    if (!res.ok) return "auto";
    const body = (await res.json()) as { value?: unknown };
    const v = body.value;
    if (v === "off" || v === "auto" || v === "manual") return v;
    return "auto";
  } catch {
    return "auto";
  } finally {
    clearTimeout(timer);
  }
}

// Constructed once and reused for every "update-available" payload. The
// publish target in electron-builder.yml is the source of truth; this
// constant must mirror it. Keep in sync if the publish section ever moves.
const RELEASE_NOTES_BASE = "https://github.com/zasonic/AltoSymbiosisAgents/releases/tag";
// List, not /latest, so we pick up pre-releases too (the in-app banner is
// the only update channel during a -test.N phase). per_page kept small to
// stay well inside the 60-req/hr unauthenticated rate limit.
const RELEASES_LIST_API = "https://api.github.com/repos/zasonic/AltoSymbiosisAgents/releases?per_page=10";
const UPDATE_POLL_INTERVAL_MS = 6 * 60 * 60 * 1000;

interface ParsedVersion {
  numeric: [number, number, number];
  prerelease: string | null;
}

function parseVersion(v: string): ParsedVersion | null {
  // Accepts "1.2.3", "v1.2.3", "1.2.3-test.1", etc. The prerelease tail
  // (everything after the first "-") is captured so the compare function
  // can iterate "1.0.0-test.1" → "1.0.0-test.2" correctly.
  const m = /^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$/.exec(v);
  if (!m) return null;
  return {
    numeric: [Number(m[1]), Number(m[2]), Number(m[3])],
    prerelease: m[4] ?? null,
  };
}

function compareVersions(a: string, b: string): number {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  if (!pa || !pb) return 0;
  for (let i = 0; i < 3; i += 1) {
    if (pa.numeric[i] !== pb.numeric[i]) return pa.numeric[i] - pb.numeric[i];
  }
  // SemVer: a version with no prerelease tail outranks any prerelease of the
  // same numeric version. So 1.0.0 > 1.0.0-test.5.
  if (pa.prerelease === null && pb.prerelease !== null) return 1;
  if (pa.prerelease !== null && pb.prerelease === null) return -1;
  if (pa.prerelease === pb.prerelease) return 0;
  const segA = (pa.prerelease as string).split(".");
  const segB = (pb.prerelease as string).split(".");
  for (let i = 0; i < Math.max(segA.length, segB.length); i += 1) {
    if (segA[i] === undefined) return -1;
    if (segB[i] === undefined) return 1;
    const numA = /^\d+$/.test(segA[i]) ? Number(segA[i]) : null;
    const numB = /^\d+$/.test(segB[i]) ? Number(segB[i]) : null;
    if (numA !== null && numB !== null) {
      if (numA !== numB) return numA - numB;
    } else if (segA[i] !== segB[i]) {
      return segA[i] < segB[i] ? -1 : 1;
    }
  }
  return 0;
}

interface GhRelease {
  tag_name?: string;
  draft?: boolean;
  assets?: { name?: string; browser_download_url?: string }[];
}

async function checkManualUpdate(): Promise<void> {
  // Manual mode polls the public GH API directly — no auth needed. A failure
  // here (network, rate limit, GH outage) is silent so an offline run doesn't
  // spam the user with banners.
  try {
    const res = await fetch(RELEASES_LIST_API, {
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!res.ok) return;
    const releases = (await res.json()) as GhRelease[];
    const installed = app.getVersion();
    let best: { tag: string; downloadUrl: string } | null = null;
    for (const rel of releases) {
      if (rel.draft) continue;
      const tag = rel.tag_name ?? "";
      if (!tag) continue;
      if (compareVersions(tag, installed) <= 0) continue;
      const exeAsset = (rel.assets ?? []).find(
        (a) => typeof a.name === "string" && a.name.toLowerCase().endsWith(".exe"),
      );
      const downloadUrl = exeAsset?.browser_download_url;
      if (!downloadUrl) continue;
      if (!best || compareVersions(tag, best.tag) > 0) {
        best = { tag, downloadUrl };
      }
    }
    if (!best) return;
    sendToRenderer("update:available", {
      version: best.tag.replace(/^v/, ""),
      notesUrl: `${RELEASE_NOTES_BASE}/${best.tag}`,
      downloadUrl: best.downloadUrl,
    });
  } catch (err) {
    logToFile(`manual update check failed: ${err instanceof Error ? err.message : err}\n`);
  }
}

async function wireAutoUpdater(): Promise<void> {
  if (!app.isPackaged) return;

  const info = sidecar?.getInfo();
  const mechanism: UpdateMechanism = info ? await fetchUpdateMechanism(info) : "auto";
  if (mechanism === "off") return;

  if (mechanism === "manual") {
    // No electron-updater listeners in manual mode — the manual path doesn't
    // download anything in-process. checkManualUpdate emits update:available
    // with a downloadUrl, and the UpdateBanner switches to "Download" mode.
    checkManualUpdate().catch(() => {});
    updaterTimer = setInterval(() => {
      checkManualUpdate().catch(() => {});
    }, UPDATE_POLL_INTERVAL_MS);
    return;
  }

  // mechanism === "auto"
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  // Match the manual path: pick up pre-releases too. Without this,
  // electron-updater skips any tag with a "-test.N" suffix even when
  // it's the newest non-draft release.
  autoUpdater.allowPrerelease = true;

  autoUpdater.on("update-available", (autoInfo) => {
    sendToRenderer("update:available", {
      version: autoInfo.version,
      notesUrl: `${RELEASE_NOTES_BASE}/v${autoInfo.version}`,
    });
  });
  autoUpdater.on("update-downloaded", (autoInfo) => {
    sendToRenderer("update:downloaded", { version: autoInfo.version });
  });
  autoUpdater.on("error", (err) => {
    // Log only — surfacing this via a dialog would break the
    // non-blocking guarantee. The user simply doesn't see a banner
    // when the check fails, and the next 6h tick retries.
    logToFile(`autoUpdater error: ${err.message}\n`);
  });

  // Check on launch and every 6 hours. Hold the interval handle so we can
  // clear it on quit (otherwise it keeps a reference to autoUpdater alive
  // and prevents clean process exit on some Electron versions).
  autoUpdater.checkForUpdatesAndNotify().catch(() => {});
  updaterTimer = setInterval(() => {
    autoUpdater.checkForUpdatesAndNotify().catch(() => {});
  }, UPDATE_POLL_INTERVAL_MS);
}

function startSidecar(userDataDir: string): SidecarManager {
  // Create + wire the SidecarManager and kick off `start()` in the
  // background. Callers consume readiness via the status event (the
  // bootstrap:start handler uses waitForSidecarReady; whenReady's
  // bootSidecar wrapper awaits the same way). Splitting "spawn" from
  // "wait" lets bootstrap:start observe status transitions without
  // double-awaiting .start().
  sidecar = new SidecarManager(PROJECT_ROOT, userDataDir);
  sidecar.on("status", (status) => {
    sendToRenderer("sidecar:status", status);
    logToFile(`sidecar status: ${JSON.stringify(status)}\n`);
  });
  sidecar.start().catch((err) => {
    logToFile(`sidecar.start rejected: ${err instanceof Error ? err.message : err}\n`);
    // Status: crashed has already fired from inside SidecarManager.
  });
  return sidecar;
}

async function bootSidecar(userDataDir: string): Promise<void> {
  // Used by app.whenReady() once the bin/ tree is in place. Wait up to 30s
  // for ready so the chat UI doesn't render before the sidecar is alive;
  // on failure, the existing renderer status handler shows the error UI.
  const mgr = startSidecar(userDataDir);
  try {
    await waitForSidecarReady(mgr, 30_000);
  } catch (err) {
    logToFile(`bootSidecar: ${err instanceof Error ? err.message : err}\n`);
  }
}

app.whenReady().then(async () => {
  // Single-instance lock so multiple launches reuse the existing window.
  const got = app.requestSingleInstanceLock();
  if (!got) {
    app.quit();
    return;
  }
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  // Strip the default menu in production; keep DevTools accessible in dev.
  if (app.isPackaged) Menu.setApplicationMenu(null);

  const userDataDir = app.getPath("userData");
  bootMainLog(userDataDir);
  bootBootstrapLog(userDataDir);

  // Compute bootstrap readiness BEFORE opening the window so the renderer's
  // first paint can already gate on the cached boolean — avoids a flash of
  // the chat UI behind the BootstrapWizard overlay.
  bootstrappedCache = await isBootstrapped();
  logToFile(`bootstrap: isBootstrapped() = ${bootstrappedCache}\n`);

  wireIpc();
  await createWindow();
  // The sidecar only boots when the venv is in place. On a fresh install the
  // BootstrapWizard takes over, runs the install flow, and calls bootSidecar
  // itself once the venv is ready (commit 4 wires that path).
  if (bootstrappedCache) {
    await bootSidecar(userDataDir);
  }
  await wireAutoUpdater();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  // On macOS the app conventionally stays alive with no windows; the dock
  // icon's "activate" event will reopen one. Keep the sidecar and log stream
  // running so reopening doesn't land on a dead backend. before-quit handles
  // teardown when the user actually quits.
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", async (event) => {
  if (updaterTimer) {
    clearInterval(updaterTimer);
    updaterTimer = null;
  }
  if (sidecar) {
    event.preventDefault();
    try {
      await sidecar.stop();
    } catch {
      /* ignore */
    }
    sidecar = null;
    mainLogStream?.end();
    mainLogStream = null;
    bootstrapLogStream?.end();
    bootstrapLogStream = null;
    app.exit(0);
    return;
  }
  // Sidecar already torn down (e.g. via update:install-now). Still flush
  // the log streams so the final lines from this session aren't truncated.
  mainLogStream?.end();
  mainLogStream = null;
  bootstrapLogStream?.end();
  bootstrapLogStream = null;
});
