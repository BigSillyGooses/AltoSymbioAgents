// @vitest-environment node
//
// Main-process vitest spec for miniconda.ts. node:https and node:child_process
// are swapped via vi.mock so the suite never touches the network and never
// spawns a real installer; that lets the same suite run on Linux CI even
// though the actual download/install pipeline is Windows-only.
//
// The resume test is the load-bearing case (per round-2 review #2 of the
// commit plan): it pre-creates a partial .part file, expects a `Range`
// header on the resumed request, and verifies the recovered full file
// hashes to the expected SHA256.

import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("node:https", () => ({
  get: vi.fn(),
}));
vi.mock("node:child_process", () => ({
  spawn: vi.fn(),
}));

import { get as httpsGet } from "node:https";
import { spawn } from "node:child_process";

import {
  ChecksumMismatch,
  DownloadError,
  InstallerExitNonZero,
  downloadWithResume,
  runSilentInstall,
} from "../miniconda";

type FakeResponse = Readable & {
  statusCode: number;
  headers: Record<string, string>;
};

function buildFakeResponse(opts: {
  status: number;
  body: Buffer;
  headers?: Record<string, string>;
}): FakeResponse {
  const chunks = [opts.body];
  const stream = new Readable({
    read() {
      const next = chunks.shift();
      if (next) this.push(next);
      else this.push(null);
    },
  }) as FakeResponse;
  stream.statusCode = opts.status;
  stream.headers = opts.headers ?? {};
  return stream;
}

function sha256(buf: Buffer): string {
  return createHash("sha256").update(buf).digest("hex");
}

let tempDir: string;
let partPath: string;

beforeEach(() => {
  vi.clearAllMocks();
  tempDir = mkdtempSync(join(tmpdir(), "miniconda-test-"));
  partPath = join(tempDir, "installer.exe.part");
});

afterEach(() => {
  try {
    rmSync(tempDir, { recursive: true, force: true });
  } catch {
    /* best-effort */
  }
});

