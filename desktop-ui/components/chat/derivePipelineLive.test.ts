import { describe, expect, it } from "vitest";

import { derivePipelineLive } from "./derivePipelineLive";
import type { StreamingEvent } from "./events";

function evt(type: string, data: unknown = {}, at = 0): StreamingEvent {
  return { type, data, at };
}

describe("derivePipelineLive", () => {
  it("returns idle state for empty events", () => {
    expect(derivePipelineLive([])).toEqual({ steps: [], phase: "idle" });
  });

  describe("phase transitions", () => {
    it("transitions idle → decomposing → running → synthesising → complete", () => {
      expect(derivePipelineLive([
        evt("pipeline_decomposing"),
      ]).phase).toBe("decomposing");

      expect(derivePipelineLive([
        evt("pipeline_decomposing"),
        evt("pipeline_plan", { steps: [{ agent: "A", task: "t" }] }),
      ]).phase).toBe("running");

      expect(derivePipelineLive([
        evt("pipeline_plan", { steps: [{ agent: "A", task: "t" }] }),
        evt("pipeline_synthesising"),
      ]).phase).toBe("synthesising");

      expect(derivePipelineLive([
        evt("pipeline_synthesising"),
        evt("pipeline_complete"),
      ]).phase).toBe("complete");
    });

    it("flips back to running on pipeline_step_started after synthesising", () => {
      // Defensive: if the backend ever re-enters running, phase reflects it.
      const live = derivePipelineLive([
        evt("pipeline_synthesising"),
        evt("pipeline_step_started", { step: 1, agent: "A" }),
      ]);
      expect(live.phase).toBe("running");
    });
  });

  describe("step accumulation", () => {
    it("seeds steps from pipeline_plan in declared order", () => {
      const live = derivePipelineLive([
        evt("pipeline_plan", {
          steps: [
            { agent: "Researcher", task: "find" },
            { agent: "Writer", task: "compose" },
          ],
        }),
      ]);
      expect(live.steps).toEqual([
        { step: 1, agent: "Researcher", task: "find" },
        { step: 2, agent: "Writer", task: "compose" },
      ]);
    });

    it("uses default agent/task when pipeline_plan omits fields", () => {
      const live = derivePipelineLive([
        evt("pipeline_plan", { steps: [{}, { task: "go" }] }),
      ]);
      expect(live.steps[0]).toEqual({ step: 1, agent: "Specialist", task: "" });
      expect(live.steps[1]).toEqual({ step: 2, agent: "Specialist", task: "go" });
    });

    it("merges pipeline_step_complete metadata into the existing step", () => {
      const live = derivePipelineLive([
        evt("pipeline_plan", { steps: [{ agent: "A", task: "t" }] }),
        evt("pipeline_step_complete", {
          step: 1,
          confidence: "high",
          validation_passed: true,
          tokens: 42,
          duration_ms: 1234,
          challenger_signal: false,
        }),
      ]);
      expect(live.steps).toHaveLength(1);
      expect(live.steps[0]).toMatchObject({
        step: 1,
        agent: "A",
        task: "t",
        confidence: "high",
        validation_passed: true,
        tokens: 42,
        duration_ms: 1234,
        challenger_signal: false,
      });
    });

    it("ignores step_started / step_complete with no step number", () => {
      const live = derivePipelineLive([
        evt("pipeline_step_started", { agent: "ghost" }),
        evt("pipeline_step_complete", { agent: "ghost" }),
      ]);
      expect(live.steps).toEqual([]);
    });

    it("pipeline_plan clears any prior step accumulation", () => {
      const live = derivePipelineLive([
        evt("pipeline_step_started", { step: 9, agent: "stale" }),
        evt("pipeline_plan", { steps: [{ agent: "fresh", task: "t" }] }),
      ]);
      expect(live.steps).toHaveLength(1);
      expect(live.steps[0].agent).toBe("fresh");
    });

    it("sorts steps by step number even if events arrive out of order", () => {
      const live = derivePipelineLive([
        evt("pipeline_step_started", { step: 3, agent: "C" }),
        evt("pipeline_step_started", { step: 1, agent: "A" }),
        evt("pipeline_step_started", { step: 2, agent: "B" }),
      ]);
      expect(live.steps.map((s) => s.agent)).toEqual(["A", "B", "C"]);
    });

    it("step_started fills agent/task from the prior plan entry when omitted", () => {
      const live = derivePipelineLive([
        evt("pipeline_plan", { steps: [{ agent: "Planned", task: "plan-task" }] }),
        evt("pipeline_step_started", { step: 1 }), // no agent / task override
      ]);
      expect(live.steps[0].agent).toBe("Planned");
      expect(live.steps[0].task).toBe("plan-task");
    });
  });

  it("non-pipeline events are ignored", () => {
    const live = derivePipelineLive([
      evt("route_decided", { model: "claude" }),
      evt("memory_recalled", { facts_count: 1 }),
      evt("security_scan", { verdict: "allow" }),
    ]);
    expect(live).toEqual({ steps: [], phase: "idle" });
  });
});
