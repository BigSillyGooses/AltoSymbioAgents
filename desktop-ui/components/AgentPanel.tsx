import { useEffect, useState } from "react";

import { Agents, type AgentPerformance } from "@/api/client";
import { useAppStore } from "@/stores/appStore";

interface AgentRow {
  id: string;
  name: string;
  description?: string;
  system_prompt?: string;
  role?: string;
  model_preference?: string;
  temperature?: number;
  max_tokens?: number;
  is_builtin?: boolean | number;
}

interface AgentFormState {
  name: string;
  description: string;
  system_prompt: string;
  model_preference: string;
  temperature: string;
  max_tokens: string;
}

const EMPTY_FORM: AgentFormState = {
  name: "",
  description: "",
  system_prompt: "You are a helpful AI assistant.",
  model_preference: "auto",
  temperature: "0.7",
  max_tokens: "4096",
};

const MODEL_PREFERENCES = [
  { value: "auto",   label: "Auto (let the router decide)" },
  { value: "claude", label: "Claude (cloud)" },
  { value: "local",  label: "Local model" },
];

export function AgentPanel() {
  const ready = useAppStore((s) => s.sidecarStatus?.status === "ready");
  const pushToast = useAppStore((s) => s.pushToast);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [loading, setLoading] = useState(true);
  // null = closed; "new" = create mode; agent id = edit mode.
  const [editing, setEditing] = useState<string | null>(null);

  const reload = async () => {
    try {
      const rows = (await Agents.list()) as AgentRow[];
      setAgents(rows ?? []);
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Could not load agents",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!ready) return;
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready]);

  const handleDelete = async (a: AgentRow) => {
    if (a.is_builtin) return;
    const ok = window.confirm(
      `Delete "${a.name}"? Conversations using this agent will fall back to default routing. This cannot be undone.`,
    );
    if (!ok) return;
    try {
      await Agents.delete(a.id);
      pushToast({ kind: "success", text: `Deleted ${a.name}` });
      await reload();
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Delete failed",
      });
    }
  };

  return (
    <div className="p-6 overflow-y-auto h-full">
      <header className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Agents</h1>
          <p className="text-sm text-ink-dim">
            Define personas, model preferences, and budgets. Builtin agents
            can't be deleted.
          </p>
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={() => setEditing("new")}
          data-testid="agent-new"
        >
          + New agent
        </button>
      </header>

      {loading && <div className="text-ink-faint text-sm">Loading…</div>}

      {!loading && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {agents.map((a) => (
            <div key={a.id} className="card group relative">
              <div className="flex items-center justify-between mb-1">
                <h3 className="font-semibold">{a.name}</h3>
                <div className="flex items-center gap-1">
                  {a.is_builtin ? (
                    <span className="pill">Builtin</span>
                  ) : (
                    <span className="pill text-accent border-accent/40">
                      Custom
                    </span>
                  )}
                </div>
              </div>
              <p className="text-sm text-ink-dim mb-2 line-clamp-3">
                {a.description}
              </p>
              <div className="flex items-center gap-2 flex-wrap">
                {a.model_preference && (
                  <span className="pill text-[10px]">
                    model: {a.model_preference}
                  </span>
                )}
                {a.role && (
                  <span className="pill text-[10px]">role: {a.role}</span>
                )}
                <AgentPerformancePill agentId={a.id} />
              </div>
              <div className="absolute right-2 top-2 flex gap-1 opacity-0 group-hover:opacity-100 transition">
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  onClick={() => setEditing(a.id)}
                  data-testid={`agent-edit-${a.id}`}
                >
                  Edit
                </button>
                {!a.is_builtin && (
                  <button
                    type="button"
                    className="btn-ghost text-xs text-err"
                    onClick={() => handleDelete(a)}
                    data-testid={`agent-delete-${a.id}`}
                  >
                    Delete
                  </button>
                )}
              </div>
            </div>
          ))}
          {!agents.length && (
            <div className="text-ink-faint text-sm">No agents yet.</div>
          )}
        </div>
      )}

      {editing !== null && (
        <AgentEditor
          agent={editing === "new" ? null : agents.find((a) => a.id === editing) ?? null}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await reload();
          }}
        />
      )}
    </div>
  );
}

// ── Phase 3: 7-day alignment performance pill ─────────────────────────────────
//
// Fetches GET /agents/{id}/performance per card. Cheap (a single GROUP BY on
// agent_performance) and lazy — failures fall back to "no data" so the card
// still renders. The hex palette mirrors the cream theme: accent for the
// healthy band (≥90% aligned), warn for the marginal band, err for clearly
// drifting agents.

function _toneFor(rate: number): string {
  if (rate >= 0.9) return "border-accent/40 bg-accent/10 text-ink";
  if (rate >= 0.7) return "border-warn/40 bg-warn/10 text-ink";
  return "border-err/40 bg-err/10 text-err";
}