describe("downloadWithResume", () => {
  it("downloads a full file when no .part exists and verifies SHA256", async () => {
    const body = Buffer.from("hello world", "utf-8");
    const expected = sha256(body);

    vi.mocked(httpsGet).mockImplementation(((url: unknown, opts: unknown, cb: unknown) => {
      const callback = (typeof opts === "function" ? opts : cb) as (
        res: FakeResponse,
      ) => void;
      const res = buildFakeResponse({
        status: 200,
        body,
        headers: { "content-length": String(body.length) },
      });
      setImmediate(() => callback(res));
      return { on: vi.fn() } as unknown as ReturnType<typeof httpsGet>;
    }) as typeof httpsGet);

    const onPct = vi.fn();
    await downloadWithResume("https://example/miniconda.exe", partPath, expected, onPct);

    expect(readFileSync(partPath)).toEqual(body);
    expect(onPct).toHaveBeenCalledWith(100);
  });

  it("resumes from a .part file: sends Range header, appends remaining bytes, hash matches", async () => {
    const fullBody = Buffer.from(
      "hello world — this is the complete miniconda payload",
      "utf-8",
    );
    const half = fullBody.length >> 1;
    const partial = fullBody.subarray(0, half);
    const remaining = fullBody.subarray(half);
    const expected = sha256(fullBody);

    writeFileSync(partPath, partial);

    let rangeHeaderSeen: string | undefined;
    vi.mocked(httpsGet).mockImplementation(((url: unknown, opts: unknown, cb: unknown) => {
      const callback = (typeof opts === "function" ? opts : cb) as (
        res: FakeResponse,
      ) => void;
      const headers =
        typeof opts === "function"
          ? {}
          : ((opts as { headers?: Record<string, string> })?.headers ?? {});
      rangeHeaderSeen = headers["Range"];

      const res = buildFakeResponse({
        status: 206,
        body: remaining,
        headers: {
          "content-range": `bytes ${half}-${fullBody.length - 1}/${fullBody.length}`,
          "content-length": String(remaining.length),
        },
      });
      setImmediate(() => callback(res));
      return { on: vi.fn() } as unknown as ReturnType<typeof httpsGet>;
    }) as typeof httpsGet);

    await downloadWithResume(
      "https://example/miniconda.exe",
      partPath,
      expected,
      () => {},
    );

    expect(rangeHeaderSeen).toBe(`bytes=${half}-`);
    expect(readFileSync(partPath)).toEqual(fullBody);
    expect(sha256(readFileSync(partPath))).toBe(expected);
  });

  it("falls back to a fresh download when the server returns 200 despite Range", async () => {
    // Server ignored the Range header (some CDNs do this); miniconda.ts must
    // truncate the .part file and re-hash from scratch instead of appending.
    const fullBody = Buffer.from("complete reset payload", "utf-8");
    const expected = sha256(fullBody);

    writeFileSync(partPath, Buffer.from("stale partial bytes", "utf-8"));

    vi.mocked(httpsGet).mockImplementation(((url: unknown, opts: unknown, cb: unknown) => {
      const callback = (typeof opts === "function" ? opts : cb) as (
        res: FakeResponse,
      ) => void;
      const res = buildFakeResponse({
        status: 200,
        body: fullBody,
        headers: { "content-length": String(fullBody.length) },
      });
      setImmediate(() => callback(res));
      return { on: vi.fn() } as unknown as ReturnType<typeof httpsGet>;
    }) as typeof httpsGet);

    await downloadWithResume(
      "https://example/miniconda.exe",
      partPath,
      expected,
      () => {},
    );

    expect(readFileSync(partPath)).toEqual(fullBody);
  });

  it("throws ChecksumMismatch and deletes the .part file when the hash differs", async () => {
    const body = Buffer.from("payload that won't hash to the expected value", "utf-8");

    vi.mocked(httpsGet).mockImplementation(((url: unknown, opts: unknown, cb: unknown) => {
      const callback = (typeof opts === "function" ? opts : cb) as (
        res: FakeResponse,
      ) => void;
      const res = buildFakeResponse({ status: 200, body });
      setImmediate(() => callback(res));
      return { on: vi.fn() } as unknown as ReturnType<typeof httpsGet>;
    }) as typeof httpsGet);

    await expect(
      downloadWithResume("https://example/miniconda.exe", partPath, "ff".repeat(32), () => {}),
    ).rejects.toBeInstanceOf(ChecksumMismatch);
    expect(existsSync(partPath)).toBe(false);
  });

  it("labels socket errors as DownloadError", async () => {
    vi.mocked(httpsGet).mockImplementation((() => {
      const req = {
        on: vi.fn((event: string, handler: (err: Error) => void) => {
          if (event === "error") {
            setImmediate(() => handler(new Error("ECONNREFUSED")));
          }
          return req;
        }),
      };
      return req as unknown as ReturnType<typeof httpsGet>;
    }) as typeof httpsGet);

    await expect(
      downloadWithResume("https://example/miniconda.exe", partPath, "ff".repeat(32), () => {}),
    ).rejects.toBeInstanceOf(DownloadError);
  });
});

describe("runSilentInstall", () => {
  it("resolves when the installer exits 0", async () => {
    vi.mocked(spawn).mockImplementation((() => {
      const handlers: Record<string, (code: number | null, signal: NodeJS.Signals | null) => void> =
        {};
      const child = {
        on(event: string, h: (...args: unknown[]) => void) {
          handlers[event] = h as never;
          return child;
        },
      };
      setImmediate(() => handlers["exit"]?.(0, null));
      return child as unknown as ReturnType<typeof spawn>;
    }) as typeof spawn);

    await runSilentInstall("C:/installer.exe", "C:/target", () => {});
  });

  it("throws InstallerExitNonZero on non-zero exit code", async () => {
    vi.mocked(spawn).mockImplementation((() => {
      const handlers: Record<string, (code: number | null, signal: NodeJS.Signals | null) => void> =
        {};
      const child = {
        on(event: string, h: (...args: unknown[]) => void) {
          handlers[event] = h as never;
          return child;
        },
      };
      setImmediate(() => handlers["exit"]?.(1, null));
      return child as unknown as ReturnType<typeof spawn>;
    }) as typeof spawn);

    await expect(
      runSilentInstall("C:/installer.exe", "C:/target", () => {}),
    ).rejects.toBeInstanceOf(InstallerExitNonZero);
  });
});
