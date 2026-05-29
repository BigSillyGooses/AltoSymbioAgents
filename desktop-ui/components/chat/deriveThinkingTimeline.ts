// desktop-ui/components/chat/deriveThinkingTimeline.ts
//
// Thinking timeline: non-step lifecycle events.
//
// The pipeline chips elsewhere cover one row per specialist step. Everything
// else the backend emits — checkpoint rollbacks, retries, the adversarial
// challenger, memory recall, route decisions, CaMeL plan/execute counters,
// security findings, governance blocks, alignment warnings — is summarised
// here as an ordered list of one-line rows inside the streaming bubble.
//
// Intentional drops:
//   * pipeline_started / message_start — lifecycle bookkeeping, no user value
//   * pipeline_decomposing / _plan / _step_* / _synthesising / _complete —
//     already rendered as chips by LivePipelineAttribution
//   * checkpoint_state with state="provisional" — start signal redundant with
//     the chip's "in progress" dot
//   * checkpoint_state with state="committed" — success implicit in the chip
//     flipping to its accent palette
//   * escalation_required / escalation_resolved — surfaced in EscalationPanel
//   * high_stakes_voting_started / _complete — drives a separate top-bar
//     spinner (see App.tsx setVotingActive)
//
// Each rendered row carries a state ("info" | "ok" | "warn" | "error") that
// maps to the same accent/warn/err palette PipelineAttribution and the rest
// of the chat surface already use.

import type { StreamingEvent } from "./events";
export type { StreamingEvent };

export type ThinkingRowState = "info" | "ok" | "warn" | "error";

export interface ThinkingRow {
  key:     string;
  state:   ThinkingRowState;
  icon:    string;
  label:   string;
  detail?: string;
  // Full, un-truncated narrative the compact `detail` was summarising. Only
  // the drillable variant of the timeline (DevinTimeline) reads this field;
  // ThinkingTimeline ignores it. Empty unless the event carried longer
  // free-form text than fits in the collapsed row.
  expandedDetail?: string;
}

interface CheckpointStateEvent {
  step?:    number;
  agent?:   string;
  state?:   string;
  reason?:  string;
  retry?:   number;
}

interface PipelineStepRetryEvent {
  step?:    number;
  agent?:   string;
  reason?:  string;
  attempt?: number;
}

interface ChallengerStartedEvent {
  step?:  number;
  agent?: string;
}

interface ChallengerCompleteEvent {
  step?:         number;
  signal?:       boolean;
  parse_failed?: boolean;
}

interface MemoryRecalledEvent {
  facts_count?: number;
  rag_chunks?:  number;
  memories?:    number;
}

interface CompoundQueryEvent {
  message?:    string;
  suggestion?: string;
}

interface VisionUnavailableEvent {
  active_model?: string;
  families?:     string[];
}

interface GovernanceBlockedEvent {
  agent_id?: string;
  reason?:   string;
  policy?:   string;
}

interface CamelStartedEvent {
  rag_chunks?: number;
}

interface CamelCompleteEvent {
  executed_steps?:        number;
  capability_violations?: number;
  blocked_calls?:         number;
  error?:                 string;
}

interface ReaderCompleteEvent {
  intent?:          string;
  proposed_tools?:  string[];
  red_flags?:       string[];
}

interface ReasoningEvent {
  label?:            string;
  detail?:           string;
  thinking_preview?: string;
}

interface AlignmentWarningEvent {
  reason?: string;
}

interface SecurityAssessmentEvent {
  icon?:   string;
  label?:  string;
  detail?: string;
  status?: string;
}

interface RouteDecidedEvent {
  model?:      string;
  complexity?: string;
  reasoning?:  string;
  confidence?: number;
}

