import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

vi.mock("@/api/client", () => ({
  Chat: {
    list: vi.fn(),
    setConversationRoster: vi.fn(),
  },
  Teams: {
    get: vi.fn(),
  },
}));

import { Chat, Teams } from "@/api/client";
import { useAppStore } from "@/stores/appStore";

import { useRoster } from "./useRoster";

const RESET_STATE = useAppStore.getState();

beforeEach(() => {
  useAppStore.setState({ toasts: [] }, false);
  vi.mocked(Chat.list).mockReset();
  vi.mocked(Chat.list).mockResolvedValue([]);
  vi.mocked(Chat.setConversationRoster).mockReset();
  vi.mocked(Chat.setConversationRoster).mockResolvedValue({
    ok: true,
    agent_id: "agent-1",
    team_id: null,
  } as never);
  vi.mocked(Teams.get).mockReset();
});

afterEach(() => {
  cleanup();
  useAppStore.setState(RESET_STATE, true);
});

function renderRoster(overrides: {
  activeId?: string;
  activeAgentId?: string | null;
  activeTeamId?: string | null;
} = {}) {
  const onLocalUpdate = vi.fn();
  const onRollback = vi.fn().mockResolvedValue(undefined);
  const view = renderHook(() =>
    useRoster({
      activeId: overrides.activeId ?? "",
      activeAgentId: overrides.activeAgentId ?? null,
      activeTeamId: overrides.activeTeamId ?? null,
      onLocalUpdate,
      onRollback,
    }),
  );
  return { ...view, onLocalUpdate, onRollback };
}

describe("useRoster — derived current bindings", () => {
  it("returns empty strings and empty pendingRoster on first mount", () => {
    const { result } = renderRoster();
    expect(result.current.pendingRoster).toEqual({ agentIds: [] });
    expect(result.current.currentAgentId).toBe("");
    expect(result.current.currentTeamId).toBe("");
  });

  it("with no activeId, surfaces a single pending agent id as currentAgentId", () => {
    const { result } = renderRoster();
    act(() => {
      result.current.setPendingRoster({ agentIds: ["a-1"] });
    });
    expect(result.current.currentAgentId).toBe("a-1");
    expect(result.current.currentTeamId).toBe("");
  });

  it("with no activeId, multi-agent pending does NOT leak into currentAgentId", () => {
    const { result } = renderRoster();
    act(() => {
      result.current.setPendingRoster({ agentIds: ["a-1", "a-2"] });
    });
    expect(result.current.currentAgentId).toBe("");
  });

  it("with no activeId, pending teamId surfaces as currentTeamId", () => {
    const { result } = renderRoster();
    act(() => {
      result.current.setPendingRoster({ agentIds: [], teamId: "t-1" });
    });
    expect(result.current.currentTeamId).toBe("t-1");
  });

  it("with activeId, reads bindings from activeAgentId/activeTeamId props", () => {
    const { result } = renderRoster({
      activeId: "c-1",
      activeAgentId: "row-agent",
      activeTeamId: null,
    });
    expect(result.current.currentAgentId).toBe("row-agent");
    expect(result.current.currentTeamId).toBe("");
  });
});

describe("useRoster — applyRoster", () => {
  it("with no activeId, stashes the pick into pendingRoster (no network)", async () => {
    const { result } = renderRoster();
    await act(async () => {
      await result.current.applyRoster({ agentIds: ["new"] });
    });
    expect(result.current.pendingRoster).toEqual({ agentIds: ["new"] });
    expect(Chat.setConversationRoster).not.toHaveBeenCalled();
  });

  it("with activeId + agentIds, calls Chat.setConversationRoster and onLocalUpdate", async () => {
    vi.mocked(Chat.setConversationRoster).mockResolvedValue({
      ok: true,
      agent_id: "applied-agent",
      team_id: null,
    } as never);
    const { result, onLocalUpdate } = renderRoster({ activeId: "c-1" });
    await act(async () => {
      await result.current.applyRoster({ agentIds: ["x", "y"] });
    });
    expect(Chat.setConversationRoster).toHaveBeenCalledWith("c-1", ["x", "y"]);
    expect(onLocalUpdate).toHaveBeenCalledWith("applied-agent", null);
  });

  it("with teamId, resolves members via Teams.get and passes team_id through", async () => {
    vi.mocked(Teams.get).mockResolvedValue({
      members: [{ id: "m1" }, { id: "m2" }],
    } as never);
    vi.mocked(Chat.setConversationRoster).mockResolvedValue({
      ok: true,
      agent_id: null,
      team_id: "t-saved",
    } as never);
    const { result, onLocalUpdate } = renderRoster({ activeId: "c-1" });
    await act(async () => {
      await result.current.applyRoster({ agentIds: [], teamId: "t-saved" });
    });
    expect(Teams.get).toHaveBeenCalledWith("t-saved");
    expect(Chat.setConversationRoster).toHaveBeenCalledWith(
      "c-1", ["m1", "m2"], "t-saved",
    );
    expect(onLocalUpdate).toHaveBeenCalledWith(null, "t-saved");
  });

  it("with teamId but no members, fails with a friendly toast and rolls back", async () => {
    vi.mocked(Teams.get).mockResolvedValue({ members: [] } as never);
    const { result, onLocalUpdate, onRollback } = renderRoster({ activeId: "c-1" });
    await act(async () => {
      await result.current.applyRoster({ agentIds: [], teamId: "t-empty" });
    });
    expect(Chat.setConversationRoster).not.toHaveBeenCalled();
    expect(onLocalUpdate).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(useAppStore.getState().toasts.some(
        (t) => t.text === "Selected team has no members",
      )).toBe(true);
    });
    expect(onRollback).toHaveBeenCalled();
  });

  it("on Chat.setConversationRoster failure, surfaces toast + calls onRollback", async () => {
    vi.mocked(Chat.setConversationRoster).mockRejectedValue(
      new Error("backend down"),
    );
    const { result, onLocalUpdate, onRollback } = renderRoster({ activeId: "c-1" });
    await act(async () => {
      await result.current.applyRoster({ agentIds: ["a"] });
    });
    expect(onLocalUpdate).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(useAppStore.getState().toasts.some(
        (t) => t.text === "backend down",
      )).toBe(true);
    });
    expect(onRollback).toHaveBeenCalled();
  });
});
