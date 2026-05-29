import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { Timeline } from "./Timeline";
import type { ThinkingRow } from "./deriveThinkingTimeline";

afterEach(() => {
  cleanup();
});

const ROWS: ThinkingRow[] = [
  {
    key: "r1",
    state: "info",
    icon: "🎯",
    label: "Routed",
    detail: "short detail",
    expandedDetail: "the full untruncated narrative",
  },
];

describe("Timeline dispatcher", () => {
  it("renders the compact ThinkingTimeline when variant is 'compact'", () => {
    render(<Timeline rows={ROWS} variant="compact" />);
    // ThinkingTimeline uses chat-stream-timeline; DevinTimeline uses
    // chat-stream-timeline-drillable. The compact picker picks the former.
    expect(screen.getByTestId("chat-stream-timeline")).toBeTruthy();
    expect(screen.queryByTestId("chat-stream-timeline-drillable")).toBeNull();
    // Compact has no button affordance — no chevron / button per row.
    expect(screen.queryByTestId("thinking-row-toggle-r1")).toBeNull();
  });

  it("renders the drillable DevinTimeline when variant is 'drillable'", () => {
    render(<Timeline rows={ROWS} variant="drillable" />);
    expect(screen.getByTestId("chat-stream-timeline-drillable")).toBeTruthy();
    expect(screen.queryByTestId("chat-stream-timeline")).toBeNull();
    // Drillable rows render a toggle button when the row carries any text
    // that can expand (detail or expandedDetail).
    expect(screen.getByTestId("thinking-row-toggle-r1")).toBeTruthy();
  });

  it("forwards the rows array unchanged to both variants", () => {
    const { rerender } = render(<Timeline rows={ROWS} variant="compact" />);
    expect(screen.getByText("Routed")).toBeTruthy();
    rerender(<Timeline rows={ROWS} variant="drillable" />);
    expect(screen.getByText("Routed")).toBeTruthy();
  });

  it("renders an empty list when there are no rows in either variant", () => {
    const { rerender } = render(<Timeline rows={[]} variant="compact" />);
    expect(screen.getByTestId("chat-stream-timeline").children).toHaveLength(0);
    rerender(<Timeline rows={[]} variant="drillable" />);
    expect(
      screen.getByTestId("chat-stream-timeline-drillable").children,
    ).toHaveLength(0);
  });
});
