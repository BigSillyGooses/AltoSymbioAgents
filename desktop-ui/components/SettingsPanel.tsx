import { useEffect, useState } from "react";

import {
  Chat,
  Models,
  PromptTemplates,
  Settings,
  System,
  Voice,
  type ModelCatalogEntry,
  type PromptTemplate,
  type SettingsManifest,
  type SettingsPayload,
  type VoiceAssetsStatus,
} from "@/api/client";
import { t } from "@/i18n";
import { ManifestGroupSection } from "@/components/ManifestGroupSection";
import { VoiceSetupModal } from "@/components/VoiceSetupModal";
import { useAppStore } from "@/stores/appStore";

interface RouterStats {
  total_exchanges: number;
  error_rate_overall: number;
  by_complexity: Record<string, { total: number; errors: number; error_rate: number }>;
  by_route: Record<string, { total: number; errors: number; error_rate: number }>;
  recent: Array<{
    route: string;
    complexity: string;
    had_error: boolean;
    model_used: string;
    created_at: string;
  }>;
}

export function SettingsPanel() {
  const ready = useAppStore((s) => s.sidecarStatus?.status === "ready");
  const pushToast = useAppStore((s) => s.pushToast);
  const [config, setConfig] = useState<SettingsPayload | null>(null);
  const [manifest, setManifest] = useState<SettingsManifest | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [routerStats, setRouterStats] = useState<RouterStats | null>(null);
  const [voiceModalOpen, setVoiceModalOpen] = useState(false);
  const [voiceStatus, setVoiceStatus] = useState<VoiceAssetsStatus | null>(null);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalogEntry[]>([]);

  const reload = async () => {
    try {
      const [fresh, mfst] = await Promise.all([
        Settings.get(),
        Settings.manifest(),
      ]);
      setConfig(fresh);
      setManifest(mfst);
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Could not load settings",
      });
    }
  };

  const reloadRouterStats = async () => {
    try {
      const stats = (await Chat.routerStats()) as RouterStats;
      setRouterStats(stats);
    } catch {
      setRouterStats(null);
    }
  };

  const reloadVoiceStatus = async () => {
    try {
      const status = await Voice.assetsStatus();
      setVoiceStatus(status);
    } catch {
      setVoiceStatus(null);
    }
  };

  const reloadModelCatalog = async () => {
    try {
      const rsp = await Models.catalog();
      setModelCatalog(rsp.models);
    } catch {
      setModelCatalog([]);
    }
  };

  useEffect(() => {
    if (ready) {
      reload();
      reloadRouterStats();
      reloadVoiceStatus();
      reloadModelCatalog();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready]);

  const save = async (key: string, value: unknown) => {
    try {
      await Settings.save(key, value);
      reload();
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Save failed",
      });
    }
  };

  const verifyKey = async () => {
    if (!apiKey.trim()) return;
    setVerifying(true);
    try {
      const rsp = await Settings.verifyApiKey(apiKey);
      if (rsp.ok) {
        pushToast({ kind: "success", text: rsp.message });
        setApiKey("");
        reload();
      } else {
        pushToast({ kind: "error", text: rsp.message });
      }
    } catch (err) {
      pushToast({
        kind: "error",
        text: err instanceof Error ? err.message : "Verify failed",
      });
    } finally {
      setVerifying(false);
    }
  };

  const toggleVoiceInput = async (enabled: boolean) => {
    await save("voice_input_enabled", enabled);
    if (enabled) {
      const fresh = await Voice.assetsStatus().catch(() => null);
      setVoiceStatus(fresh);
      if (fresh && !fresh.stt_ready) setVoiceModalOpen(true);
    }
  };

  const toggleVoiceOutput = async (enabled: boolean) => {
    await save("voice_output_enabled", enabled);
    if (enabled) {
      const fresh = await Voice.assetsStatus().catch(() => null);
      setVoiceStatus(fresh);
      if (fresh && !fresh.tts_ready) setVoiceModalOpen(true);
    }
  };

  if (!config || !manifest) {
    return (
      <div className="p-6 text-ink-dim text-sm">
        {ready ? "Loading…" : "Waiting for backend…"}
      </div>
    );
  }

  return (
    <div className="p-6 overflow-y-auto h-full max-w-2xl space-y-4">
      <header>
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-sm text-ink-dim">API keys, model selection, routing.</p>
      </header>

      {/* ── API key (hand-coded: secret field + verify flow) ─────────────── */}
      <section className="card">
        <h3 className="font-semibold mb-2">Anthropic API key</h3>
        <div className="text-sm text-ink-dim mb-2">
          {config.claude_api_key_set
            ? `Stored in OS keyring · ${config.claude_api_key}`
            : "Not configured."}
        </div>
        <div className="flex gap-2">
          <input
            className="input"
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="sk-ant-…"
          />
          <button className="btn-primary" onClick={verifyKey} disabled={verifying}>
            {verifying ? "Verifying…" : "Verify & save"}
          </button>
        </div>
      </section>

      {/* ── Model (hand-coded: catalog dropdown) ─────────────────────────── */}
      <section className="card">
        <h3 className="font-semibold mb-2">Model</h3>
        <label className="label">Claude model</label>
        {modelCatalog.length > 0 ? (
          <select
            className="input"
            value={config.claude_model}
            onChange={(e) => {
              const next = e.target.value;
              setConfig({ ...config, claude_model: next });
              save("claude_model", next);
            }}
          >
            {modelCatalog.find((m) => m.id === config.claude_model)
              ? null
              : (
                <option value={config.claude_model}>
                  {config.claude_model} (not in catalog)
                </option>
              )}
            {modelCatalog.map((m) => (
              <option key={m.id} value={m.id}>
                {m.display_name} — ${m.input_price_per_mtok}/${m.output_price_per_mtok} per MTok
              </option>
            ))}
          </select>
        ) : (
          <input
            className="input"
            value={config.claude_model}
            onChange={(e) =>
              setConfig({ ...config, claude_model: e.target.value })
            }
            onBlur={() => save("claude_model", config.claude_model)}
          />
        )}
      </section>

      {/* ── Manifest-driven sections ──────────────────────────────────────── */}
      <ManifestGroupSection groupId="routing"    manifest={manifest} onSave={save} />
      <RoutingPerformanceSection stats={routerStats} />
      <ManifestGroupSection groupId="local_models" manifest={manifest} onSave={save} />
      <ManifestGroupSection groupId="updates"    manifest={manifest} onSave={save} />
      <ManifestGroupSection groupId="budget"     manifest={manifest} onSave={save} />
      <ManifestGroupSection groupId="appearance" manifest={manifest} onSave={save} />

      {/* ── Voice (hand-coded: asset status + modal trigger) ─────────────── */}
      <section className="card" data-testid="settings-voice-section">
        <h3 className="font-semibold mb-2">Voice</h3>
        <p className="text-sm text-ink-dim mb-3">
          Speak to dictate messages and have replies read back to you. Both
          features run on your machine — audio never leaves this computer.
        </p>
        <label className="flex items-start gap-2 mb-2">
          <input
            type="checkbox"
            data-testid="settings-voice-input-toggle"
            className="mt-1"
            checked={!!config.voice_input_enabled}
            onChange={(e) => toggleVoiceInput(e.target.checked)}
          />
          <span className="text-sm">
            <div>Voice input</div>
            <div className="text-xs text-ink-dim">
              Adds a mic button to the chat input. Click to record, click again
              to transcribe with Whisper.cpp.
            </div>
          </span>
        </label>
        <label className="flex items-start gap-2 mb-3">
          <input
            type="checkbox"
            data-testid="settings-voice-output-toggle"
            className="mt-1"
            checked={!!config.voice_output_enabled}
            onChange={(e) => toggleVoiceOutput(e.target.checked)}
          />
          <span className="text-sm">
            <div>Voice output</div>
            <div className="text-xs text-ink-dim">
              Adds a speaker button to assistant messages. Click to hear the
              reply spoken aloud with Piper.
            </div>
          </span>
        </label>

        {(config.voice_input_enabled || config.voice_output_enabled) && (
          <>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="label">Speech-to-text model</label>
                <input
                  className="input"
                  value={config.stt_model_id || ""}
                  onChange={(e) =>
                    setConfig({ ...config, stt_model_id: e.target.value })
                  }
                  onBlur={() => save("stt_model_id", config.stt_model_id)}
                />
              </div>
              <div>
                <label className="label">Text-to-speech voice</label>
                <input
                  className="input"
                  value={config.tts_voice_id || ""}
                  onChange={(e) =>
                    setConfig({ ...config, tts_voice_id: e.target.value })
                  }
                  onBlur={() => save("tts_voice_id", config.tts_voice_id)}
                />
              </div>
            </div>

            <div className="rounded-md border border-line bg-bg-2 px-3 py-2 text-xs space-y-1.5 mt-3">
              <div className="flex items-center justify-between">
                <span className="text-ink-dim">STT model</span>
                <span className={voiceStatus?.stt_ready ? "text-ok" : "text-warn"}>
                  {voiceStatus == null
                    ? "Checking…"
                    : voiceStatus.stt_ready
                      ? "Ready"
                      : "Not downloaded"}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-ink-dim">TTS voice</span>
                <span className={voiceStatus?.tts_ready ? "text-ok" : "text-warn"}>
                  {voiceStatus == null
                    ? "Checking…"
                    : voiceStatus.tts_ready
                      ? "Ready"
                      : "Not downloaded"}
                </span>
              </div>
              <div className="flex gap-2 pt-2">
                <button
                  type="button"
                  className="btn-ghost text-xs"
                  onClick={reloadVoiceStatus}
                >
                  Re-check
                </button>
                {(voiceStatus && (!voiceStatus.stt_ready || !voiceStatus.tts_ready)) && (
                  <button
                    type="button"
                    className="btn-primary text-xs"
                    onClick={() => setVoiceModalOpen(true)}
                    data-testid="settings-voice-download"
                  >
                    Download models
                  </button>
                )}
              </div>
            </div>
          </>
        )}
      </section>

      <VoiceSetupModal
        open={voiceModalOpen}
        onClose={() => setVoiceModalOpen(false)}
        onComplete={() => {
          setVoiceModalOpen(false);
          reloadVoiceStatus();
        }}
      />

      {/* ── System prompt (hand-coded: template picker sub-component) ─────── */}
      <section className="card">
        <h3 className="font-semibold mb-2">System prompt</h3>
        <ManifestGroupSection groupId="chat" manifest={manifest} onSave={save} />
        <SystemPromptTemplatesPicker
          onApply={(tmpl) => save("system_prompt", tmpl.body)}
        />
      </section>

      {/* ── New manifest-driven groups ────────────────────────────────────── */}
      <ManifestGroupSection groupId="rag"      manifest={manifest} onSave={save} />
      <ManifestGroupSection groupId="memory"   manifest={manifest} onSave={save} />
      <ManifestGroupSection groupId="advanced" manifest={manifest} onSave={save} />

      {/* ── Troubleshooting ───────────────────────────────────────────────── */}
      <section className="card">
        <h3 className="font-semibold mb-2">Troubleshooting</h3>
        <div className="space-y-2">
          <button
            type="button"
            className="btn-ghost"
            onClick={async () => {
              try {
                await System.exportDiagnostics();
                pushToast({ kind: "success", text: "Diagnostics exported" });
              } catch {
                pushToast({ kind: "error", text: "Export failed" });
              }
            }}
          >
            Export diagnostics
          </button>
          <button
            type="button"
            className="btn-danger"
            onClick={() => {
              const ok = window.confirm(
                "This will clear your conversation history, memory, and settings. Your API key (stored in the OS keyring) will not be affected. This cannot be undone. Continue?",
              );
              if (ok) {
                window.electronAPI.restartSidecar();
              }
            }}
          >
            Reset to defaults
          </button>
        </div>
      </section>
    </div>
  );
}

