// desktop-ui/components/chat/WebSourceChips.tsx
//
// Shared renderer for web-research source chips. Both the live streaming
// bubble (WebFetchStatus in ChatView) and the persisted assistant message
// (MessageBubble, reloaded from SQLite) render the *same* clickable chips
// through this component, so the live view and the saved view can't drift.
//
// Each chip shows the page title (falling back to the host) and opens the
// real URL in the user's browser when clicked — the only technical detail a
// non-technical user ever sees is "where this came from".

import { System } from "@/api/client";
import type { WebSource } from "@/components/chat/deriveWebFetches";

export function WebSourceChips({ sources }: { sources: WebSource[] }) {
  if (sources.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {sources.map((s: WebSource) => (
        <button
          key={s.url}
          type="button"
          title={s.url}
          onClick={() => System.openUrl(s.url).catch(() => {})}
          className="inline-flex items-center gap-1 rounded-full border border-line bg-bg-2/60 px-2 py-0.5 text-ink-dim hover:text-ink"
        >
          <span aria-hidden>🌐</span>
          <span className="max-w-[16rem] truncate">{s.title}</span>
        </button>
      ))}
    </div>
  );
}
