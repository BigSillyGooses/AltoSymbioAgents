import { useEffect, useMemo, useRef, useState } from "react";

import { Teams, type TeamRow } from "@/api/client";
import { useAppStore } from "@/stores/appStore";
import { useAgents, useTeams } from "@/components/chat/queries";

export interface AgentRow {
  id: string;
  name: string;
  description?: string;
  role?: string;
  model_preference?: string;
  is_builtin?: boolean | number;
}

// What the parent (ChatView) needs to apply the pick.
//   agentIds = []  → clear binding
//   agentIds = [x] → solo agent
//   agentIds = 2+  → ad-hoc team
//   teamId set     → preset team (overrides agentIds)
export interface RosterPick {
  agentIds: string[];
  teamId?: string;
}

interface Props {
  // The current binding from the conversation: an agent id, a team id, or
  // neither. Empty strings = unset.
  agentId: string;
  teamId: string;
  // Apply the user's selection. Resolves once persisted; the parent will
  // typically refresh the conversation row from the response.
  onApply: (pick: RosterPick) => Promise<void>;
  disabled?: boolean;
}

export function RosterPicker({ agentId, teamId, onApply, disabled = false }: Props) {
  const sidecarReady = useAppStore((s) => s.sidecarStatus?.status === "ready");
  const pushToast = useAppStore((s) => s.pushToast);

  const agentsQuery = useAgents({ enabled: sidecarReady });
  const teamsQuery = useTeams({ enabled: sidecarReady });

  const agents = useMemo<AgentRow[]>(
    () => (agentsQuery.data ?? []) as AgentRow[],
    [agentsQuery.data],
  );
  const teams = useMemo<TeamRow[]>(
    () => (teamsQuery.data ?? []).filter((t) => !t.is_adhoc),
    [teamsQuery.data],
  );

  const [open, setOpen] = useState(false);
  // Local in-popover selection — committed only on Apply so transient
  // multi-clicks don't thrash the backend.
  const [draft, setDraft] = useState<Set<string>>(new Set());
  const [draftTeamId, setDraftTeamId] = useState<string>("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [savingTeam, setSavingTeam] = useState(false);
  const [saveName, setSaveName] = useState("");

  const containerRef = useRef<HTMLDivElement | null>(null);

  // When the picker opens, seed the draft from the current binding so the
  // user sees what's already applied and can edit it in place.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setSavingTeam(false);
    setSaveName("");
    setDraftTeamId(teamId || "");
    if (teamId) {
      // Hydrate the draft from the team's members so the chips reflect it.
      Teams.get(teamId)
        .then((rsp) => {
          const members = (rsp as { members?: { id: string }[] })?.members ?? [];
          setDraft(new Set(members.map((m) => m.id)));
        })
        .catch(() => setDraft(new Set()));
    } else if (agentId) {
      setDraft(new Set([agentId]));
    } else {
      setDraft(new Set());
    }
  }, [open, agentId, teamId]);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const activeAgent = useMemo(
    () => agents.find((a) => a.id === agentId),
    [agents, agentId],
  );
  const activeTeam = useMemo(
    () => teams.find((t) => t.id === teamId),
    [teams, teamId],
  );

  const pillLabel = (() => {
    if (teamId) return activeTeam?.name ?? "Team";
    if (agentId) return activeAgent?.name ?? "Agent";
    return "No agent";
  })();

  const pillIcon = teamId ? "◆" : "●";

  const filtered = query.trim()
    ? agents.filter((a) => {
        const q = query.trim().toLowerCase();
        return (
          a.name.toLowerCase().includes(q) ||
          (a.description ?? "").toLowerCase().includes(q) ||
          (a.role ?? "").toLowerCase().includes(q)
        );
      })
    : agents;

  const toggleAgent = (id: string) => {
    // Clicking an agent clears any preset-team draft — the user is now
    // building an ad-hoc roster.
    setDraftTeamId("");
    setDraft((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const pickTeam = (tid: string) => {
    setDraftTeamId(tid);
    // Hydrate chip state from members for visual feedback.
    Teams.get(tid)
      .then((rsp) => {
        const members = (rsp as { members?: { id: string }[] })?.members ?? [];
        setDraft(new Set(members.map((m) => m.id)));
      })
      .catch(() => {});
  };

  // Mirror backend coordinator-pick (agent_registry.py:840): first agent with
  // role == "coordinator", else the first selected agent. When the draft is a
  // saved-team preset we surface the team's stored coordinator_id verbatim.
  const coordinatorId = useMemo<string | null>(() => {
    if (draftTeamId) {
      const t = teams.find((x) => x.id === draftTeamId);
      return t?.coordinator_id ?? null;
    }
    if (draft.size < 2) return null;
    const draftAgents = agents.filter((a) => draft.has(a.id));
    if (draftAgents.length === 0) return null;
    const explicit = draftAgents.find(
      (a) => (a.role ?? "").toLowerCase() === "coordinator",
    );
    return (explicit ?? draftAgents[0]).id;
  }, [draft, draftTeamId, agents, teams]);

  const coordinatorName = useMemo(() => {
    if (!coordinatorId) return null;
    return agents.find((a) => a.id === coordinatorId)?.name ?? null;
  }, [coordinatorId, agents]);

  const apply = async () => {
    setBusy(true);
    try {
      const ids = Array.from(draft);
      // If a saved team was picked AND no chips were tweaked, route via teamId
      // so the conversation binds to the saved preset rather than spawning
      // an ad-hoc duplicate.
      if (draftTeamId && ids.length >= 2) {
        await onApply({ agentIds: [], teamId: draftTeamId });
      } else {
        await onApply({ agentIds: ids });
      }
      setOpen(false);
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Could not apply roster",
      });
    } finally {
      setBusy(false);
    }
  };

  // Save-as-team flow: only meaningful when the conversation already binds
  // to an ad-hoc team (teamId set + activeTeam is undefined, meaning the
  // saved-teams list doesn't include it because is_adhoc=1).
  const canSaveCurrent =
    !!teamId && !teams.find((t) => t.id === teamId);

  const saveCurrentAsTeam = async () => {
    if (!teamId || !saveName.trim()) return;
    setBusy(true);
    try {
      await Teams.saveAdhoc(teamId, saveName.trim());
      pushToast({ kind: "success", text: `Saved "${saveName.trim()}"` });
      setSavingTeam(false);
      setSaveName("");
      await teamsQuery.refetch();
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Save failed",
      });
    } finally {
      setBusy(false);
    }
  };

  const draftSize = draft.size;
  const draftSummary = (() => {
    if (draftTeamId) {
      const t = teams.find((x) => x.id === draftTeamId);
      return t ? `Preset team: ${t.name}` : "Preset team";
    }
    if (draftSize === 0) return "Smart routing";
    if (draftSize === 1) {
      const onlyId = Array.from(draft)[0];
      return agents.find((a) => a.id === onlyId)?.name ?? "1 agent";
    }
    return `Team of ${draftSize}`;
  })();

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled || !sidecarReady}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-full
                   bg-bg-1 text-ink hover:shadow-soft-2 shadow-soft-1 transition
                   disabled:opacity-50 disabled:cursor-not-allowed"
        aria-haspopup="dialog"
        aria-expanded={open}
        data-testid="roster-picker-pill"
        title={activeTeam?.description ?? activeAgent?.description ?? "Pick agents or a team"}
      >
        <span className="text-accent" aria-hidden>{pillIcon}</span>
        <span className="truncate max-w-[200px]">{pillLabel}</span>
        <svg
          width="12" height="12" viewBox="0 0 12 12" fill="none"
          stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
          strokeLinejoin="round" aria-hidden
          className={`transition-transform ${open ? "rotate-180" : ""}`}
        >
          <polyline points="3 5 6 8 9 5" />
        </svg>
      </button>

      {open && (
        <div
          className="absolute right-0 top-full mt-2 w-[32rem] max-h-[78vh] overflow-y-auto
                     glass z-30 p-3"
          role="dialog"
          aria-label="Compose roster"
          data-testid="roster-picker-dropdown"
        >
          <header className="flex items-center justify-between gap-2 mb-2">
            <div className="text-sm font-medium">Compose roster</div>
            {canSaveCurrent && !savingTeam && (
              <button
                type="button"
                className="btn-ghost text-xs"
                onClick={() => setSavingTeam(true)}
                data-testid="roster-picker-save-current"
              >
                Save current as team…
              </button>
            )}
          </header>

          {savingTeam && (
            <div className="rounded-md border border-line bg-bg-2 p-2 mb-2 space-y-2">
              <input
                type="text"
                className="input"
                placeholder="Team name (e.g. Research squad)"
                value={saveName}
                onChange={(e) => setSaveName(e.target.value)}
                autoFocus
                data-testid="roster-picker-save-name"
              />
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  onClick={() => setSavingTeam(false)}
                  disabled={busy}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn-primary text-xs"
                  onClick={saveCurrentAsTeam}
                  disabled={busy || !saveName.trim()}
                  data-testid="roster-picker-save-commit"
                >
                  Save
                </button>
              </div>
            </div>
          )}

          {teams.length > 0 && (
            <section className="mb-3" aria-labelledby="roster-saved-teams-label">
              <div
                id="roster-saved-teams-label"
                className="px-1 pb-1 text-[10px] uppercase tracking-wide text-ink-faint"
              >
                Saved teams
              </div>
              <div className="flex flex-wrap gap-1.5">
                {teams.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => pickTeam(t.id)}
                    className={`px-2.5 py-1 text-xs rounded-full transition
                      ${draftTeamId === t.id
                        ? "bg-accent/20 ring-1 ring-accent text-ink"
                        : "bg-bg-2 hover:bg-bg-3 text-ink-faint"}`}
                    title={t.description}
                    data-testid={`roster-picker-team-${t.id}`}
                    aria-pressed={draftTeamId === t.id}
                  >
                    <span aria-hidden>◆ </span>
                    {t.name}
                  </button>
                ))}
              </div>
            </section>
          )}

          <section aria-labelledby="roster-agents-label">
            <div className="flex items-center justify-between gap-2 mb-1">
              <div
                id="roster-agents-label"
                className="px-1 text-[10px] uppercase tracking-wide text-ink-faint"
              >
                Agents · click to add or remove
              </div>
            </div>

            <input
              type="text"
              placeholder="Search agents…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="input mb-2"
              data-testid="roster-picker-search"
            />

            <div
              role="group"
              aria-label="Available agents"
              className="grid grid-cols-1 sm:grid-cols-2 gap-1.5"
            >
              <AgentChipCard
                agent={null}
                selected={draftSize === 0 && !draftTeamId}
                isCoordinator={false}
                onClick={() => {
                  setDraft(new Set());
                  setDraftTeamId("");
                }}
              />
              {filtered.length === 0 && agents.length === 0 ? (
                <div className="col-span-full px-3 py-2 text-xs text-ink-faint">
                  No agents yet.
                </div>
              ) : filtered.length === 0 ? (
                <div className="col-span-full px-3 py-2 text-xs text-ink-faint">
                  No matches.
                </div>
              ) : (
                filtered.map((a) => (
                  <AgentChipCard
                    key={a.id}
                    agent={a}
                    selected={draft.has(a.id)}
                    isCoordinator={a.id === coordinatorId}
                    onClick={() => toggleAgent(a.id)}
                  />
                ))
              )}
            </div>
          </section>

          <footer className="flex items-center justify-between gap-2 mt-3 pt-2 border-t border-line/40">
            <span
              className="text-[11px] text-ink-faint"
              data-testid="roster-picker-summary"
            >
              {coordinatorName
                ? `Coordinator: ${coordinatorName} · ${draftSummary}`
                : draftSummary}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                className="btn-ghost text-xs"
                onClick={() => setOpen(false)}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary text-xs"
                onClick={apply}
                disabled={busy}
                data-testid="roster-picker-apply"
              >
                {busy ? "Applying…" : "Apply"}
              </button>
            </div>
          </footer>
        </div>
      )}
    </div>
  );
}