function AgentPerformancePill({ agentId }: { agentId: string }) {
  const [data, setData] = useState<AgentPerformance | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    Agents.performance(agentId)
      .then((res) => {
        if (alive) setData(res);
      })
      .catch(() => {
        /* leave data null; the pill will show "no data" */
      })
      .finally(() => {
        if (alive) setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [agentId]);

  if (!loaded) {
    return <span className="pill text-[10px] text-ink-faint">7d: …</span>;
  }
  if (!data || data.total_interactions === 0) {
    return (
      <span
        className="pill text-[10px] text-ink-faint"
        title="No alignment signal in the last 7 days"
        data-testid={`agent-performance-${agentId}`}
      >
        7d: no data
      </span>
    );
  }
  const pct = Math.round(data.alignment_rate * 100);
  return (
    <span
      className={`pill text-[10px] ${_toneFor(data.alignment_rate)}`}
      title={`${pct}% aligned across ${data.total_interactions} interactions (last 7 days)`}
      data-testid={`agent-performance-${agentId}`}
    >
      7d: {pct}% aligned · {data.total_interactions}
    </span>
  );
}

// ── Modal editor ──────────────────────────────────────────────────────────────

interface AgentEditorProps {
  agent: AgentRow | null;
  onClose: () => void;
  onSaved: () => void;
}

function AgentEditor({ agent, onClose, onSaved }: AgentEditorProps) {
  const pushToast = useAppStore((s) => s.pushToast);
  const [form, setForm] = useState<AgentFormState>(() => {
    if (!agent) return EMPTY_FORM;
    return {
      name: agent.name ?? "",
      description: agent.description ?? "",
      system_prompt: agent.system_prompt ?? "",
      model_preference: agent.model_preference ?? "auto",
      temperature: String(agent.temperature ?? 0.7),
      max_tokens: String(agent.max_tokens ?? 4096),
    };
  });
  const [busy, setBusy] = useState(false);

  const isEdit = !!agent;
  const isBuiltin = !!agent?.is_builtin;

  const submit = async () => {
    if (!form.name.trim()) {
      pushToast({ kind: "error", text: "Name is required" });
      return;
    }
    setBusy(true);
    try {
      const temperature = parseFloat(form.temperature) || 0.7;
      const max_tokens = parseInt(form.max_tokens, 10) || 4096;
      if (isEdit && agent) {
        await Agents.update(agent.id, {
          name: form.name.trim(),
          description: form.description,
          system_prompt: form.system_prompt,
          model_preference: form.model_preference,
          temperature,
          max_tokens,
        });
        pushToast({ kind: "success", text: `Saved ${form.name}` });
      } else {
        await Agents.create({
          name: form.name.trim(),
          description: form.description,
          system_prompt: form.system_prompt,
          model_preference: form.model_preference,
          temperature,
          max_tokens,
        });
        pushToast({ kind: "success", text: `Created ${form.name}` });
      }
      onSaved();
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Save failed",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/30"
      onClick={onClose}
    >
      <div
        className="glass w-full max-w-xl max-h-[90vh] overflow-y-auto p-6"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={isEdit ? "Edit agent" : "Create agent"}
      >
        <header className="mb-4">
          <h2 className="text-lg font-semibold">
            {isEdit ? `Edit ${agent?.name}` : "New agent"}
          </h2>
          {isBuiltin && (
            <p className="text-xs text-ink-faint mt-1">
              Builtin agents can be edited but not deleted.
            </p>
          )}
        </header>

        <div className="space-y-3">
          <div>
            <label className="label">Name</label>
            <input
              className="input"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="Researcher, Code Reviewer, etc."
              data-testid="agent-form-name"
            />
          </div>

          <div>
            <label className="label">Description</label>
            <input
              className="input"
              value={form.description}
              onChange={(e) =>
                setForm({ ...form, description: e.target.value })
              }
              placeholder="One-line summary of what this agent is for."
              data-testid="agent-form-description"
            />
          </div>

          <div>
            <label className="label">System prompt</label>
            <textarea
              className="input min-h-[120px] font-mono text-xs"
              value={form.system_prompt}
              onChange={(e) =>
                setForm({ ...form, system_prompt: e.target.value })
              }
              placeholder="You are a helpful AI assistant…"
              data-testid="agent-form-prompt"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label">Model preference</label>
              <select
                className="input"
                value={form.model_preference}
                onChange={(e) =>
                  setForm({ ...form, model_preference: e.target.value })
                }
                data-testid="agent-form-model"
              >
                {MODEL_PREFERENCES.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label">Temperature</label>
              <input
                className="input"
                type="number"
                step="0.05"
                min="0"
                max="2"
                value={form.temperature}
                onChange={(e) =>
                  setForm({ ...form, temperature: e.target.value })
                }
              />
            </div>
          </div>

          <div>
            <label className="label">Max tokens per response</label>
            <input
              className="input"
              type="number"
              step="1"
              min="64"
              max="32000"
              value={form.max_tokens}
              onChange={(e) => setForm({ ...form, max_tokens: e.target.value })}
            />
          </div>
        </div>

        <footer className="flex justify-end gap-2 mt-6">
          <button
            type="button"
            className="btn-ghost"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={submit}
            disabled={busy}
            data-testid="agent-form-save"
          >
            {busy ? "Saving…" : isEdit ? "Save changes" : "Create agent"}
          </button>
        </footer>
      </div>
    </div>
  );
}
