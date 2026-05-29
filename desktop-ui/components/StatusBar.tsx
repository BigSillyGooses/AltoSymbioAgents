// desktop-ui/components/StatusBar.tsx — top-of-app status indicator.
//
// Shows a colored dot for the sidecar lifecycle, the assigned port, the app
// version, and (when crashed) a Restart Backend button.

import { useEffect, useState } from "react";

import { System } from "@/api/client";
import { useAppStore } from "@/stores/appStore";

export function StatusBar() {
  const status = useAppStore((s) => s.sidecarStatus);
  const votingActive = useAppStore((s) => s.votingActive);
  const [version, setVersion] = useState<string>("");
  const [localModelOnline, setLocalModelOnline] = useState<boolean | null>(null);
  const [keyringAvailable, setKeyringAvailable] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    window.electronAPI
      .getAppVersion()
      .then((v) => {
        if (alive) setVersion(v);
      })
      .catch(() => {
        // Preload bridge may not be ready yet (or, in tests, missing
        // entirely). Leave the version blank rather than logging an
        // unhandled rejection.
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (status?.status !== "ready") return;
    let alive = true;
    const check = async () => {
      try {
        const s = await System.serviceStatus();
        if (!alive) return;
        setLocalModelOnline(s?.local_client?.ok ?? false);
        setKeyringAvailable(s?.keyring?.ok ?? true);
      } catch {
        if (alive) {
          setLocalModelOnline(false);
          // Don't flip the keyring warning on a fetch failure — that's a
          // sidecar problem, not a secrets-on-disk problem.
        }
      }
    };
    check();
    const interval = setInterval(check, 30_000);
    return () => { alive = false; clearInterval(interval); };
  }, [status]);

  const dot = (() => {
    if (!status) return "bg-ink-faint";
    switch (status.status) {
      case "ready":
        return "bg-ok shadow-[0_0_8px_rgba(61,214,140,0.5)]";
      case "starting":
        return "bg-warn animate-pulse";
      case "crashed":
        return "bg-err";
      case "stopped":
        return "bg-ink-faint";
    }
  })();

  const label = (() => {
    if (!status) return "Initializing…";
    switch (status.status) {
      case "ready":
        return `Ready · :${status.port}`;
      case "starting":
        return "Starting backend…";
      case "crashed":
        return status.error || "Backend crashed";
      case "stopped":
        return "Backend stopped";
    }
  })();

  const handleRestart = async () => {
    try {
      await window.electronAPI.restartSidecar();
    } catch (err) {
      console.error("restart failed:", err);
    }
  };

  const handleExportLogs = async () => {
    try {
      await System.exportDiagnostics();
    } catch {
      try {
        const path = await window.electronAPI.getUserDataPath();
        await window.electronAPI.openExternal(path);
      } catch (err) {
        console.error("export logs failed:", err);
      }
    }
  };

  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2 border-b border-line bg-bg-1/80 backdrop-blur">
      <span role="status" aria-live="polite" className="flex items-center gap-2 min-w-0">
        <span className={`h-2 w-2 rounded-full flex-shrink-0 ${dot}`} aria-hidden />
        <span className="text-xs text-ink-dim truncate" title={label}>
          {label}
        </span>
      </span>
      <div className="flex items-center gap-3">
        {votingActive && (
          <span
            className="text-[11px] px-1.5 py-0.5 rounded border border-accent/40 bg-accent/10 text-accent flex items-center gap-1.5"
            title="Running 3-sample weighted-vote consensus on this high-stakes turn"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-accent animate-pulse" aria-hidden />
            Verifying with consensus…
          </span>
        )}
        {localModelOnline === false && status?.status === "ready" && (
          <span
            className="text-[11px] px-1.5 py-0.5 rounded border border-warn/40 bg-warn/10 text-warn"
            title="All messages routed to Claude (higher cost)"
          >
            Local model offline
          </span>
        )}
        {keyringAvailable === false && status?.status === "ready" && (
          <span
            className="text-[11px] px-1.5 py-0.5 rounded border border-warn/40 bg-warn/10 text-warn"
            title={
              "Your OS keyring is unavailable. Your Anthropic API key is " +
              "stored in plaintext in settings.json. Move to a machine with " +
              "a working keyring (Windows Credential Manager, macOS Keychain, " +
              "Linux SecretService) for stronger storage."
            }
          >
            ⚠ API key stored in plaintext
          </span>
        )}
        {status?.status === "crashed" && (
          <>
            <button className="btn-danger text-xs" onClick={handleRestart}>
              Restart Backend
            </button>
            <button className="btn-ghost text-xs" onClick={handleExportLogs}>
              Export logs
            </button>
          </>
        )}
        {version && (
          <span className="text-xs text-ink-faint font-mono">v{version}</span>
        )}
      </div>
    </div>
  );
}
