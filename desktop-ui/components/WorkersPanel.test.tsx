import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { WorkersPanel } from "./WorkersPanel";
import { useAppStore } from "@/stores/appStore";

vi.mock("@/api/client", () => ({
  Workers: {
    list: vi.fn(),
    tasks: vi.fn(),
    run: vi.fn(),
  },
}));

import { Workers } from "@/api/client";

const READY_STATUS = { status: "ready" as const, port: 1234, token: "tok" };

const WORKERS = [
  { name: "reindex", description: "Embed pending records." },
  { name: "memory_audit", description: "Audit memory tiers." },
  { name: "trajectory_report", description: "Per-agent success rates." },
];

const TASKS = [
  {
    id: "t1",
    worker: "reindex",
    status: "done" as const,
    params: {},
    result: { indexed: 3 },
    error: null,
    progress: 1,
    created_at: "2026-06-01T00:00:00Z",
    started_at: "2026-06-01T00:00:00Z",
    finished_at: "2026-06-01T00:00:01Z",
  },
];

const RESET_STATE = useAppStore.getState();

beforeEach(() => {
  useAppStore.setState({ sidecarStatus: READY_STATUS, toasts: [] }, false);
  vi.mocked(Workers.list).mockReset().mockResolvedValue({ workers: WORKERS });
  vi.mocked(Workers.tasks).mockReset().mockResolvedValue({ tasks: TASKS });
  vi.mocked(Workers.run).mockReset().mockResolvedValue({ ok: true, task_id: "x" });
});

afterEach(() => {
  cleanup();
  useAppStore.setState(RESET_STATE, true);
});

describe("WorkersPanel", () => {
  it("lists the registered workers and recent tasks", async () => {
    render(<WorkersPanel />);
    await waitFor(() => expect(Workers.list).toHaveBeenCalled());
    // "reindex" appears in both a worker card and the recent-task row.
    expect(screen.getAllByText("reindex").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("memory_audit")).toBeTruthy();
    expect(screen.getByText("trajectory_report")).toBeTruthy();
    // Recent task result is rendered.
    await waitFor(() => expect(screen.getByText(/"indexed": 3/)).toBeTruthy());
  });

  it("running a worker calls the API and refreshes", async () => {
    render(<WorkersPanel />);
    await waitFor(() => expect(Workers.list).toHaveBeenCalledTimes(1));

    const runButtons = screen.getAllByText("Run");
    await userEvent.click(runButtons[0]);

    await waitFor(() => expect(Workers.run).toHaveBeenCalledWith("reindex"));
    // refresh() re-fetches after a successful run.
    await waitFor(() => expect(Workers.list).toHaveBeenCalledTimes(2));
  });
});
