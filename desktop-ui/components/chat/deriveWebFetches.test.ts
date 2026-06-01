import { describe, expect, it } from "vitest";

import { deriveWebFetches, hostLabel } from "./deriveWebFetches";
import type { StreamingEvent } from "./events";

function evt(data: unknown, type = "web_fetch"): StreamingEvent {
  return { type, data, at: 0 };
}

describe("deriveWebFetches", () => {
  it("is empty when there are no web_fetch events", () => {
    const out = deriveWebFetches([evt({ foo: 1 }, "pipeline_plan")]);
    expect(out).toEqual({ active: null, sources: [] });
  });

  it("tracks the active URL while fetching", () => {
    const out = deriveWebFetches([
      evt({ status: "fetching", url: "https://example.com/a" }),
    ]);
    expect(out.active).toBe("https://example.com/a");
    expect(out.sources).toEqual([]);
  });

  it("clears active and records a source on done", () => {
    const out = deriveWebFetches([
      evt({ status: "fetching", url: "https://example.com/a" }),
      evt({ status: "done", url: "https://example.com/a", title: "Example A" }),
    ]);
    expect(out.active).toBeNull();
    expect(out.sources).toEqual([{ url: "https://example.com/a", title: "Example A" }]);
  });

  it("falls back to the host when no title is given", () => {
    const out = deriveWebFetches([
      evt({ status: "done", url: "https://news.example.org/x" }),
    ]);
    expect(out.sources[0]).toEqual({
      url: "https://news.example.org/x",
      title: "news.example.org",
    });
  });

  it("clears active and records no source on error/blocked", () => {
    const out = deriveWebFetches([
      evt({ status: "fetching", url: "http://10.0.0.1/" }),
      evt({ status: "error", url: "http://10.0.0.1/", error: "refused" }),
    ]);
    expect(out.active).toBeNull();
    expect(out.sources).toEqual([]);
  });

  it("dedupes repeated fetches of the same url", () => {
    const out = deriveWebFetches([
      evt({ status: "done", url: "https://x.com", title: "X" }),
      evt({ status: "done", url: "https://x.com", title: "X" }),
    ]);
    expect(out.sources).toHaveLength(1);
  });
});

describe("hostLabel", () => {
  it("returns the host", () => {
    expect(hostLabel("https://example.com/path?q=1")).toBe("example.com");
  });
  it("falls back to the raw string for junk", () => {
    expect(hostLabel("not a url")).toBe("not a url");
  });
});
