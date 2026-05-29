import { useCallback, useEffect, useRef, useState } from "react";

import type { BootstrapProgressEvent } from "@/env";
import { useAppStore } from "@/stores/appStore";

// Three-step progress wizard for the Pinokio bootstrap pipeline. Auto-starts
// `bootstrap:start` on mount (Windows only) and renders one of three faces:
//
//   1. Progress face — three labeled steps with per-step progress bars driven
//      by `bootstrap:progress` events.
//   2. Error face — labeled error card (Retry / Reset bin / Open log folder).
//      Retry re-invokes `bootstrap:start` which skips phases whose terminal
//      artifact already exists, so a step-3 SidecarBootError doesn't trigger
//      a fresh 600 MB Miniconda download.
//   3. Unsupported-platform face — non-Windows dev users see a message
//      pointing at the legacy `backend/.venv` fallback. The packaged
//      installer is Win-only per electron-builder.yml so end users only
//      hit this on dev.
//
// Bootstrap is considered done only when the main process emits
// `bootstrap:done`, which fires AFTER waitForSidecarReady resolves —
// gating the flip of `bootstrapped` on a live /health, not just on file
// existence. See desktop-shell/main.ts:bootstrap:start handler.

interface StepState {
  /** null = not started yet; -1 = indeterminate (pip --quiet fallback); 0..100 = in-progress/done. */
  pct: number | null;
  phase?: string;
  message?: string;
}

interface ErrorState {
  step: number;
  label: string;
  cause: string;
  logPath: string;
}

const STEP_LABELS: Record<1 | 2 | 3, string> = {
  1: "Downloading Python",
  2: "Setting up environment",
  3: "Almost done",
};

