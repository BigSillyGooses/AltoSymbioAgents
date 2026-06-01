// desktop-ui/components/design/artifactParser.ts
//
// Streaming parser for <artifact identifier="..." type="..." title="...">…
// </artifact> tags emitted by an agent in Design Studio mode. Ported
// line-for-line from Open Design's apps/web/src/artifacts/parser.ts so a
// future upstream refresh stays mechanical — the ArtifactEvent union is
// intentionally preserved.
//
// Feed deltas in, iterate events. Handles one artifact at a time, ignores
// nesting, and holds back enough bytes at a chunk boundary to detect a
// partial open ("<art") or close ("</artifa") tag split across deltas.

export type ArtifactEvent =
  | { type: "text"; delta: string }
  | { type: "artifact:start"; identifier: string; artifactType: string; title: string }
  | { type: "artifact:chunk"; identifier: string; delta: string }
  | { type: "artifact:end"; identifier: string; fullContent: string };

const OPEN_PREFIX = "<artifact";
const CLOSE_TAG = "</artifact>";

interface ParserState {
  inside: boolean;
  buffer: string;
  identifier: string;
  artifactType: string;
  title: string;
  content: string;
}

function parseAttrs(raw: string): Record<string, string> {
  const re = /(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
  const out: Record<string, string> = {};
  let m: RegExpExecArray | null = re.exec(raw);
  while (m !== null) {
    out[m[1] as string] = (m[2] ?? m[3] ?? "") as string;
    m = re.exec(raw);
  }
  return out;
}

type OpenTagMatch =
  | { kind: "complete"; start: number; end: number; attrs: string }
  | { kind: "partial"; start: number }
  | { kind: "none" };

function findOpenTag(buffer: string): OpenTagMatch {
  let from = 0;
  while (from <= buffer.length) {
    const idx = buffer.indexOf(OPEN_PREFIX, from);
    if (idx === -1) {
      // Maybe a strict prefix at the tail (e.g. "<art") — hold back.
      const tail = buffer.lastIndexOf("<");
      if (tail !== -1) {
        const slice = buffer.slice(tail);
        if (OPEN_PREFIX.startsWith(slice) && slice.length < OPEN_PREFIX.length) {
          return { kind: "partial", start: tail };
        }
      }
      return { kind: "none" };
    }

    const after = idx + OPEN_PREFIX.length;
    const next = buffer.charAt(after);
    if (next === "") return { kind: "partial", start: idx };
    if (!/\s/.test(next)) {
      // Not a real <artifact ...> open (e.g. "<artifactual"). Keep scanning.
      from = after;
      continue;
    }

    // Quote-aware scan for the closing '>'.
    let i = after;
    let quote: '"' | "'" | null = null;
    while (i < buffer.length) {
      const c = buffer.charAt(i);
      if (quote !== null) {
        if (c === quote) quote = null;
      } else if (c === '"' || c === "'") {
        quote = c;
      } else if (c === ">") {
        return { kind: "complete", start: idx, end: i + 1, attrs: buffer.slice(after, i) };
      }
      i++;
    }
    return { kind: "partial", start: idx };
  }
  return { kind: "none" };
}

export function createArtifactParser() {
  const state: ParserState = {
    inside: false,
    buffer: "",
    identifier: "",
    artifactType: "",
    title: "",
    content: "",
  };

  function* feed(delta: string): Generator<ArtifactEvent> {
    state.buffer += delta;

    while (state.buffer.length > 0) {
      if (!state.inside) {
        const open = findOpenTag(state.buffer);
        if (open.kind === "none") {
          yield { type: "text", delta: state.buffer };
          state.buffer = "";
          return;
        }
        if (open.kind === "partial") {
          if (open.start > 0) {
            yield { type: "text", delta: state.buffer.slice(0, open.start) };
            state.buffer = state.buffer.slice(open.start);
          }
          return;
        }
        if (open.start > 0) {
          yield { type: "text", delta: state.buffer.slice(0, open.start) };
        }
        const attrs = parseAttrs(open.attrs);
        state.inside = true;
        state.identifier = attrs["identifier"] ?? "";
        state.artifactType = attrs["type"] ?? "";
        state.title = attrs["title"] ?? "";
        state.content = "";
        state.buffer = state.buffer.slice(open.end);
        yield {
          type: "artifact:start",
          identifier: state.identifier,
          artifactType: state.artifactType,
          title: state.title,
        };
        continue;
      }

      const closeIdx = state.buffer.indexOf(CLOSE_TAG);
      if (closeIdx === -1) {
        // Hold back enough bytes to detect a partial close tag at the tail.
        const flushUpTo = state.buffer.length - (CLOSE_TAG.length - 1);
        if (flushUpTo > 0) {
          const chunk = state.buffer.slice(0, flushUpTo);
          state.content += chunk;
          state.buffer = state.buffer.slice(flushUpTo);
          yield { type: "artifact:chunk", identifier: state.identifier, delta: chunk };
        }
        return;
      }
      const finalChunk = state.buffer.slice(0, closeIdx);
      if (finalChunk.length > 0) {
        state.content += finalChunk;
        yield { type: "artifact:chunk", identifier: state.identifier, delta: finalChunk };
      }
      yield { type: "artifact:end", identifier: state.identifier, fullContent: state.content };
      state.buffer = state.buffer.slice(closeIdx + CLOSE_TAG.length);
      state.inside = false;
      state.identifier = "";
      state.artifactType = "";
      state.title = "";
      state.content = "";
    }
  }

  function* flush(): Generator<ArtifactEvent> {
    if (state.inside) {
      if (state.buffer.length > 0) {
        state.content += state.buffer;
        yield { type: "artifact:chunk", identifier: state.identifier, delta: state.buffer };
        state.buffer = "";
      }
      yield { type: "artifact:end", identifier: state.identifier, fullContent: state.content };
    } else if (state.buffer.length > 0) {
      yield { type: "text", delta: state.buffer };
    }
    state.buffer = "";
    state.inside = false;
  }

  return { feed, flush };
}

// ── Whole-message helper ────────────────────────────────────────────────────
//
// MessageRenderer works on the full accumulated buffer each render (not a
// delta stream), so it needs a one-shot split into ordered segments. Feeding
// the whole string once then flushing yields exactly that: interleaved text
// and artifact segments in source order. Mid-stream (before </artifact>
// arrives) the open artifact is surfaced via flush() as a not-yet-closed
// segment so the preview can render incrementally.

export type MessageSegment =
  | { kind: "text"; text: string }
  | {
      kind: "artifact";
      identifier: string;
      artifactType: string;
      title: string;
      content: string;
      closed: boolean;
    };

export function splitMessage(content: string): MessageSegment[] {
  const parser = createArtifactParser();
  const segments: MessageSegment[] = [];
  let textBuf = "";
  let current: Extract<MessageSegment, { kind: "artifact" }> | null = null;

  const flushText = () => {
    if (textBuf.length > 0) {
      segments.push({ kind: "text", text: textBuf });
      textBuf = "";
    }
  };

  // `fromFlush` distinguishes a real </artifact> close (seen during feed) from
  // the synthetic end flush() emits for an artifact whose closing tag hasn't
  // streamed in yet. Only a real close marks the segment `closed` (which is
  // what gates export in the UI), so a mid-stream artifact stays open.
  const consume = (event: ArtifactEvent, fromFlush: boolean) => {
    switch (event.type) {
      case "text":
        textBuf += event.delta;
        break;
      case "artifact:start":
        flushText();
        current = {
          kind: "artifact",
          identifier: event.identifier,
          artifactType: event.artifactType,
          title: event.title,
          content: "",
          closed: false,
        };
        segments.push(current);
        break;
      case "artifact:chunk":
        if (current) current.content += event.delta;
        break;
      case "artifact:end":
        if (current) {
          current.content = event.fullContent;
          current.closed = !fromFlush;
          current = null;
        }
        break;
    }
  };

  for (const event of parser.feed(content)) consume(event, false);
  for (const event of parser.flush()) consume(event, true);
  flushText();
  return segments;
}
