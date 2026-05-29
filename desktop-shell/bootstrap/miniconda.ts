// desktop-shell/bootstrap/miniconda.ts — downloads + silent-installs Miniconda
// into <userData>/bin/miniconda on first run. Owns three pieces of work:
//
//   1. Resumable HTTPS download to <tmp>/altosymbiosis-miniconda-installer.exe.part
//      (Range: bytes=N- when a partial file is present, falls back to a clean
//      re-download if the server returns 200 instead of 206 Partial Content).
//   2. SHA256 verification against the pinned hash for the URL constant below.
//      A mismatch deletes the partial file and throws ChecksumMismatch so the
//      UI can offer "Retry / Reset bin / Open log folder" (commit 4).
//   3. Silent NSIS install with `/S /D=<targetDir>`. `/D=...` MUST be the last
//      argument and unquoted per Miniconda's silent-install docs:
//      https://docs.anaconda.com/free/miniconda/miniconda-install/
//      Because /D's path can contain spaces and unquoted spaces would
//      otherwise be split by the default Windows argv escaper, spawn() is
//      invoked with `windowsVerbatimArguments: true` so Electron passes the
//      command line through verbatim.
//
// All three throw labeled error classes (DownloadError / ChecksumMismatch /
// InstallerExitNonZero) carrying `.label` strings — commit 4 will map those
// to UI error cards with retry actions.
//
// Network and child_process are imported as plain node: specifiers so vitest
// can swap them in tests via `vi.mock("node:https", ...)`.

import { spawn } from "node:child_process";
import { createHash, type Hash } from "node:crypto";
import {
  createReadStream,
  createWriteStream,
  existsSync,
  mkdirSync,
  renameSync,
  statSync,
  unlinkSync,
  type WriteStream,
} from "node:fs";
import { get as httpsGet, type RequestOptions } from "node:https";
import type { IncomingMessage } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

// Source: https://repo.anaconda.com/miniconda/ (HTML listing). Hash captured
// from the row "Miniconda3-py312_26.3.2-2-Windows-x86_64.exe" (size 93.5M,
// last-modified 2026-04-28). Re-verified from
// https://repo.anaconda.com/miniconda/ on 2026-05-13. Re-verify by fetching
// the listing and grepping for the filename; if the row's SHA256 has changed,
// Anaconda has re-rolled the artifact and this constant + the URL's version
// pin must be bumped together.
export const MINICONDA_URL =
  "https://repo.anaconda.com/miniconda/Miniconda3-py312_26.3.2-2-Windows-x86_64.exe";
export const MINICONDA_SHA256 =
  "a6640d1602392aeb5e1a3436f1825069ae7ed8f6f3e9ab14629fd20dec20a7b1";

const INSTALL_POLL_INTERVAL_MS = 500;
const INSTALL_POLL_MAX_MS = 10 * 60_000; // 10 min wall-clock cap

type Phase = "download" | "install";
type ProgressCallback = (pct: number, phase: Phase) => void;

export class DownloadError extends Error {
  readonly label = "DownloadError";
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = "DownloadError";
  }
}

export class ChecksumMismatch extends Error {
  readonly label = "ChecksumMismatch";
  constructor(public readonly expected: string, public readonly actual: string) {
    super(`SHA256 mismatch: expected ${expected}, got ${actual}`);
    this.name = "ChecksumMismatch";
  }
}

export class InstallerExitNonZero extends Error {
  readonly label = "InstallerExitNonZero";
  constructor(
    public readonly code: number | null,
    public readonly signal: NodeJS.Signals | null,
  ) {
    super(`Miniconda installer exited with code=${code} signal=${signal}`);
    this.name = "InstallerExitNonZero";
  }
}

interface DownloadOptions {
  /** Override the download URL (test-only). */
  url?: string;
  /** Override the expected SHA256 (test-only). */
  sha256?: string;
  /** Override the partial-file path (test-only). */
  partPath?: string;
}