export function BootstrapWizard() {
  const setBootstrapped = useAppStore((s) => s.setBootstrapped);
  const [platform, setPlatform] = useState<NodeJS.Platform | null>(null);
  const [steps, setSteps] = useState<Record<1 | 2 | 3, StepState>>({
    1: { pct: null },
    2: { pct: null },
    3: { pct: null },
  });
  const [error, setError] = useState<ErrorState | null>(null);
  const [resetting, setResetting] = useState(false);
  const startedRef = useRef(false);

  // Platform detection so we can suppress auto-start on non-Windows.
  useEffect(() => {
    let alive = true;
    (async () => {
      const p = await window.electronAPI.getPlatform();
      if (alive) setPlatform(p);
    })();
    return () => {
      alive = false;
    };
  }, []);

  const begin = useCallback(async () => {
    setError(null);
    try {
      const result = await window.electronAPI.startBootstrap();
      // Both success and labeled failure also stream through the
      // bootstrap:progress / bootstrap:done events that the listeners
      // below consume. We only act on the IPC return value as a
      // defensive fallback: if the renderer somehow missed the error
      // event (race during mount, listener torn down), surface the
      // resolved-value error so the user isn't stuck on a spinning UI.
      if (!result.ok) {
        setError((prev) =>
          prev ?? {
            step: 0,
            label: result.error.label,
            cause: result.error.cause,
            logPath: "",
          },
        );
      }
    } catch (err) {
      setError({
        step: 0,
        label: "BootstrapError",
        cause: err instanceof Error ? err.message : String(err),
        logPath: "",
      });
    }
  }, []);

  // Auto-start the install on Windows once we know the platform. Guarded by
  // startedRef so React StrictMode double-mount in dev doesn't kick off two
  // concurrent downloads.
  useEffect(() => {
    if (platform === null || platform !== "win32") return;
    if (startedRef.current) return;
    startedRef.current = true;
    void begin();
  }, [platform, begin]);

  // Progress + done subscriptions. Stable identity across re-renders so the
  // unsubscribe-on-unmount semantics are correct.
  useEffect(() => {
    const unsubProgress = window.electronAPI.onBootstrapProgress(
      (event: BootstrapProgressEvent) => {
        if (event.error) {
          setError({
            step: event.step,
            label: event.error.label,
            cause: event.error.cause,
            logPath: event.error.logPath,
          });
          return;
        }
        const stepNum = event.step as 1 | 2 | 3;
        if (stepNum !== 1 && stepNum !== 2 && stepNum !== 3) return;
        setSteps((prev) => ({
          ...prev,
          [stepNum]: {
            pct: event.pct,
            phase: event.phase,
            message: event.message,
          },
        }));
      },
    );
    const unsubDone = window.electronAPI.onBootstrapDone(() => {
      setBootstrapped(true);
    });
    return () => {
      unsubProgress();
      unsubDone();
    };
  }, [setBootstrapped]);

  const onRetry = useCallback(() => {
    // Reset error only; main-process skips phases whose artifacts exist, so
    // step 1/2 will already report 100% if they completed before the
    // failure.
    void begin();
  }, [begin]);

  const onResetBin = useCallback(async () => {
    setResetting(true);
    try {
      await window.electronAPI.resetBin();
      setSteps({ 1: { pct: null }, 2: { pct: null }, 3: { pct: null } });
      setError(null);
      // Force a fresh run after the wipe.
      startedRef.current = true;
      void begin();
    } finally {
      setResetting(false);
    }
  }, [begin]);

  const onOpenLogs = useCallback(async () => {
    await window.electronAPI.openBootstrapLogs();
  }, []);

  if (platform !== null && platform !== "win32") {
    return <UnsupportedPlatform />;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
      <div
        className="w-full"
        data-testid="bootstrap-wizard"
        style={{
          maxWidth: 448,
          background: 'rgba(255,255,255,0.76)',
          backdropFilter: 'blur(28px) saturate(160%)',
          WebkitBackdropFilter: 'blur(28px) saturate(160%)',
          border: '1px solid rgba(255,255,255,0.70)',
          borderRadius: 20,
          boxShadow: '0 4px 24px rgba(60,40,80,0.08), 0 12px 48px rgba(60,40,80,0.06)',
          padding: '28px 28px 24px',
          animation: 'bootstrap-fade-up 0.32s ease both',
        }}
      >
        <div className="flex items-center gap-3 mb-5">
          {/* App mark — gradient rounded square */}
          <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
            <defs>
              <linearGradient id="bw-mark-grad" x1="0" y1="0" x2="36" y2="36" gradientUnits="userSpaceOnUse">
                <stop offset="0%" stopColor="#c4b8ff" />
                <stop offset="100%" stopColor="#8a76e0" />
              </linearGradient>
            </defs>
            <rect width="36" height="36" rx="8.5" fill="url(#bw-mark-grad)" />
            <text x="18" y="26" fontFamily="Georgia, serif" fontSize="21" fontStyle="italic" fill="white" textAnchor="middle" opacity="0.95">a</text>
          </svg>
          <div>
            <div className="text-sm font-semibold text-ink" style={{ letterSpacing: '-0.015em', lineHeight: 1.2 }}>
              altosybioagents
            </div>
            <div className="text-ink-faint font-medium" style={{ fontSize: 10.5, letterSpacing: '0.075em', textTransform: 'uppercase', marginTop: 2 }}>
              First-run setup
            </div>
          </div>
        </div>

        {/* Fading divider */}
        <div className="mb-5" style={{ height: 1, background: 'linear-gradient(to right, rgba(232,226,212,0.9), rgba(232,226,212,0.2))' }} />

        {error ? (
          <ErrorCard
            error={error}
            resetting={resetting}
            onRetry={onRetry}
            onResetBin={onResetBin}
            onOpenLogs={onOpenLogs}
          />
        ) : (
          <div data-testid="bootstrap-steps">
            <StepRow n={1} label={STEP_LABELS[1]} state={steps[1]} isLast={false} />
            <StepRow n={2} label={STEP_LABELS[2]} state={steps[2]} isLast={false} />
            <StepRow n={3} label={STEP_LABELS[3]} state={steps[3]} isLast={true} />
          </div>
        )}

        {!error && (
          <div className="flex items-center gap-1.5 mt-5 pt-4" style={{ borderTop: '1px solid rgba(232,226,212,0.5)' }}>
            <svg width="11" height="11" viewBox="0 0 11 11" fill="none" className="flex-shrink-0">
              <circle cx="5.5" cy="5.5" r="4.5" stroke="#a09aab" strokeWidth="1" />
              <path d="M5.5 3v3l1.5 1.5" stroke="#a09aab" strokeWidth="1" strokeLinecap="round" />
            </svg>
            <span className="text-ink-faint" style={{ fontSize: 11.5 }}>
              About 5 min · ~600 MB · downloaded once, works offline
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

interface StepRowProps {
  n: 1 | 2 | 3;
  label: string;
  state: StepState;
  isLast: boolean;
}

function StepDot({ status }: { status: 'pending' | 'active' | 'done' }) {
  const base = 'w-[22px] h-[22px] rounded-full flex-shrink-0 flex items-center justify-center relative';

  if (status === 'done') return (
    <div className={base} style={{ background: '#4a8a68' }}>
      <svg width="11" height="9" viewBox="0 0 11 9" fill="none"
        style={{ animation: 'bootstrap-check-pop 0.3s cubic-bezier(0.22,0.61,0.36,1) both' }}>
        <path d="M1 4.5l3.2 3L10 1" stroke="white" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );

  if (status === 'active') return (
    <div className={base}>
      {/* Ripple */}
      <div style={{
        position: 'absolute', inset: -2, borderRadius: '50%',
        border: '1.5px solid #b4a7f5',
        animation: 'bootstrap-ripple 1.8s ease-out infinite',
      }} />
      {/* Spinning arc */}
      <div style={{
        position: 'absolute', inset: 0, borderRadius: '50%',
        border: '2px solid rgba(180,167,245,0.18)',
        borderTopColor: '#b4a7f5', borderRightColor: '#b4a7f5',
        animation: 'bootstrap-spin 0.72s linear infinite',
      }} />
      {/* Center dot */}
      <div style={{ width: 7, height: 7, borderRadius: '50%', background: '#b4a7f5', opacity: 0.55 }} />
    </div>
  );

  // pending
  return (
    <div className={base} style={{ border: '1.5px solid #e8e2d4', background: 'rgba(255,255,255,0.45)' }}>
      <div style={{ width: 5, height: 5, borderRadius: '50%', background: '#e8e2d4' }} />
    </div>
  );
}

function StepRow({ n, label, state, isLast }: StepRowProps) {
  const status: 'pending' | 'active' | 'done' =
    state.pct === null ? 'pending' :
    state.pct >= 100   ? 'done'    : 'active';

  const indeterminate = state.pct === -1;
  const visualPct =
    status === 'done'    ? 100 :
    status === 'pending' ? 0   :
    indeterminate        ? 55  :
    Math.max(0, Math.min(99, state.pct ?? 0));

  const connectorDone = state.pct != null && state.pct >= 100;

  return (
    <div className="flex gap-3" data-testid={`bootstrap-step-${n}`}>
      {/* Left col: dot + connector line */}
      <div className="flex flex-col items-center flex-shrink-0 pt-px">
        <StepDot status={status} />
        {!isLast && (
          <div
            style={{
              width: 1, flex: 1, minHeight: 18,
              marginTop: 5, marginBottom: 5,
              background: connectorDone
                ? 'linear-gradient(to bottom, rgba(74,138,104,0.35), rgba(180,167,245,0.18))'
                : 'rgba(232,226,212,0.7)',
              transition: 'background 0.5s ease',
            }}
          />
        )}
      </div>

      {/* Right col: label + bar + message */}
      <div className="flex-1 min-w-0" style={{ paddingBottom: isLast ? 0 : 20 }}>
        <div className="flex items-baseline gap-2">
          <span
            className="flex-1 transition-colors duration-200"
            style={{
              fontSize: 13.5,
              fontWeight: status === 'active' ? 500 : 400,
              color: status === 'pending' ? 'var(--color-ink-faint, #a09aab)' : 'var(--color-ink, #2a2730)',
            }}
          >
            {label}
          </span>
          {status === 'active' && !indeterminate && state.pct != null && (
            <span className="font-mono text-ink-faint flex-shrink-0" style={{ fontSize: 11 }}>
              {state.pct}%
            </span>
          )}
        </div>

        {/* Progress track */}
        <div
          role="progressbar"
          aria-label={label}
          aria-valuenow={visualPct}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-busy={status === 'active'}
          style={{
            height: 2, borderRadius: 99, marginTop: 7, overflow: 'hidden',
            background:
              status === 'done'    ? 'rgba(74,138,104,0.14)'  :
              'rgba(180,167,245,0.13)',
            opacity: status === 'pending' ? 0.45 : 1,
            transition: 'opacity 0.2s, background 0.3s',
          }}
        >
          <div
            style={{
              height: '100%', borderRadius: 99,
              width: `${visualPct}%`,
              background:
                status === 'done' ? '#4a8a68' :
                indeterminate
                  ? 'linear-gradient(90deg, transparent 0%, #b4a7f5 50%, transparent 100%)'
                  : 'linear-gradient(90deg, #b4a7f5, #9485e6)',
              backgroundSize: indeterminate ? '320px 100%' : undefined,
              animation: indeterminate ? 'bootstrap-shimmer 1.4s ease-in-out infinite' : undefined,
              transition: indeterminate ? undefined : 'width 0.38s cubic-bezier(0.22,0.61,0.36,1)',
            }}
          />
        </div>

        {/* Sub-message (package name / status) */}
        {state.message && (
          <div
            key={state.message}
            className="font-mono text-ink-faint truncate"
            title={state.message}
            style={{ fontSize: 11, marginTop: 5, animation: 'bootstrap-msg-fade 0.18s ease both' }}
          >
            {state.message}
          </div>
        )}
      </div>
    </div>
  );
}

interface ErrorCardProps {
  error: ErrorState;
  resetting: boolean;
  onRetry: () => void;
  onResetBin: () => Promise<void>;
  onOpenLogs: () => Promise<void>;
}

function ErrorCard({ error, resetting, onRetry, onResetBin, onOpenLogs }: ErrorCardProps) {
  return (
    <div
      data-testid="bootstrap-error"
      style={{
        marginTop: 16,
        background: 'rgba(184,85,71,0.05)',
        border: '1px solid rgba(184,85,71,0.18)',
        borderRadius: 12, padding: '14px 16px',
        animation: 'bootstrap-fade-up 0.22s ease both',
      }}
    >
      <div className="flex gap-2.5 items-start">
        <div style={{
          width: 28, height: 28, borderRadius: '50%', flexShrink: 0,
          background: 'rgba(184,85,71,0.10)',
          border: '1px solid rgba(184,85,71,0.22)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', marginTop: 1,
        }}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M6 2.5v4M6 8.5v.5" stroke="#b85547" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-err font-semibold" style={{ fontSize: 10.5, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 5 }}>
            {error.label}
          </div>
          <div className="text-ink" style={{ fontSize: 13, lineHeight: 1.55 }}>{error.cause}</div>
          {error.logPath && (
            <div className="font-mono text-ink-faint break-all" style={{ fontSize: 10.5, marginTop: 8, lineHeight: 1.5 }}>
              {error.logPath}
            </div>
          )}
        </div>
      </div>

      <div className="flex gap-2 mt-3.5">
        <button
          className="btn-primary flex-1"
          onClick={onRetry}
          disabled={resetting}
          data-testid="bootstrap-error-retry"
        >
          Retry
        </button>
        <button
          className="btn-ghost flex-1"
          onClick={() => void onResetBin()}
          disabled={resetting}
          data-testid="bootstrap-error-reset"
        >
          {resetting ? 'Resetting…' : 'Reset bin'}
        </button>
        <button
          className="btn-ghost flex-1"
          onClick={() => void onOpenLogs()}
          data-testid="bootstrap-error-open-logs"
        >
          Open logs
        </button>
      </div>
    </div>
  );
}

function UnsupportedPlatform() {
  return (
    <div className="fixed inset-0 z-50 bg-bg/95 flex items-center justify-center p-6">
      <div
        className="w-full"
        data-testid="bootstrap-wizard"
        style={{
          maxWidth: 448,
          background: 'rgba(255,255,255,0.76)',
          backdropFilter: 'blur(28px) saturate(160%)',
          WebkitBackdropFilter: 'blur(28px) saturate(160%)',
          border: '1px solid rgba(255,255,255,0.70)',
          borderRadius: 20,
          boxShadow: '0 4px 24px rgba(60,40,80,0.08), 0 12px 48px rgba(60,40,80,0.06)',
          padding: '28px 28px 24px',
          animation: 'bootstrap-fade-up 0.32s ease both',
        }}
      >
        <header className="mb-4">
          <div className="text-xs uppercase tracking-wide text-ink-faint mb-1">
            First-run setup
          </div>
          <h1 className="text-xl font-semibold">
            Windows installer only this release
          </h1>
        </header>
        <p className="text-sm text-ink-dim">
          The bundled-Python install flow ships for Windows x64 in this
          release. macOS and Linux installers are tracked for a future
          release. For development, create a Python 3.12 venv at{" "}
          <code className="text-ink">backend/.venv</code> and reinstall the
          sidecar requirements.
        </p>
      </div>
    </div>
  );
}
