// desktop-shell/preload.ts — typed bridge between renderer (sandboxed) and main.
//
// Only the methods enumerated below are exposed; everything else (Node, fs,
// child_process) stays on the main process side. JSON-only IPC; never raw
// Node objects across the boundary.

import { contextBridge, ipcRenderer } from "electron";

import type { SidecarStatus } from "./sidecar-types";

/**
 * Payload of the `bootstrap:progress` event. Carries either a progress tick
 * (pct + optional phase/message) or an error sentinel that the wizard
 * renders as the labeled error card with Retry / Reset bin / Open log
 * folder actions. Mirrored in desktop-ui/env.d.ts so the renderer's
 * tsconfig.web.json doesn't import this file.
 */
export type BootstrapProgressEvent =
  | { step: number; pct: number; phase?: string; message?: string; error?: undefined }
  | { step: number; pct?: undefined; error: { label: string; cause: string; logPath: string } };

export interface SidecarInfo {
  port: number;
  token: string;
}

export interface ElectronAPI {
  /** Returns null until the sidecar reports ready. */
  getSidecarInfo: () => Promise<SidecarInfo | null>;
  /** Force a clean restart of the sidecar (kill + respawn). */
  restartSidecar: () => Promise<SidecarInfo>;
  /** Show a native folder picker; returns the chosen absolute path or null. */
  selectFolder: () => Promise<string | null>;
  /**
   * Show a native folder picker for the Power Mode workspace folder. Returns
   * the chosen absolute path or null if the user cancelled. Reuses the same
   * Electron dialog handler as selectFolder() — the separate name lets the
   * renderer make the intent obvious in Settings → Power Mode.
   */
  selectWorkspaceFolder: () => Promise<string | null>;
  /** Show a native multi-file picker; returns absolute paths. */
  selectFiles: (filters?: { name: string; extensions: string[] }[]) => Promise<string[]>;
  /** Write `content` to a path chosen via a native save dialog. */
  saveFileDialog: (suggestedName: string, content: string) => Promise<{
    ok: boolean;
    path?: string;
    cancelled?: boolean;
    error?: string;
  }>;
  /**
   * Render `html` to a PDF in a hidden BrowserWindow via Electron's
   * webContents.printToPDF, then prompt the user with a native Save dialog
   * pre-filled to `suggestedName`. Used by the conversation export menu —
   * see backend route GET /api/chat/conversations/{id}/export.pdf-html.
   */
  exportPdf: (html: string, suggestedName: string) => Promise<{
    ok: boolean;
    path?: string;
    cancelled?: boolean;
    error?: string;
  }>;
  /** Open a URL in the user's default browser. http(s) only. */
  openExternal: (url: string) => Promise<void>;

  /** App version reported by Electron's `app.getVersion()`. */
  getAppVersion: () => Promise<string>;
  /** Path of the Electron userData dir (where logs/db/settings live). */
  getUserDataPath: () => Promise<string>;

  /**
   * Cached bootstrap readiness — true iff the Miniconda + sidecar-venv
   * tree exists at <userData>/bin and the sidecar smoke test passed. The
   * renderer reads this on first paint to decide between BootstrapWizard
   * (false) and the main UI (true). The value is computed once in
   * app.whenReady and cached for the session.
   */
  isBootstrapped: () => Promise<boolean>;
  /**
   * Force a fresh check of the bootstrap state. Used by BootstrapWizard
   * after a successful install (commit 4) and on remount to recover from
   * a stale cached `false`.
   */
  recheckBootstrap: () => Promise<boolean>;
  /**
   * NodeJS.Platform ("win32" | "darwin" | "linux" | ...) from the main
   * process. BootstrapWizard branches its copy on this so non-Windows dev
   * users see the "macOS / Linux coming later" message instead of a
   * Windows-only install flow.
   */
  getPlatform: () => Promise<NodeJS.Platform>;
  /**
   * Drive the end-to-end bootstrap install (Miniconda → sidecar venv → boot
   * sidecar). Phases that have already completed are skipped on Retry so a
   * sidecar-boot failure doesn't trigger a fresh 600 MB download. Progress
   * streams via `onBootstrapProgress`; success ends with an
   * `onBootstrapDone` event. Resolves to `{ ok: true }` on success or
   * `{ ok: false, error }` if any step labeled-errored.
   */
  startBootstrap: () => Promise<
    | { ok: true }
    | { ok: false; error: { label: string; cause: string } }
  >;
  /**
   * Subscribe to per-phase progress events while a bootstrap:start is in
   * flight. Payload is either a progress update or an error sentinel; the
   * wizard branches on `error` presence. Returns an unsubscribe fn.
   */
  onBootstrapProgress: (
    handler: (event: BootstrapProgressEvent) => void,
  ) => () => void;
  /** Fires once when bootstrap:start has reached a healthy sidecar. */
  onBootstrapDone: (handler: () => void) => () => void;
  /**
   * Recursively delete <userData>/bin so a half-installed tree can be wiped.
   * Tears down the running sidecar first (Windows file-locks the entry
   * point .exe). Recoverable user action from the wizard error card.
   */
  resetBin: () => Promise<{ ok: true; removed: string }>;
  /** Open the userData dir in the OS file explorer (Finder / Explorer). */
  openBootstrapLogs: () => Promise<{ ok: true; path: string }>;

