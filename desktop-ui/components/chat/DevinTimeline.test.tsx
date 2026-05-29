import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { DevinTimeline } from "./DevinTimeline";
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

describe("DevinTimeline", () => {
  it("renders an aria-live polite list with each row as role=status", () => {
    render(
      <DevinTimeline
        rows={[
          row({ key: "a", label: "First" }),
          row({ key: "b", label: "Second" }),
        ]}
      />,
    );
    const list = screen.getByTestId("chat-stream-timeline-drillable");
    expect(list.tagName).toBe("OL");
    expect(list.getAttribute("aria-live")).toBe("polite");
    expect(screen.getAllByRole("status")).toHaveLength(2);
    expect(screen.getByText("First")).toBeTruthy();
    expect(screen.getByText("Second")).toBeTruthy();
  });

  it("renders each row as a toggle button starting collapsed", () => {
    render(
      <DevinTimeline
        rows={[row({ key: "a", label: "Routed", detail: "complex" })]}
      />,
    );
    const btn = screen.getByTestId("thinking-row-toggle-a");
    expect(btn.tagName).toBe("BUTTON");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    // Collapsed: detail renders inside the button as the line-clamped summary.
    expect(screen.getByText("complex")).toBeTruthy();
    // No expanded panel.
    expect(screen.queryByTestId("thinking-row-panel-a")).toBeNull();
  });

  it("expands on click and shows expandedDetail in the panel", () => {
    render(
      <DevinTimeline
        rows={[row({
          key: "a",
          label: "Routed",
          detail: "complex · confidence 90% · short",
          expandedDetail: "Full reasoning text that was truncated previously.",
        })]}
      />,
    );
    const btn = screen.getByTestId("thinking-row-toggle-a");
    fireEvent.click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    const panel = screen.getByTestId("thinking-row-panel-a");
    expect(panel.textContent).toBe(
      "Full reasoning text that was truncated previously.",
    );
    // Collapsed summary is hidden once open; expanded panel is the new view.
    expect(screen.queryByText("complex · confidence 90% · short")).toBeNull();
  });

  it("falls back to detail in the panel when expandedDetail is absent", () => {
    render(
      <DevinTimeline
        rows={[row({ key: "a", label: "Recalled", detail: "3 facts · 2 chunks" })]}
      />,
    );
    fireEvent.click(screen.getByTestId("thinking-row-toggle-a"));
    const panel = screen.getByTestId("thinking-row-panel-a");
    expect(panel.textContent).toBe("3 facts · 2 chunks");
    // Without line-clamp on the panel.
    expect(panel.className).not.toContain("line-clamp");
  });

  it("collapses again on a second click", () => {
    render(
      <DevinTimeline
        rows={[row({ key: "a", label: "Routed", detail: "x", expandedDetail: "y" })]}
      />,
    );
    const btn = screen.getByTestId("thinking-row-toggle-a");
    fireEvent.click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("thinking-row-panel-a")).toBeNull();
  });

  it("expands rows independently", () => {
    render(
      <DevinTimeline
        rows={[
          row({ key: "a", label: "A", expandedDetail: "deep A" }),
          row({ key: "b", label: "B", expandedDetail: "deep B" }),
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("thinking-row-toggle-a"));
    expect(screen.getByTestId("thinking-row-toggle-a").getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("thinking-row-toggle-b").getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByTestId("thinking-row-panel-a").textContent).toBe("deep A");
    expect(screen.queryByTestId("thinking-row-panel-b")).toBeNull();
  });

  it("renders non-drillable rows as plain non-button items (no chevron, no toggle)", () => {
    // Rows like challenger_started carry only label/icon — no detail and no
    // expandedDetail. They should not render a disclosure affordance.
    render(
      <DevinTimeline
        rows={[row({ key: "a", label: "Reviewer checking step 2" })]}
      />,
    );
    expect(screen.queryByTestId("thinking-row-toggle-a")).toBeNull();
    expect(screen.getByTestId("thinking-row-a")).toBeTruthy();
    // No chevron character should appear for non-drillable rows.
    expect(screen.queryByText("▸")).toBeNull();
    expect(screen.getByText("Reviewer checking step 2")).toBeTruthy();
  });

  it("a row with only short detail is still drillable (button + chevron + panel)", () => {
    // Even when expandedDetail is absent, the line-clamp removal on expand
    // is a meaningful disclosure — the panel preserves the toggle parity.
    render(
      <DevinTimeline
        rows={[row({ key: "a", label: "Recalled", detail: "3 facts" })]}
      />,
    );
    const btn = screen.getByTestId("thinking-row-toggle-a");
    expect(btn.tagName).toBe("BUTTON");
    expect(btn.getAttribute("aria-controls")).toBe("devin-row-panel-a");
  });

  it("applies state-specific palette classes", () => {
    render(
      <DevinTimeline
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
    render(<DevinTimeline rows={[]} />);
    expect(
      screen.getByTestId("chat-stream-timeline-drillable").children,
    ).toHaveLength(0);
  });

  it("intermixes drillable and non-drillable rows preserving order", () => {
    render(
      <DevinTimeline
        rows={[
          row({ key: "a", label: "First",  detail: "with detail" }),
          row({ key: "b", label: "Bare" }),
          row({ key: "c", label: "Third",  expandedDetail: "deep" }),
        ]}
      />,
    );
    const items = screen.getAllByRole("status");
    expect(items).toHaveLength(3);
    // Two drillable buttons, one plain row in the middle.
    expect(screen.getByTestId("thinking-row-toggle-a")).toBeTruthy();
    expect(screen.queryByTestId("thinking-row-toggle-b")).toBeNull();
    expect(screen.getByTestId("thinking-row-toggle-c")).toBeTruthy();
  });

  it("uses aria-controls to link the toggle button to its panel id", () => {
    render(
      <DevinTimeline
        rows={[row({ key: "abc", label: "L", expandedDetail: "rich" })]}
      />,
    );
    const btn = screen.getByTestId("thinking-row-toggle-abc");
    expect(btn.getAttribute("aria-controls")).toBe("devin-row-panel-abc");
    fireEvent.click(btn);
    const panel = screen.getByTestId("thinking-row-panel-abc");
    expect(panel.getAttribute("id")).toBe("devin-row-panel-abc");
  });
});