/**
 * Download Miniconda (resumable, SHA256-verified) and silent-install it into
 * `targetDir`. `onProgress` fires throughout both phases; tests can pass an
 * empty `() => {}` callback. Throws DownloadError / ChecksumMismatch /
 * InstallerExitNonZero on failure paths.
 */
export async function downloadAndInstallMiniconda(
  targetDir: string,
  onProgress: ProgressCallback,
  opts: DownloadOptions = {},
): Promise<void> {
  const url = opts.url ?? MINICONDA_URL;
  const expectedSha = opts.sha256 ?? MINICONDA_SHA256;
  const partPath =
    opts.partPath ?? join(tmpdir(), "altosymbiosis-miniconda-installer.exe.part");
  const installerPath = partPath.replace(/\.part$/, "");

  // Ensure the parent dir of the installer exists; some test environments
  // strip tmpdir() between runs.
  const partParent = dirname(partPath);
  if (!existsSync(partParent)) mkdirSync(partParent, { recursive: true });

  await downloadWithResume(url, partPath, expectedSha, (pct) =>
    onProgress(pct, "download"),
  );

  try {
    if (existsSync(installerPath)) unlinkSync(installerPath);
    renameSync(partPath, installerPath);
  } catch (err) {
    throw new DownloadError("Failed to promote .part to installer path", err);
  }

  await runSilentInstall(installerPath, targetDir, (pct) =>
    onProgress(pct, "install"),
  );
}

/**
 * Resumable HTTPS download. Sends `Range: bytes=N-` when a partial file is
 * present; falls back to a clean restart if the server returns 200 instead
 * of 206 Partial Content. The hash is constructed incrementally so the
 * full-file SHA256 can be verified even when the download arrives across
 * multiple sessions.
 */
export async function downloadWithResume(
  url: string,
  partPath: string,
  expectedSha: string,
  onPct: (pct: number) => void,
): Promise<void> {
  const startBytes = existsSync(partPath) ? statSync(partPath).size : 0;

  // If a partial file exists, hash it first so the resumed stream can keep
  // building toward the same digest. If the resumed response turns out to
  // be 200 (server ignored Range) instead of 206, we reset both the file
  // and the hash before re-downloading from scratch.
  let hash: Hash = createHash("sha256");
  if (startBytes > 0) {
    await hashFile(partPath, hash);
  }

  const headers: RequestOptions["headers"] = {
    // Some CDNs swallow Range when no UA is set. The exact UA doesn't matter;
    // we just need it to be present.
    "User-Agent": "AltoSymbiosis-Bootstrap/1.0",
  };
  if (startBytes > 0) {
    headers["Range"] = `bytes=${startBytes}-`;
  }

  await new Promise<void>((resolve, reject) => {
    const req = httpsGet(url, { headers }, (res) => {
      handleResponse(res, startBytes).then(resolve, reject);
    });
    req.on("error", (err) =>
      reject(new DownloadError(`HTTPS GET failed for ${url}`, err)),
    );
  });

  const actualSha = hash.digest("hex");
  if (actualSha !== expectedSha) {
    try {
      unlinkSync(partPath);
    } catch {
      /* best-effort */
    }
    throw new ChecksumMismatch(expectedSha, actualSha);
  }

  async function handleResponse(
    res: IncomingMessage,
    rangeStart: number,
  ): Promise<void> {
    const status = res.statusCode ?? 0;

    // Server ignored Range and is sending us the full file: discard any
    // partial state and re-hash from scratch.
    if (rangeStart > 0 && status === 200) {
      try {
        unlinkSync(partPath);
      } catch {
        /* best-effort */
      }
      hash = createHash("sha256");
      await pipeToFile(res, partPath, 0, parseTotal(res, 0), onPct, hash);
      return;
    }

    if (rangeStart > 0 && status === 206) {
      const total = parseTotal(res, rangeStart);
      await pipeToFile(res, partPath, rangeStart, total, onPct, hash, true);
      return;
    }

    if (rangeStart === 0 && status === 200) {
      const total = parseTotal(res, 0);
      await pipeToFile(res, partPath, 0, total, onPct, hash);
      return;
    }

    throw new DownloadError(
      `Unexpected HTTP status ${status} from ${url} (rangeStart=${rangeStart})`,
    );
  }
}

