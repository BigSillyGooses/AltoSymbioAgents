// desktop-shell/bootstrap/bin_manager.ts — paths and readiness checks for the
// Pinokio-style runtime tree at <userData>/bin/. Owns three pieces of state:
//
//   <binRoot>/miniconda/python.exe          — bundled Miniconda interpreter,
//                                              installed by miniconda.ts
//   <binRoot>/sidecar-venv/Scripts/python.exe — the venv built off miniconda
//   <binRoot>/sidecar-venv/Scripts/altosymbiosis-server.exe
//                                              — the entry-point shim that pip
//                                              generates from
//                                              [project.scripts] in
//                                              backend/pyproject.toml. This is
//                                              what sidecar.ts spawns.
//
// `isBootstrapped()` is the single source of truth that main.ts consults
// before deciding to boot the sidecar vs. hand off to BootstrapWizard. The
// smoke test goes beyond an import check: it actually spawns the entry
// point, waits for `PORT=<n>` on stdout, GETs /health, then kills the
// child. Catches the class of bugs where imports succeed but the server
// crashes at bind time.
//
// Windows-only this branch. On non-Windows we fall back to detecting a
// legacy `backend/.venv` (preserves dev workflow on mac/linux). Packaged
// builds are Win-x64 only — see electron-builder.yml — so non-Windows is
// effectively dev-only.

import { app } from "electron";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import type { EventEmitter } from "node:events";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { SidecarStatus } from "../sidecar-types";

// Mirrors PROJECT_ROOT in main.ts. In a packaged build the path resolves to a
// location inside the asar archive that doesn't exist on disk; only the dev
// branch reads it, so the sidecar source resolution is correct in both modes.
const PROJECT_ROOT = app.isPackaged
  ? app.getAppPath()
  : fileURLToPath(new URL("../..", import.meta.url));

// TODO(engines): when the engine-download branch lands, extend isBootstrapped
// with an existsSync check on <binRoot>/engines/llama-server.exe. Tracked at
// the follow-up "engine variant download wizard" issue.

const SMOKE_SPAWN_TIMEOUT_MS = 10_000;
const SMOKE_HEALTH_TIMEOUT_MS = 5_000;

export function getBinRoot(): string {
  return join(app.getPath("userData"), "bin");
}

export function getMinicondaPython(): string {
  // Windows path. Layout on disk after a silent Miniconda install:
  //   <binRoot>/miniconda/python.exe
  //   <binRoot>/miniconda/Scripts/...
  return join(getBinRoot(), "miniconda", "python.exe");
}

export function getSidecarPython(): string {
  // python.exe inside the venv. Used by sidecar_venv.ts to drive pip; NOT
  // the binary that sidecar.ts spawns at runtime — that's the entry point
  // below.
  return join(getBinRoot(), "sidecar-venv", "Scripts", "python.exe");
}

export function getSidecarEntryPoint(): string {
  // The console-script shim pip generates from
  // `[project.scripts] altosymbiosis-server = "server:main"`. Sidecar.ts
  // spawns this directly with `--token ... --user-data ...`.
  return join(getBinRoot(), "sidecar-venv", "Scripts", "altosymbiosis-server.exe");
}

export function getResourceDir(name: string): string {
  // Resolves either a packaged resource directory (process.resourcesPath) or
  // its dev-mode source path under the repo root. Sidecar.ts and
  // sidecar_venv.ts both use this to locate the sidecar Python source.
  if (app.isPackaged) {
    return join(process.resourcesPath, name);
  }
  // In dev the sidecar source lives at <repo>/backend; alias "sidecar" to it
  // so callers don't have to know about the rename. Other names map 1:1 to a
  // sibling of backend/ under the repo root.
  if (name === "sidecar") {
    return join(PROJECT_ROOT, "backend");
  }
  return join(PROJECT_ROOT, name);
}

export async function isBootstrapped(): Promise<boolean> {
  if (process.platform !== "win32") {
    // Non-Windows dev fallback: if a legacy backend/.venv exists at the repo
    // root, treat the app as bootstrapped so the existing dev flow keeps
    // working. macOS / Linux installer support arrives in a future branch.
    return existsSync(join(getResourceDir("sidecar"), ".venv"));
  }

  if (!existsSync(getMinicondaPython())) return false;
  if (!existsSync(getSidecarEntryPoint())) return false;

  return runSidecarSmokeTest();
}

export class SidecarBootError extends Error {
  readonly label = "SidecarBootError";
  constructor(
    public readonly cause: string,
    public readonly logPath?: string,
  ) {
    super(`Sidecar failed to reach ready: ${cause}`);
    this.name = "SidecarBootError";
  }
}