// ── System prompt template picker ─────────────────────────────────────────────

interface SystemPromptTemplatesPickerProps {
  onApply: (tmpl: PromptTemplate) => void;
}

function SystemPromptTemplatesPicker({
  onApply,
}: SystemPromptTemplatesPickerProps) {
  const ready = useAppStore((s) => s.sidecarStatus?.status === "ready");
  const pushToast = useAppStore((s) => s.pushToast);
  const templates = useAppStore((s) => s.promptTemplates);
  const setPromptTemplates = useAppStore((s) => s.setPromptTemplates);

  useEffect(() => {
    if (!ready) return;
    let alive = true;
    PromptTemplates.list()
      .then((rows) => {
        if (alive) setPromptTemplates(rows);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [ready, setPromptTemplates]);

  const systemPrompts = templates.filter((p) => p.kind === "system_prompt");
  if (systemPrompts.length === 0) return null;

  return (
    <div
      className="mt-3 rounded-md border border-line bg-bg-2 px-3 py-2"
      data-testid="settings-system-prompt-templates"
    >
      <div className="text-xs text-ink-dim mb-2">
        {t("prompts.set_as_default_system_prompt")}
      </div>
      <ul className="space-y-1">
        {systemPrompts.map((tmpl) => (
          <li
            key={tmpl.id}
            className="flex items-center justify-between gap-2 text-xs"
          >
            <span className="truncate">{tmpl.title}</span>
            <button
              type="button"
              className="btn-ghost text-xs"
              data-testid={`settings-set-default-${tmpl.id}`}
              onClick={() => {
                onApply(tmpl);
                pushToast({
                  kind: "success",
                  text: t("prompts.set_as_default.success"),
                });
              }}
            >
              {t("prompts.set_as_default_system_prompt")}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── Routing performance stats ─────────────────────────────────────────────────

function RoutingPerformanceSection({ stats }: { stats: RouterStats | null }) {
  if (!stats || stats.total_exchanges === 0) {
    return (
      <section className="card">
        <h3 className="font-semibold mb-2">Routing Performance</h3>
        <div className="text-sm text-ink-dim">No routing data yet</div>
      </section>
    );
  }

  const errorPct = (stats.error_rate_overall * 100).toFixed(1);
  const claudeCount = stats.by_route?.claude?.total ?? 0;
  const localCount = stats.by_route?.local?.total ?? 0;
  const complexityKeys = ["simple", "medium", "complex"];

  return (
    <section className="card">
      <h3 className="font-semibold mb-2">Routing Performance</h3>
      <div className="grid grid-cols-3 gap-2 mb-3">
        <div className="text-center">
          <div className="text-lg font-semibold">{stats.total_exchanges}</div>
          <div className="text-xs text-ink-dim">Total</div>
        </div>
        <div className="text-center">
          <div className="text-lg font-semibold">{errorPct}%</div>
          <div className="text-xs text-ink-dim">Error rate</div>
        </div>
        <div className="text-center">
          <div className="text-lg font-semibold">
            {claudeCount} / {localCount}
          </div>
          <div className="text-xs text-ink-dim">Claude / local</div>
        </div>
      </div>

      <div className="text-xs font-semibold mb-1">By complexity</div>
      <div className="grid grid-cols-3 gap-2 mb-3 text-xs">
        {complexityKeys.map((k) => {
          const b = stats.by_complexity?.[k];
          return (
            <div key={k} className="border border-base-700 rounded p-2">
              <div className="capitalize text-ink-dim">{k}</div>
              <div>
                {b ? `${b.total} · ${(b.error_rate * 100).toFixed(1)}% err` : "—"}
              </div>
            </div>
          );
        })}
      </div>

      {stats.recent.length > 0 && (
        <>
          <div className="text-xs font-semibold mb-1">Recent decisions</div>
          <div className="text-xs space-y-1 max-h-48 overflow-y-auto">
            {stats.recent.slice(0, 10).map((r, i) => (
              <div
                key={i}
                className="flex justify-between gap-2 border-b border-base-700/50 pb-1"
              >
                <span className="text-ink-dim">{r.route}</span>
                <span className="text-ink-dim">{r.complexity}</span>
                <span className={r.had_error ? "text-rose-400" : ""}>
                  {r.model_used || "—"}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
