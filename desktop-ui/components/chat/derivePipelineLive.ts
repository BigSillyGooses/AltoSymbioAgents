// desktop-ui/components/chat/derivePipelineLive.ts
//
// Live pipeline reducer: projects the SSE event log into the "Decomposing →
// Step n/N → Synthesising" view the streaming bubble renders alongside the
// per-step attribution chips.
//
// Sibling of deriveThinkingTimeline (which projects the same event log into
// the non-step thinking-timeline rows). Single-agent turns never emit any
// pipeline_* events, so the reducer stays in its `idle` initial shape and
// adds no overhead.

import type { PipelineStep } from "./MessageBubble";
import type { StreamingEvent } from "./events";

export type PipelinePhase =
  | "idle"
  | "decomposing"
  | "running"
  | "synthesising"
  | "complete";

export interface PipelineLive {
  steps: PipelineStep[];
  phase: PipelinePhase;
}

interface PipelinePlanEvent {
  type?: string;
  steps?: { agent?: string; task?: string }[];
}

interface PipelineStepStartedEvent {
  type?: string;
  step?: number;
  total?: number;
  agent?: string;
  task?: string;
}

interface PipelineStepCompleteEvent {
  type?: string;
  step?: number;
  agent?: string;
  task?: string;
  confidence?: string;
  validation_passed?: boolean;
  tokens?: number;
  duration_ms?: number;
  challenger_signal?: boolean;
}

// Rebuild the live pipeline state from the SSE event log. We don't store
// per-step status incrementally on the store — the events list is already
// the source of truth, and re-deriving on each render keeps the store free
// of pipeline-specific shape. Returns idle steps[] when no pipeline events
// have fired so single-agent turns add no overhead.
export function derivePipelineLive(events: StreamingEvent[]): PipelineLive {
  const stepMap = new Map<number, PipelineStep>();
  let phase: PipelinePhase = "idle";
  for (const evt of events) {
    if (evt.type === "pipeline_decomposing") {
      phase = "decomposing";
    } else if (evt.type === "pipeline_plan") {
      const data = evt.data as PipelinePlanEvent;
      const steps = Array.isArray(data?.steps) ? data.steps : [];
      stepMap.clear();
      steps.forEach((s, i) => {
        stepMap.set(i + 1, {
          step: i + 1,
          agent: s.agent || "Specialist",
          task: s.task ?? "",
        });
      });
      phase = "running";
    } else if (evt.type === "pipeline_step_started") {
      const data = evt.data as PipelineStepStartedEvent;
      const idx = typeof data?.step === "number" ? data.step : 0;
      if (!idx) continue;
      const prev = stepMap.get(idx);
      stepMap.set(idx, {
        step: idx,
        agent: data?.agent || prev?.agent || "Specialist",
        task: data?.task ?? prev?.task ?? "",
      });
      phase = "running";
    } else if (evt.type === "pipeline_step_complete") {
      const data = evt.data as PipelineStepCompleteEvent;
      const idx = typeof data?.step === "number" ? data.step : 0;
      if (!idx) continue;
      const prev = stepMap.get(idx);
      stepMap.set(idx, {
        step: idx,
        agent: data?.agent || prev?.agent || "Specialist",
        task: data?.task ?? prev?.task ?? "",
        confidence: data?.confidence,
        validation_passed: data?.validation_passed,
        tokens: data?.tokens,
        duration_ms: data?.duration_ms,
        challenger_signal: data?.challenger_signal,
      });
    } else if (evt.type === "pipeline_synthesising") {
      phase = "synthesising";
    } else if (evt.type === "pipeline_complete") {
      phase = "complete";
    }
  }
  const steps = Array.from(stepMap.values()).sort((a, b) => a.step - b.step);
  return { steps, phase };
}
