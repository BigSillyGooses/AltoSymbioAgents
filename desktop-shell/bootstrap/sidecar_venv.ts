// desktop-shell/bootstrap/sidecar_venv.ts — builds the per-app Python venv that
// the FastAPI sidecar runs in. Layered on top of miniconda.ts:
//
//   1. Miniconda is already installed at <binRoot>/miniconda/python.exe
//      (commit 2 handles step 1).
//   2. createSidecarVenv() drives Miniconda's python to:
//        a. `python -m venv <binRoot>/sidecar-venv`
//        b. `<sidecar-venv>/Scripts/python.exe -m pip install --upgrade pip`
//        c. `<sidecar-venv>/Scripts/python.exe -m pip install [-e] <sourceDir>`
//           — non-editable in packaged mode (process.resourcesPath is read-
//           only for non-admin processes); editable in dev for fast iteration.
//   3. After step 2c finishes pip generates Scripts/altosymbiosis-server.exe
//      from backend/pyproject.toml's [project.scripts] entry. That .exe is
//      what desktop-shell/sidecar.ts spawns at runtime.
//
// Progress parsing: pip --progress-bar=raw emits machine-readable lines like
// `Progress 1234/56789`; the parser maps those to a 0..99 percentage. If the
// running pip is too old to support `raw` the install is retried in
// --quiet mode and the UI falls back to an indeterminate spinner driven by
// `Collecting <pkg>` lines from stderr.
//
// All three steps throw labeled error classes (VenvCreateError /
// PipUpgradeError / PipInstallError) so commit 4's wizard can map each to a
// recovery action.

import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";

import { getBinRoot, getSidecarPython } from "./bin_manager";

type Phase = "venv" | "pip-upgrade" | "pip";
type ProgressCallback = (pct: number, phase: Phase, message?: string) => void;

export class VenvCreateError extends Error {
  readonly label = "VenvCreateError";
  constructor(
    public readonly code: number | null,
    public readonly signal: NodeJS.Signals | null,
    public readonly stderr: string,
  ) {
    super(`venv creation failed (code=${code} signal=${signal})`);
    this.name = "VenvCreateError";
  }
}

export class PipUpgradeError extends Error {
  readonly label = "PipUpgradeError";
  constructor(
    public readonly code: number | null,
    public readonly signal: NodeJS.Signals | null,
    public readonly stderr: string,
  ) {
    super(`pip self-upgrade failed (code=${code} signal=${signal})`);
    this.name = "PipUpgradeError";
  }
}

export class PipInstallError extends Error {
  readonly label = "PipInstallError";
  constructor(
    public readonly code: number | null,
    public readonly signal: NodeJS.Signals | null,
    public readonly stderr: string,
  ) {
    super(`pip install of sidecar package failed (code=${code} signal=${signal})`);
    this.name = "PipInstallError";
  }
}

interface CreateSidecarVenvOptions {
  /** False (default) ⇒ editable=true in dev, false in packaged. True/false override for tests. */
  editable?: boolean;
  /** Override the source dir picker (test only). */
  sourceDir?: string;
}

/**
 * Build the sidecar venv from a previously-installed Miniconda interpreter.
 * Resolves when `pip install` succeeds; throws Venv/PipUpgrade/PipInstallError
 * on any sub-step failure with stderr captured for the wizard error card.
 *
 * `sourceDir` should be the absolute path to the sidecar source tree
 * (backend/ in dev; <resourcesPath>/sidecar in packaged).
 */
export async function createSidecarVenv(
  sourceDir: string,
  onProgress: ProgressCallback,
  opts: CreateSidecarVenvOptions = {},
): Promise<void> {
  const binRoot = getBinRoot();
  const minicondaPython = join(binRoot, "miniconda", "python.exe");
  const venvDir = join(binRoot, "sidecar-venv");
  const venvPython = getSidecarPython();

  if (!existsSync(dirname(venvDir))) mkdirSync(dirname(venvDir), { recursive: true });

  // ── Step 1: create the venv with Miniconda's python -m venv ──────────────
  onProgress(0, "venv");
  await runProc(
    minicondaPython,
    ["-m", "venv", venvDir],
    (code, signal, stderr) => new VenvCreateError(code, signal, stderr),
    (line, totalLines) => {
      // venv creation prints little output — fake linear progress by line count.
      onProgress(Math.min(99, totalLines * 20), "venv", line);
    },
  );
  onProgress(100, "venv");

  // ── Step 2: upgrade pip (cheap insurance against an old pip in Miniconda) ─
  onProgress(0, "pip-upgrade");
  await runProc(
    venvPython,
    ["-m", "pip", "install", "--no-cache-dir", "--upgrade", "pip"],
    (code, signal, stderr) => new PipUpgradeError(code, signal, stderr),
    (line) => onProgress(50, "pip-upgrade", line),
  );
  onProgress(100, "pip-upgrade");

  // ── Step 3: install the sidecar package from the source tree ─────────────
  const editable = opts.editable ?? wantsEditable();
  await runPipInstall(venvPython, sourceDir, editable, onProgress);
}

