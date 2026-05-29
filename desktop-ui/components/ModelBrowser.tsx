import { useEffect, useState } from "react";

import {
  Settings,
  System,
  type LocalBackend,
  type LocalModelRow,
  type LocalModelSource,
} from "@/api/client";
import { t } from "@/i18n";
import { useAppStore } from "@/stores/appStore";

function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

const BACKEND_ORDER: LocalBackend[] = ["ollama", "lm_studio", "bundled"];

// Which settings key holds the editable URL for each backend. The bundled
// source has no URL setting (its location is filesystem-derived), so the
// inline editor only renders for the two HTTP backends.
const URL_SETTING_KEY: Partial<Record<LocalBackend, string>> = {
  ollama:    "ollama_url",
  lm_studio: "lm_studio_url",
};

interface SourceCardProps {
  source: LocalModelSource;
  onSave: (key: string, value: string) => Promise<void>;
}

function SourceCard({ source, onSave }: SourceCardProps) {
  const urlKey = URL_SETTING_KEY[source.backend];
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(source.url ?? "");
  const [busy, setBusy] = useState(false);

  // Reset the draft whenever the prop changes (refresh, server save, etc.).
  useEffect(() => {
    if (!editing) setDraft(source.url ?? "");
  }, [source.url, editing]);

  const onCommit = async () => {
    if (!urlKey) return;
    setBusy(true);
    try {
      await onSave(urlKey, draft.trim());
      setEditing(false);
    } finally {
      setBusy(false);
    }
  };

  const statusLabel = source.ok
    ? `${source.count} model${source.count === 1 ? "" : "s"}`
    : (source.error || "Not reachable");
  const statusClass = source.ok
    ? "text-ok"
    : source.count === 0
      ? "text-ink-faint"
      : "text-warn";

  return (
    <div
      className="card flex items-center gap-3 flex-wrap"
      data-testid={`source-${source.backend}`}
    >
      <div className="min-w-[110px]">
        <div className="font-medium">{t(source.backend)}</div>
        <div className={`text-xs ${statusClass}`}>{statusLabel}</div>
      </div>

      <div className="flex-1 min-w-[200px]">
        {urlKey && editing ? (
          <input
            className="input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={source.url ?? ""}
            disabled={busy}
            autoFocus
            data-testid={`source-${source.backend}-url-input`}
          />
        ) : (
          <div className="text-sm text-ink-dim font-mono break-all">
            {source.url ?? "—"}
          </div>
        )}
      </div>

      {urlKey ? (
        editing ? (
          <div className="flex gap-2">
            <button
              type="button"
              className="btn-primary text-xs"
              onClick={onCommit}
              disabled={busy}
            >
              {busy ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              className="btn-ghost text-xs"
              onClick={() => {
                setDraft(source.url ?? "");
                setEditing(false);
              }}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="btn-ghost text-xs"
            onClick={() => setEditing(true)}
          >
            Edit
          </button>
        )
      ) : (
        <span className="text-[11px] text-ink-faint">filesystem</span>
      )}
    </div>
  );
}

export function ModelBrowser() {
  const ready = useAppStore((s) => s.sidecarStatus?.status === "ready");
  const pushToast = useAppStore((s) => s.pushToast);
  const [models, setModels] = useState<LocalModelRow[]>([]);
  const [sources, setSources] = useState<LocalModelSource[]>([]);
  const [current, setCurrent] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string>("");

  const load = async () => {
    setLoading(true);
    try {
      const res = await System.listLocalModels();
      setModels(res.models);
      setCurrent(res.current);
      setSources(res.sources ?? []);
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Could not load models",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!ready) return;
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready]);

  const saveUrl = async (key: string, value: string) => {
    try {
      await Settings.set(key, value);
      pushToast({ kind: "success", text: `Saved ${key}` });
      await load();
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : `Could not save ${key}`,
      });
    }
  };

  const useModel = async (id: string) => {
    setBusyId(id);
    try {
      const res = await System.setActiveLocalModel(id);
      setCurrent(res.current);
      setModels((prev) =>
        prev.map((m) => ({ ...m, loaded: m.id === res.current })),
      );
      pushToast({ kind: "success", text: `Active model set to ${id}` });
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Could not set active model",
      });
    } finally {
      setBusyId("");
    }
  };

  // Sort sources into a stable, predictable order regardless of what the
  // backend returned. Backends not yet known to the frontend still render
  // at the end so nothing is silently dropped.
  const orderedSources = [
    ...BACKEND_ORDER.map((b) => sources.find((s) => s.backend === b)).filter(
      (s): s is LocalModelSource => Boolean(s),
    ),
    ...sources.filter((s) => !BACKEND_ORDER.includes(s.backend)),
  ];

  const reachableCount = orderedSources.filter((s) => s.ok).length;
  const headerSummary =
    sources.length === 0
      ? `${models.length} model${models.length === 1 ? "" : "s"}`
      : `${models.length} model${models.length === 1 ? "" : "s"} · ${reachableCount}/${sources.length} sources online`;

  return (
    <div className="p-6 overflow-y-auto h-full">
      <header className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Models</h1>
          <p className="text-sm text-ink-dim" data-testid="model-summary">
            {headerSummary}
          </p>
        </div>
        <button
          type="button"
          className="btn-ghost"
          onClick={load}
          disabled={!ready || loading}
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </header>

      {orderedSources.length > 0 && (
        <section className="mb-4 space-y-2" data-testid="model-sources">
          {orderedSources.map((s) => (
            <SourceCard key={s.backend} source={s} onSave={saveUrl} />
          ))}
        </section>
      )}

      {models.length === 0 ? (
        <div className="card text-sm text-ink-dim" data-testid="model-empty">
          No local models found. Start Ollama or LM Studio (or download a
          bundled model), then click Refresh. Reachability is shown above
          each source.
        </div>
      ) : (
        <ul className="space-y-2">
          {models.map((m) => {
            const isActive = m.id === current;
            return (
              <li
                key={`${m.backend}:${m.id}`}
                data-testid="model-row"
                className={`card flex items-center justify-between gap-4 ${
                  isActive ? "ring-1 ring-accent/40" : ""
                }`}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-medium break-all">{m.id}</span>
                    {isActive && (
                      <span
                        data-testid="active-badge"
                        className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-accent/15 text-accent"
                      >
                        Active
                      </span>
                    )}
                    <span className="text-[11px] uppercase tracking-wide text-ink-faint">
                      {t(m.backend)}
                    </span>
                    {m.quantization && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-bg-2 text-ink-dim">
                        {m.quantization}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-ink-faint mt-0.5">
                    {formatBytes(m.size_bytes)}
                    {m.context_length
                      ? ` · ${m.context_length.toLocaleString()} ctx`
                      : ""}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn-primary whitespace-nowrap"
                  onClick={() => useModel(m.id)}
                  disabled={!ready || busyId === m.id || isActive}
                >
                  {isActive
                    ? "In use"
                    : busyId === m.id
                      ? "Setting…"
                      : "Use this model"}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
