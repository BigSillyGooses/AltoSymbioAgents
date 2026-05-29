// desktop-ui/components/chat/useRoster.ts
//
// useRoster() owns the conversation's agent/team binding from ChatView's
// perspective: what the RosterPicker pill displays, what the user has
// queued for the next-new conversation, and the apply path that talks to
// the backend.
//
// Kept dependency-light by accepting bind-shape callbacks from the caller
// instead of consuming ChatView's ConversationRow type directly. The caller
// owns the conversation list state and decides how to mutate it; this hook
// only triggers the backend write and reports the local outcome.

import { useState } from "react";

import { Chat, Teams } from "@/api/client";
import { useAppStore } from "@/stores/appStore";
import type { RosterPick } from "@/components/RosterPicker";

export interface UseRosterArgs {
  /** Id of the active conversation, or "" when none is bound yet. */
  activeId: string;
  /** Agent id stored on the active conversation row (null when unbound). */
  activeAgentId: string | null;
  /** Team id stored on the active conversation row (null when unbound). */
  activeTeamId: string | null;
  /**
   * Optimistic local update after a successful backend write: caller patches
   * the active conversation's agent_id / team_id in its local list state.
   */
  onLocalUpdate: (agentId: string | null, teamId: string | null) => void;
  /**
   * Called when the backend write fails so the caller can re-fetch the
   * authoritative conversation list and roll back the optimistic patch.
   */
  onRollback: () => Promise<void> | void;
}

export interface UseRosterResult {
  /** The roster the user has lined up for the next new conversation. */
  pendingRoster: RosterPick;
  setPendingRoster: (next: RosterPick) => void;
  /**
   * What the RosterPicker pill should show as the current agent id. For an
   * active conversation, reads the stored binding; otherwise reflects the
   * pending pick when it's a single-agent selection.
   */
  currentAgentId: string;
  /** Same idea as currentAgentId, for the team binding. */
  currentTeamId: string;
  /** Commit a roster pick: stash when no conversation, write when there is one. */
  applyRoster: (pick: RosterPick) => Promise<void>;
}

export function useRoster(args: UseRosterArgs): UseRosterResult {
  const { activeId, activeAgentId, activeTeamId, onLocalUpdate, onRollback } =
    args;
  const pushToast = useAppStore((s) => s.pushToast);

  const [pendingRoster, setPendingRoster] = useState<RosterPick>({
    agentIds: [],
  });

  const currentAgentId = activeId
    ? (activeAgentId ?? "")
    : (pendingRoster.agentIds.length === 1 && !pendingRoster.teamId
        ? pendingRoster.agentIds[0]
        : "");
  const currentTeamId = activeId
    ? (activeTeamId ?? "")
    : (pendingRoster.teamId ?? "");

  const applyRoster = async (pick: RosterPick): Promise<void> => {
    if (!activeId) {
      // No conversation yet — stash the pick and apply on next new chat.
      setPendingRoster(pick);
      return;
    }
    try {
      let result: { agent_id: string | null; team_id: string | null };
      if (pick.teamId) {
        // Picking a saved team preset: hand the team_id over directly so
        // the backend skips the find_or_create_adhoc_team detour and binds
        // straight to the saved row. Without the override the orchestrator
        // could rebind to a coincidentally-matching ad-hoc team or create
        // a duplicate ad-hoc copy of the saved team (Phase 4 fix).
        const team = (await Teams.get(pick.teamId)) as {
          members?: { id: string }[];
        };
        const memberIds = (team?.members ?? []).map((m) => m.id);
        if (memberIds.length === 0) {
          throw new Error("Selected team has no members");
        }
        const rsp = await Chat.setConversationRoster(
          activeId, memberIds, pick.teamId,
        );
        result = { agent_id: rsp.agent_id, team_id: rsp.team_id };
      } else {
        const rsp = await Chat.setConversationRoster(activeId, pick.agentIds);
        result = { agent_id: rsp.agent_id, team_id: rsp.team_id };
      }
      onLocalUpdate(result.agent_id, result.team_id);
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Could not change roster",
      });
      await onRollback();
    }
  };

  return {
    pendingRoster,
    setPendingRoster,
    currentAgentId,
    currentTeamId,
    applyRoster,
  };
}
