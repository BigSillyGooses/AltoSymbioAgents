// desktop-ui/components/chat/Timeline.tsx
//
// Dispatcher between the two timeline renders. The streaming chat bubble
// renders this single component instead of branching at the call site;
// the variant comes from the user's `timeline_variant` setting and is
// threaded down by ChatView. Unknown values fall back to the compact
// render so a forward-incompatible setting can never blank the bubble.

import type { ThinkingRow } from "./deriveThinkingTimeline";
import { ThinkingTimeline } from "./ThinkingTimeline";
import { DevinTimeline } from "./DevinTimeline";

export type TimelineVariant = "compact" | "drillable";

export function Timeline(
  { rows, variant }: { rows: ThinkingRow[]; variant: TimelineVariant },
) {
  if (variant === "drillable") {
    return <DevinTimeline rows={rows} />;
  }
  return <ThinkingTimeline rows={rows} />;
}
