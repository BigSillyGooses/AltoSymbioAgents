import { useEffect, useRef, useState } from "react";

import {
  Workers,
  type WorkerInfo,
  type WorkerTask,
} from "@/api/client";
import { useAppStore } from "@/stores/appStore";

const STATUS_STYLE: Record<string, string> = {
  pending: "text-ink-faint",
  running: "text-accent",
  done: "text-ok border-ok/40",
  error: "text-rose-500 border-rose-300/50",
};

export function WorkersPanel() {
  const ready = useAppStore((s) => s.sidecarStatus?.status === "ready");
  const pushToast = useAppStore((s) => s.pushToast);
  const bgTick = useAppStore((s) => s.bgTick);

  const [workers, setWorkers] = useState<WorkerInfo[]>([]);
  const [tasks, setTasks] = useState<WorkerTask[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const refreshing = useRef(false);

  const refresh = async () => {
    // SSE events bump bgTick rapidly during a run; skip overlapping fetches.
    if (refreshing.current) return;
    refreshing.current = true;
    try {
      const [w, t] = await Promise.all([Workers.list(), Workers.tasks(25)]);
      setWorkers(w.workers);
      setTasks(t.tasks);
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Workers refresh failed",
      });
    } finally {
      refreshing.current = false;
    }
  };

  useEffect(() => {
    if (ready) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, bgTick]);

  const run = async (worker: string) => {
    setBusy(worker);
    try {
      const rsp = await Workers.run(worker);
      if (rsp.ok) {
        pushToast({ kind: "success", text: `Started "${worker}"` });
        refresh();
      } else {
        pushToast({ kind: "error", text: rsp.error ?? "Failed to start worker" });
      }
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Failed to start worker",
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="p-6 overflow-y-auto h-full">
      <header className="mb-4">
        <h1 className="text-xl font-semibold">Background workers</h1>
        <p className="text-sm text-ink-dim">
          Run analysis jobs against your workspace. Progress streams live.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-3 mb-6">
        {workers.map((w) => (
          <div key={w.name} className="card">
            <div className="flex items-center justify-between mb-1">
              <h3 className="font-semibold">{w.name}</h3>
              <button
                className="btn-primary"
                disabled={!ready || busy === w.name}
                onClick={() => run(w.name)}
              >
                {busy === w.name ? "Starting…" : "Run"}
              </button>
            </div>
            <p className="text-xs text-ink-dim">{w.description}</p>
          </div>
        ))}
        {ready && !workers.length && (
          <div className="text-ink-faint text-sm">No workers registered.</div>
        )}
      </div>

      <h2 className="text-sm font-semibold text-ink-dim mb-2">Recent tasks</h2>
      <div className="grid grid-cols-1 gap-2">
        {tasks.map((task) => (
          <div key={task.id} className="card">
            <div className="flex items-center justify-between mb-1">
              <span className="font-medium">{task.worker}</span>
              <span className={`pill ${STATUS_STYLE[task.status] ?? ""}`}>
                {task.status}
              </span>
            </div>
            {task.status === "running" && (
              <div className="h-1.5 bg-bg-3 rounded-full overflow-hidden mt-1">
                <div
                  className="h-full bg-accent transition-all"
                  style={{ width: `${Math.round((task.progress || 0) * 100)}%` }}
                />
              </div>
            )}
            {task.status === "done" && task.result != null && (
              <pre className="text-[11px] text-ink-dim mt-1 whitespace-pre-wrap break-words max-h-32 overflow-y-auto">
                {JSON.stringify(task.result, null, 2)}
              </pre>
            )}
            {task.status === "error" && task.error && (
              <div className="text-xs text-rose-500 mt-1">{task.error}</div>
            )}
            <div className="text-[10px] text-ink-faint mt-1">{task.created_at}</div>
          </div>
        ))}
        {ready && !tasks.length && (
          <div className="text-ink-faint text-sm">No tasks run yet.</div>
        )}
      </div>
    </div>
  );
}
