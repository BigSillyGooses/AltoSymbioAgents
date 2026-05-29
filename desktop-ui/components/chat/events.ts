// desktop-ui/components/chat/events.ts
//
// Shared type for one SSE event record as the renderer sees it after
// useAppStore.streamingEvents has decoded the line. Reducers downstream
// (derivePipelineLive, deriveThinkingTimeline, …) take StreamingEvent[]
// and project a UI-shaped view over the log.

export interface StreamingEvent {
  type: string;
  data: unknown;
  at:   number;
}
