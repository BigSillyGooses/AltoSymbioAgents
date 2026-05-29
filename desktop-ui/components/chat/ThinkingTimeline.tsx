// desktop-ui/components/chat/ThinkingTimeline.tsx
//
// Thinking-timeline rows.
//
// Renders one row per non-chip lifecycle event (see deriveThinkingTimeline).
// Sits between the live pipeline chips and the buffer/spinner inside the
// streaming bubble, which is already wrapped in aria-live="polite", so each
// new row is announced by screen readers. role="status" on every row makes
// the announcement explicit even if the row is re-rendered outside the
// streaming bubble in the future.

import type { ThinkingRow, ThinkingRowState } from "./deriveThinkingTimeline";

function rowTone(state: ThinkingRowState): string {
  switch (state) {
    case "error":
      return "border-err/40 bg-err/10 text-err";
    case "warn":
      return "border-warn/40 bg-warn/10 text-ink";
    case "ok":
      return "border-accent/30 bg-accent/10 text-ink";
    default:
      return "border-line bg-bg-1 text-ink-dim";
  }
}

export function ThinkingTimeline({ rows }: { rows: ThinkingRow[] }) {
  return (
    <ol
      data-testid="chat-stream-timeline"
      className="mb-2 space-y-1 text-[11px] list-none"
      aria-live="polite"
      aria-atomic="false"
    >
      {rows.map((r) => (
        <li
          key={r.key}
          role="status"
          data-testid={`thinking-row-${r.key}`}
          className={`rounded-md border px-2 py-1 ${rowTone(r.state)}`}
        >
          <div className="flex items-center gap-1.5">
            <span aria-hidden="true">{r.icon}</span>
            <span className="font-medium">{r.label}</span>
          </div>
          {r.detail && (
            <div className="mt-0.5 text-ink-dim line-clamp-2 break-words">
              {r.detail}
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}
