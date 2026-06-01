import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// System.openUrl is the only api/client surface MessageBubble reaches (via the
// shared WebSourceChips). MessageRenderer pulls in mermaid/katex/highlight.js,
// so stub it to a plain content node to keep this a focused unit test.
const openUrl = vi.fn((_url: string) => Promise.resolve());
vi.mock("@/api/client", () => ({
  System: { openUrl: (url: string) => openUrl(url) },
}));
vi.mock("@/components/MessageRenderer", () => ({
  MessageRenderer: ({ content }: { content: string }) => <div>{content}</div>,
}));

import { MessageBubble, type MessageRow } from "./MessageBubble";

afterEach(() => {
  cleanup();
  openUrl.mockClear();
});

function row(overrides: Partial<MessageRow> = {}): MessageRow {
  return {
    id: "m1",
    role: "assistant",
    content: "hello",
    ...overrides,
  };
}

describe("MessageBubble web sources", () => {
  it("renders a Sources row with a clickable chip from an array", async () => {
    render(
      <MessageBubble
        msg={row({
          web_sources: [{ url: "https://example.com/a", title: "Example A" }],
        })}
        voiceOutputEnabled={false}
      />,
    );
    expect(screen.getByTestId("message-web-sources")).toBeTruthy();
    expect(screen.getByText("Sources")).toBeTruthy();
    const chip = screen.getByText("Example A");
    await userEvent.click(chip);
    expect(openUrl).toHaveBeenCalledWith("https://example.com/a");
  });

  it("parses web_sources delivered as a JSON string", () => {
    render(
      <MessageBubble
        msg={row({
          web_sources: JSON.stringify([
            { url: "https://docs.site/x", title: "Docs" },
          ]),
        })}
        voiceOutputEnabled={false}
      />,
    );
    expect(screen.getByTestId("message-web-sources")).toBeTruthy();
    expect(screen.getByText("Docs")).toBeTruthy();
  });

  it("falls back to the host when a source has no title", () => {
    render(
      <MessageBubble
        msg={row({ web_sources: [{ url: "https://news.example.org/p", title: "" }] })}
        voiceOutputEnabled={false}
      />,
    );
    expect(screen.getByText("news.example.org")).toBeTruthy();
  });

  it("renders no Sources row when there are none", () => {
    render(<MessageBubble msg={row()} voiceOutputEnabled={false} />);
    expect(screen.queryByTestId("message-web-sources")).toBeNull();
  });

  it("ignores malformed web_sources without throwing", () => {
    render(
      <MessageBubble
        msg={row({ web_sources: "{not json" })}
        voiceOutputEnabled={false}
      />,
    );
    expect(screen.queryByTestId("message-web-sources")).toBeNull();
  });

  // Regression guard for the both-fix: persisted pipeline chips must also
  // render now that the read path delivers the decoded `pipeline_steps`.
  it("still renders persisted pipeline attribution chips", () => {
    render(
      <MessageBubble
        msg={row({
          pipeline_steps: [
            { step: 1, agent: "Researcher", validation_passed: true },
          ],
          web_sources: [{ url: "https://example.com", title: "Src" }],
        })}
        voiceOutputEnabled={false}
      />,
    );
    expect(screen.getByTestId("message-pipeline-attribution")).toBeTruthy();
    expect(screen.getByTestId("message-web-sources")).toBeTruthy();
  });
});
