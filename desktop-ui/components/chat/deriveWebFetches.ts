// desktop-ui/components/chat/deriveWebFetches.ts
//
// Projects the SSE event log into the web-research view the streaming bubble
// renders: a live "Reading <host>…" status while a page is being fetched, plus
// a list of fetched pages (source chips). Driven entirely by the `web_fetch`
// events the backend emits from WebAPI.web_fetch_to_rag and the orchestrator's
// auto-fetch hook — single-agent turns with web research off emit none, so the
// reducer stays empty and adds no overhead.

import type { StreamingEvent } from "./events";

export interface WebSource {
  url: string;
  title: string;
}

export interface WebFetchesLive {
  // URL currently being fetched (latest "fetching" with no terminal event yet).
  active: string | null;
  // Pages successfully fetched + indexed this turn.
  sources: WebSource[];
}

interface WebFetchEvent {
  status?: "fetching" | "done" | "error" | "blocked";
  url?: string;
  title?: string;
  error?: string;
}

/** Friendly host label for a URL (falls back to the raw string). */
export function hostLabel(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

export function deriveWebFetches(events: StreamingEvent[]): WebFetchesLive {
  let active: string | null = null;
  const sources = new Map<string, WebSource>();

  for (const evt of events) {
    if (evt.type !== "web_fetch") continue;
    const data = evt.data as WebFetchEvent;
    const url = data?.url || "";
    if (!url) continue;
    if (data.status === "fetching") {
      active = url;
    } else if (data.status === "done") {
      if (active === url) active = null;
      sources.set(url, { url, title: (data.title || "").trim() || hostLabel(url) });
    } else if (data.status === "error" || data.status === "blocked") {
      if (active === url) active = null;
    }
  }

  return { active, sources: Array.from(sources.values()) };
}
