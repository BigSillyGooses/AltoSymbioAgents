// desktop-ui/components/chat/MessageBubble.tsx
//
// Layer C2 first slice: extract the leaf message-row renderer from
// ChatView.tsx. MessageBubble is the lowest-coupling component in the
// chat surface — it takes one prop bag and renders one rounded bubble
// with the role-appropriate styling and an optional model/cost footer.
//
// Phase 3 (multi-agent attribution): assistant messages that came out of
// the team pipeline persist a JSON list of per-step summaries on
// `pipeline_steps`. When present, render a collapsed strip of chips above
// the markdown — one chip per specialist contribution — so the user can
// see who did what without leaving the message.

import { useState } from "react";

import { MessageRenderer } from "@/components/MessageRenderer";
import { MessageErrorBoundary } from "@/components/MessageErrorBoundary";

export interface PipelineStep {
  step:               number;
  agent:              string;
  task?:              string;
  confidence?:        string;
  validation_passed?: boolean;
  tokens?:            number;
  duration_ms?:       number;
  challenger_signal?: boolean;
}

export interface MessageRow {
  id:              string;
  role:            "user" | "assistant" | "system";
  content:         string;
  model_used?:     string;
  cost_usd?:       number;
  pipeline_steps?: PipelineStep[] | string | null;
}

interface MessageBubbleProps {
  msg:                 MessageRow;
  voiceOutputEnabled:  boolean;
}

function _parsePipelineSteps(raw: MessageRow["pipeline_steps"]): PipelineStep[] {
  if (!raw) return [];
  if (Array.isArray(raw)) return raw;
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }
  return [];
}

export function MessageBubble({ msg, voiceOutputEnabled }: MessageBubbleProps) {
  const steps = _parsePipelineSteps(msg.pipeline_steps);
  return (
    <div
      className={`max-w-[80%] rounded-xl px-4 py-2 text-sm ${
        msg.role === "user"
          ? "ml-auto bg-accent/15 text-ink border border-accent/20"
          : "bg-bg-2 text-ink border border-line"
      }`}
    >
      {steps.length > 0 && <PipelineAttribution steps={steps} />}
      <MessageErrorBoundary>
        <MessageRenderer
          content={msg.content}
          role={msg.role}
          voiceOutputEnabled={voiceOutputEnabled}
        />
      </MessageErrorBoundary>
      {msg.model_used && (
        <div className="text-[11px] text-ink-faint mt-2">
          {msg.model_used}
          {typeof msg.cost_usd === "number" && ` · $${msg.cost_usd.toFixed(4)}`}
        </div>
      )}
    </div>
  );
}

// ── Phase 3: team-pipeline attribution chips ────────────────────────────────
//
// Renders one chip per pipeline step in a collapsed strip. Clicking the
// "Show contributions" toggle expands the strip into a full list with the
// task each specialist took on. The chip palette reuses the cream theme:
// accent border for passed validation, warn border for the rare flagged
// step (challenger fired) or a validation failure.

function _stepTone(s: PipelineStep): string {
  if (s.validation_passed === false) {
    return "border-warn/40 bg-warn/10 text-warn";
  }
  if (s.challenger_signal) {
    return "border-warn/40 bg-warn/10 text-ink";
  }
  return "border-accent/30 bg-accent/10 text-ink";
}

function PipelineAttribution({ steps }: { steps: PipelineStep[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      data-testid="message-pipeline-attribution"
      className="mb-2 -mt-0.5"
    >
      <div className="flex items-center gap-1 flex-wrap">
        {steps.map((s) => (
          <span
            key={`${s.step}-${s.agent}`}
            data-testid={`pipeline-chip-${s.step}`}
            className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium ${_stepTone(s)}`}
            title={s.task || s.agent}
          >
            <span className="opacity-60">{s.step}.</span>
            <span className="truncate max-w-[10rem]">{s.agent}</span>
          </span>
        ))}
        <button
          type="button"
          className="text-[11px] text-ink-faint hover:text-ink underline-offset-2 hover:underline"
          onClick={() => setOpen((o) => !o)}
          data-testid="pipeline-attribution-toggle"
        >
          {open ? "Hide contributions" : "Show contributions"}
        </button>
      </div>
      {open && (
        <ol
          data-testid="pipeline-attribution-detail"
          className="mt-1.5 space-y-1 text-[11px] text-ink-dim list-none"
        >
          {steps.map((s) => (
            <li
              key={`detail-${s.step}-${s.agent}`}
              className="rounded-md border border-line bg-bg-1 px-2 py-1"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-ink">
                  {s.step}. {s.agent}
                  {s.validation_passed === false && (
                    <span className="ml-1.5 text-warn">· validation failed</span>
                  )}
                  {s.challenger_signal && (
                    <span className="ml-1.5 text-warn">· flagged by reviewer</span>
                  )}
                </span>
                <span className="text-ink-faint tabular-nums">
                  {typeof s.tokens === "number" && `${s.tokens.toLocaleString()} tok`}
                  {typeof s.duration_ms === "number" && (
                    <> · {(s.duration_ms / 1000).toFixed(1)}s</>
                  )}
                </span>
              </div>
              {s.task && (
                <div className="mt-0.5 text-ink-dim line-clamp-2 break-words">
                  {s.task}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
