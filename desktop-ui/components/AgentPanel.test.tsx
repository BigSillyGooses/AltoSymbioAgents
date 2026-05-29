import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AgentPanel } from "./AgentPanel";
import { useAgents } from "@/components/chat/queries";
import { useAppStore } from "@/stores/appStore";

// A fresh QueryClient per test prevents cross-test cache leakage. Retries are
// off so a transient mock failure surfaces immediately, and gc/staleTime are 0
// so an invalidation always refetches rather than serving a cached payload.
function makeTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
    },
  });
}

// AgentPanel reads agents through the shared chat/queries.ts hooks, which import
// Agents/Chat/Teams from the client at module load — so the factory has to
// enumerate them even though this test only drives the Agents surface.
vi.mock("@/api/client", () => ({
  Agents: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    performance: vi.fn(),
  },
  Chat: { list: vi.fn() },
  Teams: { list: vi.fn() },
}));

import { Agents } from "@/api/client";

// Stands in for RosterPicker / ChatView: an independent consumer of the same
// ["agents"] cache. It must observe the new agent after AgentPanel invalidates
// that key — that propagation is the whole point of the change.
function RosterProbe() {
  const q = useAgents({ enabled: true });
  return <div data-testid="probe">{(q.data ?? []).length}</div>;
}

const READY_STATUS = { status: "ready" as const, port: 1234, token: "t" };
const NO_PERF = {
  agent_id: "a1",
  total_interactions: 0,
  alignment_rate: 0,
  period: "7d",
};
const RESET_STATE = useAppStore.getState();

beforeEach(() => {
  // replace=false merges, so the store keeps its real actions (pushToast etc.).
  useAppStore.setState({ sidecarStatus: READY_STATUS, toasts: [] }, false);
  vi.mocked(Agents.list).mockResolvedValue([
    { id: "a1", name: "Researcher", description: "finds things" },
  ]);
  vi.mocked(Agents.performance).mockResolvedValue(NO_PERF);
  vi.mocked(Agents.create).mockResolvedValue({ id: "a2", name: "Reviewer" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useAppStore.setState(RESET_STATE, true);
});

describe("AgentPanel — shared agents cache", () => {
  it("creating an agent invalidates the shared ['agents'] cache so other consumers refresh", async () => {
    const user = userEvent.setup();
    const qc = makeTestQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <AgentPanel />
        <RosterProbe />
      </QueryClientProvider>,
    );

    // First load: one agent, visible in the panel and counted by the probe.
    await screen.findByText("Researcher");
    await waitFor(() =>
      expect(screen.getByTestId("probe").textContent).toBe("1"),
    );
    const callsAfterLoad = vi.mocked(Agents.list).mock.calls.length;

    // The backend now has a second agent; the create succeeds.
    vi.mocked(Agents.list).mockResolvedValue([
      { id: "a1", name: "Researcher", description: "finds things" },
      { id: "a2", name: "Reviewer", description: "reviews" },
    ]);

    await user.click(screen.getByTestId("agent-new"));
    await user.type(screen.getByTestId("agent-form-name"), "Reviewer");
    await user.click(screen.getByTestId("agent-form-save"));

    // Invalidation triggers a refetch of the shared key, and BOTH the panel and
    // the independent probe converge on the two-agent roster — without the
    // probe ever calling reload() itself.
    await waitFor(() =>
      expect(vi.mocked(Agents.list).mock.calls.length).toBeGreaterThan(
        callsAfterLoad,
      ),
    );
    await screen.findByText("Reviewer");
    await waitFor(() =>
      expect(screen.getByTestId("probe").textContent).toBe("2"),
    );
  });
});
