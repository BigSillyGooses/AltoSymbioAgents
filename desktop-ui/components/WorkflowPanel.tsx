import { useEffect, useRef, useState } from "react";

import {
  Workflows,
  type WorkflowDetail,
  type WorkflowSummary,
  type WorkflowTemplate,
} from "@/api/client";
import { useAppStore } from "@/stores/appStore";

const TASK_STATUS_STYLE: Record<string, string> = {
  pending: "text-ink-faint",
  running: "text-accent",
  completed: "text-ok border-ok/40",
  failed: "text-rose-500 border-rose-300/50",
  skipped: "text-ink-faint italic",
};

export function WorkflowPanel() {
  const ready = useAppStore((s) => s.sidecarStatus?.status === "ready");
  const pushToast = useAppStore((s) => s.pushToast);
  const bgTick = useAppStore((s) => s.bgTick);

  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [selected, setSelected] = useState<WorkflowDetail | null>(null);
  const [templateId, setTemplateId] = useState<string>("");
  const [input, setInput] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const refreshing = useRef(false);

  const refresh = async () => {
    // SSE events bump bgTick rapidly during a run; skip overlapping fetches.
    if (refreshing.current) return;
    refreshing.current = true;
    try {
      const [tpl, wfs] = await Promise.all([
        Workflows.templates(),
        Workflows.list(50),
      ]);
      setTemplates(tpl.templates);
      setWorkflows(wfs.workflows);
      if (!templateId && tpl.templates.length) setTemplateId(tpl.templates[0].id);
      if (selected) {
        const fresh = await Workflows.get(selected.id);
        setSelected(fresh);
      }
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Workflow refresh failed",
      });
    } finally {
      refreshing.current = false;
    }
  };

  useEffect(() => {
    if (ready) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, bgTick]);

  const start = async () => {
    if (!templateId || !input.trim()) {
      pushToast({ kind: "warn", text: "Pick a template and describe the task." });
      return;
    }
    setBusy(true);
    try {
      const rsp = await Workflows.fromTemplate(templateId, input.trim(), true);
      if (rsp.ok) {
        pushToast({ kind: "success", text: "Workflow started" });
        setInput("");
        refresh();
      } else {
        pushToast({ kind: "error", text: rsp.error ?? "Failed to start workflow" });
      }
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Failed to start workflow",
      });
    } finally {
      setBusy(false);
    }
  };

  const open = async (id: string) => {
    try {
      setSelected(await Workflows.get(id));
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Failed to load workflow",
      });
    }
  };

  const resume = async (id: string) => {
    try {
      await Workflows.resume(id);
      pushToast({ kind: "info", text: "Resuming workflow…" });
      refresh();
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Resume failed",
      });
    }
  };

  const activeTemplate = templates.find((t) => t.id === templateId);

  return (
    <div className="p-6 overflow-y-auto h-full">
      <header className="mb-4">
        <h1 className="text-xl font-semibold">Workflows</h1>
        <p className="text-sm text-ink-dim">
          Run a methodology workflow (SPARC, DDD, ADR) as a multi-step DAG.
        </p>
      </header>

      {/* Start from template */}
      <div className="card mb-6">
        <h2 className="font-semibold mb-2">Start from a template</h2>
        <div className="flex flex-wrap gap-2 items-start">
          <select
            className="input"
            value={templateId}
            onChange={(e) => setTemplateId(e.target.value)}
            disabled={!ready}
          >
            {templates.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
          <input
            className="input flex-1 min-w-[200px]"
            placeholder="Describe the task or topic…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={!ready}
          />
          <button className="btn-primary" onClick={start} disabled={!ready || busy}>
            {busy ? "Starting…" : "Start"}
          </button>
        </div>
        {activeTemplate && (
          <p className="text-xs text-ink-faint mt-2">
            {activeTemplate.description} — steps: {activeTemplate.steps.join(" → ")}
          </p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-4">
        {/* Workflow list */}
        <div>
          <h2 className="text-sm font-semibold text-ink-dim mb-2">Runs</h2>
          <div className="grid grid-cols-1 gap-2">
            {workflows.map((wf) => (
              <button
                key={wf.id}
                className={`card text-left ${selected?.id === wf.id ? "ring-2 ring-accent/40" : ""}`}
                onClick={() => open(wf.id)}
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium truncate">{wf.name}</span>
                  <span className="pill">{wf.status}</span>
                </div>
                <div className="text-[10px] text-ink-faint mt-1">{wf.updated_at}</div>
              </button>
            ))}
            {ready && !workflows.length && (
              <div className="text-ink-faint text-sm">No workflows yet.</div>
            )}
          </div>
        </div>

        {/* Selected workflow detail (timeline) */}
        <div>
          <h2 className="text-sm font-semibold text-ink-dim mb-2">Timeline</h2>
          {!selected && (
            <div className="text-ink-faint text-sm">Select a run to see its steps.</div>
          )}
          {selected && (
            <div className="card">
              <div className="flex items-center justify-between mb-2">
                <span className="font-semibold truncate">{selected.name}</span>
                {selected.status === "failed" && (
                  <button className="btn-primary" onClick={() => resume(selected.id)}>
                    Resume
                  </button>
                )}
              </div>
              <ol className="relative border-l border-line/50 ml-2">
                {selected.tasks.map((task) => (
                  <li key={task.id} className="ml-4 mb-3">
                    <div className="absolute -left-[5px] w-2.5 h-2.5 rounded-full bg-accent" />
                    <div className="flex items-center justify-between">
                      <span className="font-medium">{task.name}</span>
                      <span className={`pill ${TASK_STATUS_STYLE[task.status] ?? ""}`}>
                        {task.status}
                      </span>
                    </div>
                    <div className="text-[10px] text-ink-faint">{task.agent_role}</div>
                    {task.output && (
                      <pre className="text-[11px] text-ink-dim mt-1 whitespace-pre-wrap break-words max-h-28 overflow-y-auto">
                        {task.output}
                      </pre>
                    )}
                    {task.error && (
                      <div className="text-xs text-rose-500 mt-1">{task.error}</div>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