// ── Agent chip card ──────────────────────────────────────────────────────────

interface AgentChipCardProps {
  agent: AgentRow | null;
  selected: boolean;
  isCoordinator: boolean;
  onClick: () => void;
}

function AgentChipCard({
  agent,
  selected,
  isCoordinator,
  onClick,
}: AgentChipCardProps) {
  const name = agent?.name ?? "No agent";
  const initials = agent
    ? agent.name
        .split(/\s+/)
        .map((w) => w[0])
        .join("")
        .slice(0, 2)
        .toUpperCase()
    : "•";
  const desc =
    agent?.description ??
    "Fall back to smart routing with the default system prompt.";

  return (
    <button
      type="button"
      onClick={onClick}
      role="checkbox"
      aria-checked={selected}
      className={`text-left p-2 rounded-lg border transition flex items-start gap-2
                  ${selected
                    ? "bg-accent/10 border-accent shadow-soft-1"
                    : "bg-bg-1 border-line hover:bg-bg-2"}`}
      data-testid={agent ? `roster-picker-agent-${agent.id}` : "roster-picker-agent-none"}
    >
      <span
        className={`shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-full text-[11px] font-semibold
                    ${selected
                      ? "bg-accent text-white"
                      : "bg-bg-2 text-ink-faint"}`}
        aria-hidden
      >
        {initials}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5 flex-wrap">
          <span className="text-sm font-medium truncate">{name}</span>
          {isCoordinator && (
            <span
              className="pill text-[9px] uppercase tracking-wide bg-accent/15 text-accent"
              data-testid="roster-picker-coordinator-badge"
            >
              Coordinator
            </span>
          )}
          {agent?.role && !isCoordinator && (
            <span className="pill text-[10px]">{agent.role}</span>
          )}
        </span>
        {desc && (
          <span className="block text-[11px] text-ink-faint mt-0.5 line-clamp-2">
            {desc}
          </span>
        )}
      </span>
    </button>
  );
}