/**
 * Subscribe to a SidecarManager's status event and resolve when the sidecar
 * reaches `ready`, or reject with a SidecarBootError on `crashed` or
 * timeout. Used by the bootstrap:start IPC handler to gate the wizard's
 * final `bootstrap:done` event on the sidecar actually serving /health —
 * without this, three real failure modes leak into the chat UI instead of
 * staying in the wizard error card:
 *   1. Sidecar spawn fails (port collision, missing entry point).
 *   2. Sidecar starts uvicorn but app init throws before /health
 *      (sqlite-vec load error, anything past PORT= but before READY).
 *   3. Slow first cold start exceeds whatever poll the chat UI tolerates.
 *
 * `timeoutMs` defaults to 30s — enough for a cold first boot on slow disks
 * while still surfacing genuine hangs to the user. Pass the existing
 * SidecarManager (it extends EventEmitter) as the first arg.
 */
export function waitForSidecarReady(
  sidecar: EventEmitter,
  timeoutMs = 30_000,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let settled = false;
    const onStatus = (s: SidecarStatus): void => {
      if (settled) return;
      if (s.status === "ready") {
        settled = true;
        sidecar.off("status", onStatus);
        clearTimeout(timer);
        resolve();
      } else if (s.status === "crashed") {
        settled = true;
        sidecar.off("status", onStatus);
        clearTimeout(timer);
        reject(
          new SidecarBootError(
            s.error ?? `crashed (code=${s.code}, signal=${s.signal})`,
          ),
        );
      }
      // 'starting' is the expected interim state; 'stopped' only fires on
      // explicit shutdown which doesn't happen during bootstrap:start.
    };
    sidecar.on("status", onStatus);
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      sidecar.off("status", onStatus);
      reject(new SidecarBootError(`timeout after ${timeoutMs}ms`));
    }, timeoutMs);
  });
}

async function runSidecarSmokeTest(): Promise<boolean> {
  // Spawn the entry point with throwaway --token / --user-data, wait for
  // `PORT=<n>` on stdout, GET /health, then kill the child. Resolves true
  // iff /health returned 200 within the budgeted window.
  return new Promise<boolean>((resolve) => {
    const token = randomUUID();
    let child: ReturnType<typeof spawn> | null = null;
    let resolved = false;
    let spawnTimer: NodeJS.Timeout | null = null;
    let stdoutBuf = "";

    const finish = (ok: boolean): void => {
      if (resolved) return;
      resolved = true;
      if (spawnTimer) clearTimeout(spawnTimer);
      if (child && !child.killed) {
        try {
          // taskkill /f /t is the only reliable way to reap a Python child
          // process tree on Windows — child.kill() alone leaves orphaned
          // uvicorn workers.
          spawn("taskkill", ["/f", "/t", "/pid", String(child.pid)], {
            windowsHide: true,
          });
        } catch {
          /* best-effort */
        }
      }
      resolve(ok);
    };

    try {
      child = spawn(
        getSidecarEntryPoint(),
        ["--token", token, "--user-data", tmpdir()],
        { windowsHide: true },
      );
    } catch {
      finish(false);
      return;
    }

    spawnTimer = setTimeout(() => finish(false), SMOKE_SPAWN_TIMEOUT_MS);

    child.on("error", () => finish(false));
    child.on("exit", () => {
      // If the child exits before /health succeeds, mark as failed. (If
      // it exits AFTER finish(true), `resolved` short-circuits.)
      finish(false);
    });

    child.stdout?.on("data", async (chunk: Buffer) => {
      stdoutBuf += chunk.toString("utf-8");
      const newlineIdx = stdoutBuf.indexOf("\n");
      if (newlineIdx < 0) return;
      const portLine = stdoutBuf.slice(0, stdoutBuf.indexOf("\n")).trim();
      // Drain anything before the port line; we only care about the first.
      stdoutBuf = stdoutBuf.slice(newlineIdx + 1);
      if (!portLine.startsWith("PORT=")) return;

      const port = Number.parseInt(portLine.slice(5), 10);
      if (!Number.isFinite(port) || port <= 0 || port >= 65536) {
        finish(false);
        return;
      }

      try {
        const controller = new AbortController();
        const healthTimer = setTimeout(
          () => controller.abort(),
          SMOKE_HEALTH_TIMEOUT_MS,
        );
        try {
          const res = await fetch(`http://127.0.0.1:${port}/health`, {
            headers: { Authorization: `Bearer ${token}` },
            signal: controller.signal,
          });
          finish(res.ok);
        } finally {
          clearTimeout(healthTimer);
        }
      } catch {
        finish(false);
      }
    });
  });
}
