import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { ThinkingTimeline } from "./ThinkingTimeline";
import type { ThinkingRow } from "./deriveThinkingTimeline";

afterEach(() => {
  cleanup();
});

function row(overrides: Partial<ThinkingRow> = {}): ThinkingRow {
  return {
    key: "k",
    state: "info",
    icon: "ℹ",
    label: "Label",
    ...overrides,
  };
}

describe("ThinkingTimeline", () => {
  it("renders an aria-live polite list with each row as role=status", () => {
    render(
      <ThinkingTimeline
        rows={[
          row({ key: "a", label: "First" }),
          row({ key: "b", label: "Second" }),
        ]}
      />,
    );
    const list = screen.getByTestId("chat-stream-timeline");
    expect(list.tagName).toBe("OL");
    expect(list.getAttribute("aria-live")).toBe("polite");
    expect(screen.getAllByRole("status")).toHaveLength(2);
    expect(screen.getByText("First")).toBeTruthy();
    expect(screen.getByText("Second")).toBeTruthy();
  });

  it("renders icon next to label and optional detail", () => {
    render(
      <ThinkingTimeline
        rows={[row({ icon: "🎯", label: "Routed", detail: "Long detail" })]}
      />,
    );
    expect(screen.getByText("🎯")).toBeTruthy();
    expect(screen.getByText("Routed")).toBeTruthy();
    expect(screen.getByText("Long detail")).toBeTruthy();
  });

  it("omits the detail node when detail is undefined", () => {
    render(<ThinkingTimeline rows={[row({ label: "No detail" })]} />);
    // The <li> has two children in the with-detail case (icon row + detail
    // div); we assert the no-detail case yields one direct child.
    const li = screen.getAllByRole("status")[0];
    expect(li.children).toHaveLength(1);
  });

  it("applies state-specific palette classes", () => {
    render(
      <ThinkingTimeline
        rows={[
          row({ key: "ok", state: "ok",    label: "ok" }),
          row({ key: "wa", state: "warn",  label: "warn" }),
          row({ key: "er", state: "error", label: "error" }),
          row({ key: "in", state: "info",  label: "info" }),
        ]}
      />,
    );
    const items = screen.getAllByRole("status");
    expect(items[0].className).toContain("bg-accent/10");
    expect(items[1].className).toContain("bg-warn/10");
    expect(items[2].className).toContain("bg-err/10");
    expect(items[3].className).toContain("bg-bg-1");
  });

  it("renders an empty <ol> for no rows", () => {
    render(<ThinkingTimeline rows={[]} />);
    expect(screen.getByTestId("chat-stream-timeline").children).toHaveLength(0);
  });
});
