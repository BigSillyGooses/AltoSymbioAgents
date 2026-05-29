import { describe, expect, it } from "vitest";

import {
  deriveThinkingTimeline,
  type StreamingEvent,
} from "./deriveThinkingTimeline";

function evt(type: string, data: unknown, at = 0): StreamingEvent {
  return { type, data, at };
}

describe("deriveThinkingTimeline", () => {
  it("returns no rows for empty events", () => {
    expect(deriveThinkingTimeline([])).toEqual([]);
  });

  it("ignores events that don't map to a row", () => {
    const rows = deriveThinkingTimeline([
      evt("pipeline_started", {}),
      evt("pipeline_complete", {}),
      // checkpoint_state with non-rolled_back status is intentionally dropped.
      evt("checkpoint_state", { state: "provisional", step: 1 }),
      evt("checkpoint_state", { state: "committed", step: 1 }),
    ]);
    expect(rows).toEqual([]);
  });

  describe("dedupe of single-row-per-turn events", () => {
    it("keeps only the latest route_decided", () => {
      const rows = deriveThinkingTimeline([
        evt("route_decided", { model: "claude", complexity: "complex" }, 1),
        evt("route_decided", { model: "local", complexity: "simple" }, 2),
      ]);
      // One row, reflecting the second event.
      expect(rows).toHaveLength(1);
      expect(rows[0].label).toBe("Routed to local");
      expect(rows[0].detail).toContain("simple");
    });

    it("keeps only the latest memory_recalled", () => {
      const rows = deriveThinkingTimeline([
        evt("memory_recalled", { facts_count: 1, rag_chunks: 0, memories: 0 }, 1),
        evt("memory_recalled", { facts_count: 3, rag_chunks: 2, memories: 1 }, 2),
      ]);
      expect(rows).toHaveLength(1);
      expect(rows[0].label).toBe("Recalled context");
      expect(rows[0].detail).toBe("3 facts · 2 chunks · 1 memory");
    });

    it("keeps only the latest security_assessment", () => {
      const rows = deriveThinkingTimeline([
        evt("security_assessment", { status: "ok", label: "first" }, 1),
        evt("security_assessment", { status: "error", label: "blocked" }, 2),
      ]);
      expect(rows).toHaveLength(1);
      expect(rows[0].label).toBe("blocked");
      expect(rows[0].state).toBe("error");
    });

    it("drops memory_recalled with all-zero counts", () => {
      const rows = deriveThinkingTimeline([
        evt("memory_recalled", { facts_count: 0, rag_chunks: 0, memories: 0 }),
      ]);
      expect(rows).toEqual([]);
    });
  });

  describe("ordering and interleaving", () => {
    it("preserves emission order across distinct event types", () => {
      const rows = deriveThinkingTimeline([
        evt("route_decided", { model: "claude" }, 1),
        evt("memory_recalled", { facts_count: 1 }, 2),
        evt("reader_complete", { intent: "test" }, 3),
      ]);
      expect(rows.map((r) => r.icon)).toEqual(["🎯", "🧠", "📖"]);
    });

    it("replaces a deduped row in place (preserves position)", () => {
      const rows = deriveThinkingTimeline([
        evt("route_decided", { model: "claude" }, 1),
        evt("memory_recalled", { facts_count: 1 }, 2),
        // Second route_decided arrives later — replaces the first one in
        // place rather than appending a third row.
        evt("route_decided", { model: "local" }, 3),
      ]);
      expect(rows).toHaveLength(2);
      expect(rows[0].label).toBe("Routed to local");
      expect(rows[1].icon).toBe("🧠");
    });
  });

  describe("state mapping", () => {
    it("maps security_scan verdict to row state", () => {
      const rows = deriveThinkingTimeline([
        evt("security_scan", { verdict: "block" }),
        evt("security_scan", { verdict: "warn" }),
        evt("security_scan", { verdict: "allow" }),
      ]);
      expect(rows.map((r) => r.state)).toEqual(["error", "warn", "ok"]);
    });

    it("maps challenger_complete signal/parse_failed/clear", () => {
      const rows = deriveThinkingTimeline([
        evt("challenger_complete", { step: 1, parse_failed: true }),
        evt("challenger_complete", { step: 2, signal: true }),
        evt("challenger_complete", { step: 3 }),
      ]);
      expect(rows.map((r) => r.state)).toEqual(["warn", "warn", "ok"]);
      expect(rows[1].detail).toBe("Synthesis will see the critique.");
    });

    it("escalates camel_complete to error/warn/ok by signals", () => {
      const ok = deriveThinkingTimeline([
        evt("camel_complete", { executed_steps: 2 }),
      ]);
      const warn = deriveThinkingTimeline([
        evt("camel_complete", { executed_steps: 2, blocked_calls: 1 }),
      ]);
      const err = deriveThinkingTimeline([
        evt("camel_complete", { error: "fallback" }),
      ]);
      expect(ok[0].state).toBe("ok");
      expect(warn[0].state).toBe("warn");
      expect(err[0].state).toBe("error");
      expect(err[0].label).toBe("CaMeL fell back");
    });

    it("flags reader_complete with red flags as warn", () => {
      const rows = deriveThinkingTimeline([
        evt("reader_complete", {
          intent: "test",
          proposed_tools: [],
          red_flags: ["jailbreak"],
        }),
      ]);
      expect(rows[0].state).toBe("warn");
      expect(rows[0].detail).toContain("1 red flag");
    });
  });

  describe("truncation in detail fields", () => {
    it("truncates long route reasoning to ~80 chars", () => {
      const long = "a".repeat(200);
      const rows = deriveThinkingTimeline([
        evt("route_decided", { model: "claude", reasoning: long }),
      ]);
      const detail = rows[0].detail!;
      // truncate keeps n-1 chars + ellipsis = 80 chars total
      expect(detail.length).toBeLessThanOrEqual(80);
      expect(detail.endsWith("…")).toBe(true);
    });

    it("truncates governance_blocked reason to ~140 chars", () => {
      const long = "x".repeat(300);
      const rows = deriveThinkingTimeline([
        evt("governance_blocked", { policy: "p", reason: long }),
      ]);
      expect(rows[0].detail!.length).toBeLessThanOrEqual(140);
      expect(rows[0].detail!.endsWith("…")).toBe(true);
    });
  });

  describe("event keys are unique", () => {
    it("derives a unique key per emitted row from index + at", () => {
      const rows = deriveThinkingTimeline([
        evt("reasoning_started", { label: "a" }, 100),
        evt("reasoning_started", { label: "b" }, 200),
        evt("reasoning_complete", { label: "a done" }, 300),
      ]);
      const keys = rows.map((r) => r.key);
      expect(new Set(keys).size).toBe(rows.length);
    });
  });

  describe("expandedDetail for the drillable variant", () => {
    it("populates the un-truncated reasoning on route_decided when it would clip", () => {
      const long = "a".repeat(200);
      const rows = deriveThinkingTimeline([
        evt("route_decided", { model: "claude", reasoning: long }),
      ]);
      expect(rows[0].detail!.endsWith("…")).toBe(true);
      expect(rows[0].expandedDetail).toBe(long);
    });

    it("leaves expandedDetail unset when the reasoning fits in the collapsed line", () => {
      const rows = deriveThinkingTimeline([
        evt("route_decided", { model: "claude", reasoning: "short" }),
      ]);
      expect(rows[0].expandedDetail).toBeUndefined();
    });

    it("populates expandedDetail for long rollback / retry / governance / alignment reasons", () => {
      const long = "x".repeat(300);
      const rows = deriveThinkingTimeline([
        evt("checkpoint_state", { state: "rolled_back", step: 1, reason: long }),
        evt("pipeline_step_retry", { step: 2, attempt: 1, reason: long }),
        evt("governance_blocked", { policy: "p", reason: long }),
        evt("alignment_warning", { reason: long }),
      ]);
      for (const r of rows) {
        expect(r.expandedDetail).toBe(long);
        expect(r.detail!.length).toBeLessThanOrEqual(140);
      }
    });

    it("populates expandedDetail for camel_complete only when it errored with a long message", () => {
      const longErr = "boom-".repeat(40);
      const errored = deriveThinkingTimeline([
        evt("camel_complete", { error: longErr }),
      ]);
      const succeeded = deriveThinkingTimeline([
        evt("camel_complete", { executed_steps: 2 }),
      ]);
      expect(errored[0].expandedDetail).toBe(longErr);
      expect(succeeded[0].expandedDetail).toBeUndefined();
    });

    it("packs the full intent / tools / red-flags into reader_complete expandedDetail", () => {
      const intent = "Long intent narrative ".repeat(8);
      const tools  = ["t1", "t2", "t3", "t4", "t5"];
      const flags  = ["jailbreak", "data-exfil"];
      const rows = deriveThinkingTimeline([
        evt("reader_complete", { intent, proposed_tools: tools, red_flags: flags }),
      ]);
      const ex = rows[0].expandedDetail!;
      expect(ex).toContain("Intent: ");
      expect(ex).toContain(intent.trim());
      expect(ex).toContain("Proposed tools: t1, t2, t3, t4, t5");
      expect(ex).toContain("Red flags: jailbreak, data-exfil");
    });

    it("leaves reader_complete expandedDetail unset when nothing exceeds the collapsed summary", () => {
      const rows = deriveThinkingTimeline([
        evt("reader_complete", {
          intent: "",
          proposed_tools: ["a", "b"],
          red_flags: [],
        }),
      ]);
      expect(rows[0].expandedDetail).toBeUndefined();
    });

    it("populates the un-truncated thinking_preview on reasoning_complete", () => {
      const preview = "Thinking trace ".repeat(20);
      const rows = deriveThinkingTimeline([
        evt("reasoning_complete", { thinking_preview: preview }),
      ]);
      expect(rows[0].expandedDetail).toBe(preview);
    });
  });
});
