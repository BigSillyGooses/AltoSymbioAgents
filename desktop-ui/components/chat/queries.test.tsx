import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  renderHook,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({
  Chat: { list: vi.fn() },
  Agents: { list: vi.fn() },
  Teams: { list: vi.fn() },
}));

import { Agents, Chat, Teams } from "@/api/client";

import {
  agentNameMap,
  queryKeys,
  teamNameMap,
  useAgents,
  useConversations,
  useTeams,
} from "./queries";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
    },
  });
}

function wrapWithClient(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(Chat.list).mockReset();
  vi.mocked(Agents.list).mockReset();
  vi.mocked(Teams.list).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("queryKeys", () => {
  it("conversations key includes the limit so different limits share no cache", () => {
    expect(queryKeys.conversations(50)).toEqual(["conversations", 50]);
    expect(queryKeys.conversations(10)).toEqual(["conversations", 10]);
  });
  it("agents and teams keys are stable singletons", () => {
    expect(queryKeys.agents()).toEqual(["agents"]);
    expect(queryKeys.teams()).toEqual(["teams"]);
  });
});

describe("agentNameMap / teamNameMap", () => {
  it("builds a tolerant lookup from a partial-shape list", () => {
    expect(agentNameMap([
      { id: "a-1", name: "First" },
      { id: "a-2", name: "" },     // empty name → "Agent" fallback
      { name: "no-id" },           // missing id → skipped
      { id: "a-4" },               // missing name → "Agent"
    ])).toEqual({
      "a-1": "First",
      "a-2": "Agent",
      "a-4": "Agent",
    });
  });
  it("returns {} for undefined / empty input", () => {
    expect(agentNameMap(undefined)).toEqual({});
    expect(agentNameMap([])).toEqual({});
    expect(teamNameMap(undefined)).toEqual({});
  });
  it("teams fall back to 'Team' label when name is missing", () => {
    expect(teamNameMap([
      { id: "t-1", name: "Squad", is_adhoc: 0 } as never,
      { id: "t-2", name: "", is_adhoc: 0 } as never,
    ])).toEqual({ "t-1": "Squad", "t-2": "Team" });
  });
});

describe("useConversations", () => {
  it("calls Chat.list with the requested limit and returns rows", async () => {
    vi.mocked(Chat.list).mockResolvedValue([
      { id: "c-1", title: "first" },
    ] as never);
    const { result } = renderHook(
      () => useConversations({ enabled: true, limit: 25 }),
      { wrapper: wrapWithClient(makeClient()) },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(Chat.list).toHaveBeenCalledWith(25);
    expect(result.current.data).toEqual([{ id: "c-1", title: "first" }]);
  });

  it("does not call Chat.list when enabled=false", async () => {
    const { result } = renderHook(
      () => useConversations({ enabled: false }),
      { wrapper: wrapWithClient(makeClient()) },
    );
    // Give the effect a tick.
    await waitFor(() => expect(result.current.isPending).toBe(true));
    expect(Chat.list).not.toHaveBeenCalled();
  });
});

describe("useAgents / useTeams swallow fetch errors", () => {
  it("useAgents returns [] when Agents.list rejects (don't crash the panel)", async () => {
    vi.mocked(Agents.list).mockRejectedValue(new Error("boom"));
    const { result } = renderHook(
      () => useAgents({ enabled: true }),
      { wrapper: wrapWithClient(makeClient()) },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
  });

  it("useTeams returns [] when Teams.list rejects", async () => {
    vi.mocked(Teams.list).mockRejectedValue(new Error("nope"));
    const { result } = renderHook(
      () => useTeams({ enabled: true }),
      { wrapper: wrapWithClient(makeClient()) },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
  });
});

describe("cache sharing across mounts", () => {
  it("a fresh-cache mount of useConversations reuses the existing entry", async () => {
    vi.mocked(Chat.list).mockResolvedValue([{ id: "c-1" }] as never);
    // Non-zero staleTime so the second mount sees a fresh cache entry
    // (rather than a "stale, refetch in background" entry that bumps the
    // call count). Mirrors the production client's staleTime contract.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 30_000 } },
    });
    const w = wrapWithClient(client);

    const a = renderHook(() => useConversations({ enabled: true }), { wrapper: w });
    await waitFor(() => expect(a.result.current.isSuccess).toBe(true));
    expect(Chat.list).toHaveBeenCalledTimes(1);

    const b = renderHook(() => useConversations({ enabled: true }), { wrapper: w });
    await waitFor(() => expect(b.result.current.isSuccess).toBe(true));
    // Second mount served from cache — no additional Chat.list call.
    expect(Chat.list).toHaveBeenCalledTimes(1);
    expect(b.result.current.data).toEqual([{ id: "c-1" }]);
  });
});