interface SecurityScanEvent {
  icon?:    string;
  label?:   string;
  detail?:  string;
  verdict?: string;
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

export function deriveThinkingTimeline(events: StreamingEvent[]): ThinkingRow[] {
  const rows: ThinkingRow[] = [];
  // Suppress duplicate-noise: a route_decided is emitted once per turn but a
  // few orchestrator paths re-run it; only the latest wins. Same applies to
  // memory_recalled when the post-trim recall re-fires.
  let routeRowIdx   = -1;
  let memoryRowIdx  = -1;
  let securityIdx   = -1;

  for (let i = 0; i < events.length; i++) {
    const evt = events[i];
    const d   = (evt.data ?? {}) as Record<string, unknown>;
    const key = `${evt.type}-${i}-${evt.at}`;

    if (evt.type === "route_decided") {
      const r = d as RouteDecidedEvent;
      const detailParts: string[] = [];
      if (r.complexity) detailParts.push(r.complexity);
      if (typeof r.confidence === "number") {
        detailParts.push(`confidence ${(r.confidence * 100).toFixed(0)}%`);
      }
      if (r.reasoning) detailParts.push(truncate(r.reasoning, 80));
      const row: ThinkingRow = {
        key,
        state: "info",
        icon:  "🎯",
        label: `Routed to ${r.model || "model"}`,
        detail: detailParts.join(" · ") || undefined,
        expandedDetail: r.reasoning && r.reasoning.length > 80
          ? r.reasoning
          : undefined,
      };
      if (routeRowIdx >= 0) {
        rows[routeRowIdx] = row;
      } else {
        routeRowIdx = rows.length;
        rows.push(row);
      }
      continue;
    }

    if (evt.type === "memory_recalled") {
      const r = d as MemoryRecalledEvent;
      const facts = r.facts_count ?? 0;
      const rag   = r.rag_chunks  ?? 0;
      const mems  = r.memories    ?? 0;
      if (facts + rag + mems === 0) continue;
      const parts: string[] = [];
      if (facts > 0) parts.push(`${facts} fact${facts === 1 ? "" : "s"}`);
      if (rag   > 0) parts.push(`${rag} chunk${rag   === 1 ? "" : "s"}`);
      if (mems  > 0) parts.push(`${mems} memor${mems  === 1 ? "y" : "ies"}`);
      const row: ThinkingRow = {
        key,
        state: "info",
        icon:  "🧠",
        label: "Recalled context",
        detail: parts.join(" · "),
      };
      if (memoryRowIdx >= 0) {
        rows[memoryRowIdx] = row;
      } else {
        memoryRowIdx = rows.length;
        rows.push(row);
      }
      continue;
    }

    if (evt.type === "security_assessment") {
      const r = d as SecurityAssessmentEvent;
      const state: ThinkingRowState =
        r.status === "error" ? "error" :
        r.status === "warn"  ? "warn"  :
        "ok";
      const row: ThinkingRow = {
        key,
        state,
        icon:  r.icon  || "🛡️",
        label: r.label || "Security check",
        detail: r.detail,
      };
      if (securityIdx >= 0) {
        rows[securityIdx] = row;
      } else {
        securityIdx = rows.length;
        rows.push(row);
      }
      continue;
    }

    if (evt.type === "security_scan") {
      const r = d as SecurityScanEvent;
      const state: ThinkingRowState =
        r.verdict === "block" ? "error" :
        r.verdict === "warn"  ? "warn"  :
        "ok";
      rows.push({
        key,
        state,
        icon:  r.icon  || "🛡️",
        label: r.label || "Input scan",
        detail: r.detail,
      });
      continue;
    }

    if (evt.type === "checkpoint_state") {
      const r = d as CheckpointStateEvent;
      // Only surface failures here; commit/start are implicit in the chip.
      if (r.state !== "rolled_back") continue;
      rows.push({
        key,
        state: "warn",
        icon:  "↺",
        label: `Step ${r.step ?? "?"} rolled back${r.agent ? ` · ${r.agent}` : ""}`,
        detail: r.reason ? truncate(r.reason, 140) : undefined,
        expandedDetail: r.reason && r.reason.length > 140 ? r.reason : undefined,
      });
      continue;
    }

    if (evt.type === "pipeline_step_retry") {
      const r = d as PipelineStepRetryEvent;
      rows.push({
        key,
        state: "warn",
        icon:  "🔁",
        label: `Step ${r.step ?? "?"} retry${r.attempt ? ` (attempt ${r.attempt})` : ""}${r.agent ? ` · ${r.agent}` : ""}`,
        detail: r.reason ? truncate(r.reason, 140) : undefined,
        expandedDetail: r.reason && r.reason.length > 140 ? r.reason : undefined,
      });
      continue;
    }

    if (evt.type === "challenger_started") {
      const r = d as ChallengerStartedEvent;
      rows.push({
        key,
        state: "info",
        icon:  "⚖",
        label: `Reviewer checking step ${r.step ?? "?"}${r.agent ? ` · ${r.agent}` : ""}`,
      });
      continue;
    }

    if (evt.type === "challenger_complete") {
      const r = d as ChallengerCompleteEvent;
      if (r.parse_failed) {
        rows.push({
          key,
          state: "warn",
          icon:  "⚖",
          label: `Reviewer step ${r.step ?? "?"}: response unparseable`,
        });
      } else if (r.signal) {
        rows.push({
          key,
          state: "warn",
          icon:  "⚖",
          label: `Reviewer flagged step ${r.step ?? "?"}`,
          detail: "Synthesis will see the critique.",
        });
      } else {
        rows.push({
          key,
          state: "ok",
          icon:  "⚖",
          label: `Reviewer cleared step ${r.step ?? "?"}`,
        });
      }
      continue;
    }

    if (evt.type === "compound_query_detected") {
      const r = d as CompoundQueryEvent;
      rows.push({
        key,
        state: "info",
        icon:  "🧩",
        label: r.message || "Multi-part request detected",
        detail: r.suggestion,
      });
      continue;
    }

    if (evt.type === "vision_unavailable") {
      const r = d as VisionUnavailableEvent;
      const fams = Array.isArray(r.families) && r.families.length > 0
        ? `Try: ${r.families.join(", ")}`
        : undefined;
      rows.push({
        key,
        state: "warn",
        icon:  "🖼️",
        label: `Local model can't see images${r.active_model ? ` (${r.active_model})` : ""}`,
        detail: fams,
      });
      continue;
    }

    if (evt.type === "governance_blocked") {
      const r = d as GovernanceBlockedEvent;
      rows.push({
        key,
        state: "error",
        icon:  "⛔",
        label: `Governance blocked${r.policy ? ` · ${r.policy}` : ""}`,
        detail: r.reason ? truncate(r.reason, 140) : undefined,
        expandedDetail: r.reason && r.reason.length > 140 ? r.reason : undefined,
      });
      continue;
    }

    if (evt.type === "camel_started") {
      const r = d as CamelStartedEvent;
      rows.push({
        key,
        state: "info",
        icon:  "🐫",
        label: "CaMeL planning",
        detail: typeof r.rag_chunks === "number"
          ? `${r.rag_chunks} retrieved chunk${r.rag_chunks === 1 ? "" : "s"}`
          : undefined,
      });
      continue;
    }

    if (evt.type === "camel_complete") {
      const r = d as CamelCompleteEvent;
      const errored = !!(r.error && r.error.length > 0);
      const violations = r.capability_violations ?? 0;
      const blocked    = r.blocked_calls ?? 0;
      const state: ThinkingRowState = errored
        ? "error"
        : (violations > 0 || blocked > 0)
          ? "warn"
          : "ok";
      const parts: string[] = [];
      if (typeof r.executed_steps === "number") parts.push(`${r.executed_steps} step${r.executed_steps === 1 ? "" : "s"}`);
      if (violations > 0) parts.push(`${violations} capability violation${violations === 1 ? "" : "s"}`);
      if (blocked    > 0) parts.push(`${blocked} blocked call${blocked === 1 ? "" : "s"}`);
      rows.push({
        key,
        state,
        icon:  "🐫",
        label: errored ? "CaMeL fell back" : "CaMeL plan executed",
        detail: errored ? truncate(r.error || "", 140) : (parts.join(" · ") || undefined),
        expandedDetail: errored && r.error && r.error.length > 140
          ? r.error
          : undefined,
      });
      continue;
    }

    if (evt.type === "reader_complete") {
      const r = d as ReaderCompleteEvent;
      const tools = Array.isArray(r.proposed_tools) ? r.proposed_tools : [];
      const flags = Array.isArray(r.red_flags)      ? r.red_flags      : [];
      const detailParts: string[] = [];
      if (tools.length > 0) detailParts.push(`tools: ${tools.slice(0, 3).join(", ")}${tools.length > 3 ? "…" : ""}`);
      if (flags.length > 0) detailParts.push(`${flags.length} red flag${flags.length === 1 ? "" : "s"}`);
      // Compose the un-truncated narrative for the drill-down: full intent +
      // full tools list + full red-flags list. Only emit when at least one
      // field carries more than the collapsed `detail` could fit.
      const expandedParts: string[] = [];
      if (r.intent && r.intent.length > 0) expandedParts.push(`Intent: ${r.intent}`);
      if (tools.length > 3) expandedParts.push(`Proposed tools: ${tools.join(", ")}`);
      if (flags.length > 0) expandedParts.push(`Red flags: ${flags.join(", ")}`);
      rows.push({
        key,
        state: flags.length > 0 ? "warn" : "info",
        icon:  "📖",
        label: "Reader analysed request",
        detail: detailParts.join(" · ") || (r.intent ? truncate(r.intent, 100) : undefined),
        expandedDetail: expandedParts.length > 0 ? expandedParts.join("\n") : undefined,
      });
      continue;
    }

    if (evt.type === "reasoning_started") {
      const r = d as ReasoningEvent;
      rows.push({
        key,
        state: "info",
        icon:  "💭",
        label: r.label || "Extended reasoning…",
        detail: r.detail,
      });
      continue;
    }

    if (evt.type === "reasoning_complete") {
      const r = d as ReasoningEvent;
      rows.push({
        key,
        state: "ok",
        icon:  "💭",
        label: r.label || "Reasoning complete",
        detail: r.detail || (r.thinking_preview ? truncate(r.thinking_preview, 140) : undefined),
        expandedDetail: r.thinking_preview && r.thinking_preview.length > 140
          ? r.thinking_preview
          : undefined,
      });
      continue;
    }

    if (evt.type === "alignment_warning") {
      const r = d as AlignmentWarningEvent;
      rows.push({
        key,
        state: "warn",
        icon:  "⚠",
        label: "Response may not address your request",
        detail: r.reason ? truncate(r.reason, 140) : undefined,
        expandedDetail: r.reason && r.reason.length > 140 ? r.reason : undefined,
      });
      continue;
    }
  }

  return rows;
}
