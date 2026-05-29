// desktop-ui/components/chat/queries.ts
//
// TanStack Query hooks for the three lists ChatView reads on every mount:
// conversations, agents, teams. Centralising the keys and fetch fns here
// lets callers share the cache and lets mutations elsewhere invalidate by
// key without duplicating string literals.
//
// The renderer talks to a loopback sidecar so the network is either up or
// down; we keep retry minimal and let staleTime smooth over the brief
// window in which AgentPanel mutations haven't yet pushed back.

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { Agents, Chat, Teams, type TeamRow } from "@/api/client";

export const queryKeys = {
  conversations: (limit: number) => ["conversations", limit] as const,
  agents:        ()              => ["agents"]              as const,
  teams:         ()              => ["teams"]               as const,
};

export interface ConversationRow {
  id: string;
  title?: string;
  agent_id?: string | null;
  team_id?: string | null;
  updated_at?: string;
}

export interface AgentRow {
  id?: string;
  name?: string;
}

export function useConversations(args: {
  enabled: boolean;
  limit?: number;
}): UseQueryResult<ConversationRow[]> {
  const limit = args.limit ?? 50;
  return useQuery({
    queryKey: queryKeys.conversations(limit),
    queryFn: async (): Promise<ConversationRow[]> => {
      return (await Chat.list(limit)) as ConversationRow[];
    },
    enabled: args.enabled,
  });
}

export function useAgents(args: {
  enabled: boolean;
}): UseQueryResult<AgentRow[]> {
  return useQuery({
    queryKey: queryKeys.agents(),
    queryFn: async (): Promise<AgentRow[]> => {
      try {
        return (await Agents.list()) as AgentRow[];
      } catch {
        return [];
      }
    },
    enabled: args.enabled,
  });
}

export function useTeams(args: {
  enabled: boolean;
}): UseQueryResult<TeamRow[]> {
  return useQuery({
    queryKey: queryKeys.teams(),
    queryFn: async (): Promise<TeamRow[]> => {
      try {
        return (await Teams.list()) as TeamRow[];
      } catch {
        return [];
      }
    },
    enabled: args.enabled,
  });
}

/**
 * Build a quick lookup of agent id → display name from the result of
 * useAgents(). Centralised here so the conversation list, RosterPicker and
 * any future panel share the same fallback string ("Agent") if a row is
 * missing its name.
 */
export function agentNameMap(rows: AgentRow[] | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const a of rows ?? []) {
    if (a?.id) out[a.id] = a.name || "Agent";
  }
  return out;
}

export function teamNameMap(rows: TeamRow[] | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const t of rows ?? []) {
    if (t?.id) out[t.id] = t.name || "Team";
  }
  return out;
}