function parseTotal(res: IncomingMessage, rangeStart: number): number {
  // Prefer Content-Range's total size when present (206 responses); fall back
  // to Content-Length + rangeStart (which is 0 for a full-body 200 response).
  const cr = res.headers["content-range"];
  if (typeof cr === "string") {
    const m = /\/(\d+)\s*$/.exec(cr);
    if (m) return Number.parseInt(m[1], 10);
  }
  const cl = res.headers["content-length"];
  const clNum = typeof cl === "string" ? Number.parseInt(cl, 10) : NaN;
  if (Number.isFinite(clNum)) return clNum + rangeStart;
  return 0; // Unknown total — progress will be indeterminate.
}

function pipeToFile(
  res: IncomingMessage,
  partPath: string,
  startBytes: number,
  total: number,
  onPct: (pct: number) => void,
  hash: Hash,
  append = false,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let out: WriteStream;
    try {
      out = createWriteStream(partPath, { flags: append ? "a" : "w" });
    } catch (err) {
      reject(new DownloadError("Failed to open .part file for write", err));
      return;
    }

    let downloaded = startBytes;
    res.on("data", (chunk: Buffer) => {
      downloaded += chunk.length;
      hash.update(chunk);
      if (total > 0) {
        onPct(Math.min(99, Math.floor((downloaded / total) * 100)));
      }
    });
    res.on("error", (err) => {
      out.destroy();
      reject(new DownloadError("Response stream errored", err));
    });
    res.on("end", () => {
      out.end(() => {
        onPct(100);
        resolve();
      });
    });
    res.pipe(out);
  });
}

function hashFile(path: string, hash: Hash): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", () => resolve());
  });
}

/**
 * Silent install via NSIS. `/D=<path>` must be the LAST argument and is
 * unquoted per the NSIS spec; we pass `windowsVerbatimArguments: true` so
 * Electron's argv escaper doesn't quote it. Progress is approximated by
 * polling for the appearance of `<targetDir>/python.exe`; NSIS itself emits
 * no machine-readable progress signal.
 */
export async function runSilentInstall(
  installerPath: string,
  targetDir: string,
  onPct: (pct: number) => void,
): Promise<void> {
  let stopPoll = false;
  const pollStart = Date.now();
  const targetPython = join(targetDir, "python.exe");

  // Approximate progress: bump every 500ms until python.exe appears, then
  // jump straight to 99% so the UI doesn't pin at 99 forever. The final
  // hop to 100 happens when spawn exits successfully.
  const tick = setInterval(() => {
    if (stopPoll) return;
    const elapsed = Date.now() - pollStart;
    if (existsSync(targetPython)) {
      onPct(99);
      return;
    }
    const pct = Math.min(95, Math.floor((elapsed / INSTALL_POLL_MAX_MS) * 95));
    onPct(pct);
  }, INSTALL_POLL_INTERVAL_MS);

  try {
    await new Promise<void>((resolve, reject) => {
      const child = spawn(installerPath, ["/S", `/D=${targetDir}`], {
        windowsVerbatimArguments: true,
        windowsHide: true,
      });
      child.on("error", () => reject(new InstallerExitNonZero(null, null)));
      child.on("exit", (code, signal) => {
        if (code === 0) resolve();
        else reject(new InstallerExitNonZero(code, signal));
      });
    });
    onPct(100);
  } finally {
    stopPoll = true;
    clearInterval(tick);
  }
}
