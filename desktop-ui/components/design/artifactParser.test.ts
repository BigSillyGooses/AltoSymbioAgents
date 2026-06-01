import { describe, expect, it } from "vitest";

import {
  createArtifactParser,
  splitMessage,
  type ArtifactEvent,
  type MessageSegment,
} from "./artifactParser";

// Drain a parser over a list of deltas (then flush) into a flat event array,
// so we can assert on the exact event sequence regardless of chunk boundaries.
function run(deltas: string[]): ArtifactEvent[] {
  const parser = createArtifactParser();
  const events: ArtifactEvent[] = [];
  for (const d of deltas) {
    for (const e of parser.feed(d)) events.push(e);
  }
  for (const e of parser.flush()) events.push(e);
  return events;
}

describe("createArtifactParser", () => {
  it("emits a single text event for plain content", () => {
    const events = run(["hello world"]);
    expect(events).toEqual([{ type: "text", delta: "hello world" }]);
  });

  it("parses a complete artifact with text before and after", () => {
    const msg =
      'Here you go.\n<artifact identifier="hero" type="text/html" title="Hero">' +
      "<!doctype html><h1>Hi</h1></artifact>\nDone.";
    const events = run([msg]);
    expect(events[0]).toEqual({ type: "text", delta: "Here you go.\n" });
    const start = events.find((e) => e.type === "artifact:start");
    expect(start).toEqual({
      type: "artifact:start",
      identifier: "hero",
      artifactType: "text/html",
      title: "Hero",
    });
    const end = events.find((e) => e.type === "artifact:end");
    expect(end).toEqual({
      type: "artifact:end",
      identifier: "hero",
      fullContent: "<!doctype html><h1>Hi</h1>",
    });
    expect(events[events.length - 1]).toEqual({ type: "text", delta: "\nDone." });
  });

  it("reassembles an open tag split across feed() calls", () => {
    // Split right in the middle of "<artifact".
    const events = run([
      'intro <arti',
      'fact identifier="a" type="text/html" title="A">body',
      "</artifact>",
    ]);
    const start = events.find((e) => e.type === "artifact:start");
    expect(start && start.type === "artifact:start" && start.identifier).toBe("a");
    const end = events.find((e) => e.type === "artifact:end");
    expect(end && end.type === "artifact:end" && end.fullContent).toBe("body");
  });

  it("reassembles a close tag split across feed() calls", () => {
    const events = run([
      '<artifact identifier="a" type="text/html" title="A">abc</arti',
      "fact>tail",
    ]);
    const end = events.find((e) => e.type === "artifact:end");
    expect(end && end.type === "artifact:end" && end.fullContent).toBe("abc");
    expect(events[events.length - 1]).toEqual({ type: "text", delta: "tail" });
  });

  it("does not treat <artifactual> as an artifact open tag", () => {
    const events = run(["the word <artifactual> is not a tag"]);
    expect(events.every((e) => e.type === "text")).toBe(true);
    expect(events.map((e) => (e.type === "text" ? e.delta : "")).join("")).toBe(
      "the word <artifactual> is not a tag",
    );
  });

  it("flush() closes an unterminated artifact", () => {
    const events = run([
      '<artifact identifier="a" type="text/html" title="A">never closed',
    ]);
    const end = events.find((e) => e.type === "artifact:end");
    expect(end).toEqual({
      type: "artifact:end",
      identifier: "a",
      fullContent: "never closed",
    });
  });
});

describe("splitMessage", () => {
  it("returns a single text segment for plain prose", () => {
    const segs: MessageSegment[] = splitMessage("just text");
    expect(segs).toEqual([{ kind: "text", text: "just text" }]);
  });

  it("splits prose and a closed artifact into ordered segments", () => {
    const msg =
      'Built it.\n<artifact identifier="p" type="text/html" title="Page">' +
      "<html></html></artifact>";
    const segs = splitMessage(msg);
    expect(segs[0]).toEqual({ kind: "text", text: "Built it.\n" });
    expect(segs[1]).toMatchObject({
      kind: "artifact",
      identifier: "p",
      title: "Page",
      content: "<html></html>",
      closed: true,
    });
  });

  it("marks an unterminated artifact as not closed (mid-stream)", () => {
    const segs = splitMessage(
      '<artifact identifier="p" type="text/html" title="P"><html>',
    );
    expect(segs).toHaveLength(1);
    expect(segs[0]).toMatchObject({ kind: "artifact", closed: false, content: "<html>" });
  });
});
