// desktop-ui/components/chat/DevinTimeline.tsx
//
// Drillable variant of the thinking timeline. Sibling of ThinkingTimeline:
// same ThinkingRow[] input, same row-state palette, same accessibility
// posture (aria-live="polite" on the list, role="status" on each row),
// but each row is a button that toggles an expanded panel underneath.
//
// Collapsed: same compact one-liner ThinkingTimeline renders (icon + label
// + line-clamped detail).
// Expanded: detail rendered without line-clamp, plus the un-truncated
// `expandedDetail` narrative when the originating event carried more text
// than the collapsed `detail` could fit. Whitespace-pre-wrap so multi-line
// reader-output / reasoning previews render legibly.

import { useCallback, useState } from "react";

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

export function DevinTimeline({ rows }: { rows: ThinkingRow[] }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = useCallback((key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  return (
    <ol
      data-testid="chat-stream-timeline-drillable"
      className="mb-2 space-y-1 text-[11px] list-none"
      aria-live="polite"
      aria-atomic="false"
    >
      {rows.map((r) => {
        const open       = expanded.has(r.key);
        const panelId    = `devin-row-panel-${r.key}`;
        const fullText   = r.expandedDetail ?? r.detail;
        // A row is drillable when there's something the panel can show.
        // Non-drillable rows (e.g. challenger_started, which carries only
        // a label) render as plain rows without a chevron or button — a
        // disclosure affordance that doesn't disclose anything is worse
        // than no affordance.
        const drillable  = !!fullText;
        const showPanel  = drillable && open;
        if (!drillable) {
          return (
            <li
              key={r.key}
              role="status"
              data-testid={`thinking-row-${r.key}`}
              className={`rounded-md border px-2 py-1 ${rowTone(r.state)}`}
            >
              <div className="flex items-center gap-1.5">
                <span aria-hidden="true" className="inline-block w-3" />
                <span aria-hidden="true">{r.icon}</span>
                <span className="font-medium">{r.label}</span>
              </div>
            </li>
          );
        }
        return (
          <li
            key={r.key}
            role="status"
            data-testid={`thinking-row-${r.key}`}
            className={`rounded-md border ${rowTone(r.state)}`}
          >
            <button
              type="button"
              aria-expanded={open}
              aria-controls={panelId}
              onClick={() => toggle(r.key)}
              data-testid={`thinking-row-toggle-${r.key}`}
              className="w-full text-left px-2 py-1 flex flex-col gap-0.5 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-md"
            >
              <div className="flex items-center gap-1.5">
                <span
                  aria-hidden="true"
                  className={`inline-block w-3 transition-transform ${open ? "rotate-90" : ""}`}
                >
                  ▸
                </span>
                <span aria-hidden="true">{r.icon}</span>
                <span className="font-medium">{r.label}</span>
              </div>
              {r.detail && !open && (
                <div className="text-ink-dim line-clamp-2 break-words pl-[1.125rem]">
                  {r.detail}
                </div>
              )}
            </button>
            {showPanel && (
              <div
                id={panelId}
                data-testid={`thinking-row-panel-${r.key}`}
                className="px-2 pb-1 pl-[1.625rem] text-ink-dim break-words whitespace-pre-wrap"
              >
                {fullText}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