function wantsEditable(): boolean {
  // Dynamic import would slow startup; defer the electron app reference to
  // avoid pulling it in when sidecar_venv.ts is imported from a test that
  // doesn't have Electron globals.
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const electron = require("electron") as { app?: { isPackaged?: boolean } };
    return electron.app?.isPackaged === false;
  } catch {
    // Outside an Electron process (e.g., vitest) — default to editable for
    // the assumed dev scenario. Tests override via opts.editable.
    return true;
  }
}

async function runPipInstall(
  venvPython: string,
  sourceDir: string,
  editable: boolean,
  onProgress: ProgressCallback,
): Promise<void> {
  onProgress(0, "pip");
  const baseArgs = ["-m", "pip", "install", "--no-cache-dir"];
  const tail = editable ? ["-e", sourceDir] : [sourceDir];

  // Primary path: --progress-bar=raw for machine-readable progress.
  try {
    await runProc(
      venvPython,
      [...baseArgs, "--progress-bar=raw", ...tail],
      (code, signal, stderr) => new PipInstallError(code, signal, stderr),
      (line) => {
        const parsed = parseRawProgress(line);
        if (parsed) {
          onProgress(parsed.pct, "pip");
        } else if (line.startsWith("Collecting ")) {
          onProgress(-1, "pip", line.trim());
        }
      },
    );
    onProgress(100, "pip");
    return;
  } catch (err) {
    if (!(err instanceof PipInstallError)) throw err;
    // If pip is old enough that --progress-bar=raw is unrecognized, the run
    // exits non-zero before installing anything. Detect that and retry with
    // --quiet + an indeterminate spinner via "Collecting" lines.
    if (!/unrecognized arguments?: --progress-bar=raw|no such option: --progress-bar=raw/i.test(err.stderr)) {
      throw err;
    }
  }

  // Fallback: --quiet + indeterminate spinner driven by "Collecting" lines.
  await runProc(
    venvPython,
    [...baseArgs, "--quiet", ...tail],
    (code, signal, stderr) => new PipInstallError(code, signal, stderr),
    (line) => {
      if (line.startsWith("Collecting ")) {
        onProgress(-1, "pip", line.trim());
      }
    },
  );
  onProgress(100, "pip");
}

/**
 * Parse a single line of pip's `--progress-bar=raw` output:
 *   "Progress 12345 of 678910"  (modern pip 24+)
 *   "Progress 12345/678910"     (some intermediate variants)
 *   "Progress 50%"              (very old format — pct directly)
 * Returns null if the line isn't a progress event.
 */
function parseRawProgress(line: string): { pct: number } | null {
  const m1 = /^Progress\s+(\d+)\s*(?:of|\/)\s*(\d+)/.exec(line);
  if (m1) {
    const done = Number.parseInt(m1[1], 10);
    const total = Number.parseInt(m1[2], 10);
    if (Number.isFinite(done) && Number.isFinite(total) && total > 0) {
      return { pct: Math.min(99, Math.floor((done / total) * 100)) };
    }
  }
  const m2 = /^Progress\s+(\d+)\s*%/.exec(line);
  if (m2) {
    const pct = Number.parseInt(m2[1], 10);
    if (Number.isFinite(pct)) return { pct: Math.min(99, Math.max(0, pct)) };
  }
  return null;
}

/**
 * Spawn `cmd args`, accumulating stderr for error reporting and forwarding
 * each stdout line to `onLine`. Resolves on exit code 0; rejects with the
 * caller-supplied error factory on non-zero exit or spawn errors.
 */
function runProc(
  cmd: string,
  args: string[],
  toError: (code: number | null, signal: NodeJS.Signals | null, stderr: string) => Error,
  onLine: (line: string, totalLines: number) => void,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawn(cmd, args, { windowsHide: true });
    } catch (err) {
      reject(toError(null, null, err instanceof Error ? err.message : String(err)));
      return;
    }
    let stdoutBuf = "";
    let stderrBuf = "";
    let lineCount = 0;
    child.stdout.on("data", (chunk: Buffer) => {
      stdoutBuf += chunk.toString("utf-8");
      while (true) {
        const nl = stdoutBuf.indexOf("\n");
        if (nl < 0) break;
        const line = stdoutBuf.slice(0, nl).trimEnd();
        stdoutBuf = stdoutBuf.slice(nl + 1);
        lineCount += 1;
        onLine(line, lineCount);
      }
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderrBuf += chunk.toString("utf-8");
      // Cap stderr capture so a flooded sub-process can't pin unbounded memory.
      if (stderrBuf.length > 256 * 1024) {
        stderrBuf = stderrBuf.slice(-256 * 1024);
      }
    });
    child.on("error", (err) => reject(toError(null, null, err.message)));
    child.on("exit", (code, signal) => {
      if (code === 0) resolve();
      else reject(toError(code, signal, stderrBuf));
    });
  });
}