  /** Subscribe to sidecar lifecycle changes; returns an unsubscribe fn. */
  onSidecarStatus: (handler: (status: SidecarStatus) => void) => () => void;
  /**
   * Subscribe to "update available" notifications. In auto mode the payload
   * carries {version, notesUrl}. In manual mode (update_mechanism="manual")
   * the payload also includes {downloadUrl}, signalling the UpdateBanner to
   * render a "Download" button that opens the GH releases page instead of
   * a "Restart now" button that triggers an in-place install.
   */
  onUpdateAvailable: (
    handler: (info: { version: string; notesUrl?: string; downloadUrl?: string }) => void,
  ) => () => void;
  /** Subscribe to "update downloaded" notifications. */
  onUpdateDownloaded: (handler: (info: { version: string }) => void) => () => void;
  /** Restart and apply a downloaded update. */
  installUpdate: () => Promise<void>;
}

const api: ElectronAPI = {
  getSidecarInfo: () => ipcRenderer.invoke("sidecar:get-info"),
  restartSidecar: () => ipcRenderer.invoke("sidecar:restart"),

  selectFolder: () => ipcRenderer.invoke("dialog:select-folder"),
  selectWorkspaceFolder: () => ipcRenderer.invoke("dialog:select-folder"),
  selectFiles: (filters) => ipcRenderer.invoke("dialog:select-files", filters),
  saveFileDialog: (suggestedName, content) =>
    ipcRenderer.invoke("dialog:save-file", { suggestedName, content }),
  exportPdf: (html, suggestedName) =>
    ipcRenderer.invoke("export:pdf", { html, suggestedName }),
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", url),

  getAppVersion: () => ipcRenderer.invoke("app:version"),
  getUserDataPath: () => ipcRenderer.invoke("app:user-data-path"),

  isBootstrapped: () => ipcRenderer.invoke("app:is-bootstrapped"),
  recheckBootstrap: () => ipcRenderer.invoke("app:recheck-bootstrap"),
  getPlatform: () => ipcRenderer.invoke("app:platform"),
  startBootstrap: () => ipcRenderer.invoke("bootstrap:start"),
  onBootstrapProgress: (handler) => {
    const wrapped = (_e: Electron.IpcRendererEvent, event: BootstrapProgressEvent) =>
      handler(event);
    ipcRenderer.on("bootstrap:progress", wrapped);
    return () => ipcRenderer.removeListener("bootstrap:progress", wrapped);
  },
  onBootstrapDone: (handler) => {
    const wrapped = () => handler();
    ipcRenderer.on("bootstrap:done", wrapped);
    return () => ipcRenderer.removeListener("bootstrap:done", wrapped);
  },
  resetBin: () => ipcRenderer.invoke("bootstrap:reset-bin"),
  openBootstrapLogs: () => ipcRenderer.invoke("bootstrap:open-logs"),

  onSidecarStatus: (handler) => {
    const wrapped = (_e: Electron.IpcRendererEvent, status: SidecarStatus) => handler(status);
    ipcRenderer.on("sidecar:status", wrapped);
    return () => ipcRenderer.removeListener("sidecar:status", wrapped);
  },
  onUpdateAvailable: (handler) => {
    const wrapped = (
      _e: Electron.IpcRendererEvent,
      info: { version: string; notesUrl?: string; downloadUrl?: string },
    ) => handler(info);
    ipcRenderer.on("update:available", wrapped);
    return () => ipcRenderer.removeListener("update:available", wrapped);
  },
  onUpdateDownloaded: (handler) => {
    const wrapped = (_e: Electron.IpcRendererEvent, info: { version: string }) => handler(info);
    ipcRenderer.on("update:downloaded", wrapped);
    return () => ipcRenderer.removeListener("update:downloaded", wrapped);
  },
  installUpdate: () => ipcRenderer.invoke("update:install-now"),
};

contextBridge.exposeInMainWorld("electronAPI", api);
