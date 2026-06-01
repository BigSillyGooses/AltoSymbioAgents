// desktop-ui/components/design/ArtifactView.tsx
//
// Renders a generated HTML design artifact in a locked-down sandboxed iframe
// with an export toolbar (copy HTML, download .html, open in a new window).
//
// SECURITY (load-bearing): the iframe uses sandbox="allow-scripts" via the
// `srcdoc` attribute and deliberately does NOT include `allow-same-origin`.
// Agent-generated HTML therefore runs with an opaque origin: it cannot reach
// the app's loopback origin, read cookies, or see the bearer token the
// Electron main process injects on renderer→sidecar requests. We also omit
// allow-popups / allow-modals / allow-top-navigation. This is intentionally
// stricter than the markdown path (which keeps skipHtml) — the sandbox is the
// only thing standing between untrusted HTML and the app.

import { useCallback, useState } from "react";

const SANDBOX = "allow-scripts";

interface ArtifactViewProps {
  title: string;
  identifier: string;
  content: string;
  // While the closing </artifact> hasn't streamed in yet the preview is shown
  // but export is disabled (the document is incomplete).
  closed: boolean;
}

export function ArtifactView({ title, identifier, content, closed }: ArtifactViewProps) {
  const [copied, setCopied] = useState(false);

  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API may be unavailable (insecure context). Silently ignore —
      // the user can still download or open the artifact.
    }
  }, [content]);

  const onDownload = useCallback(() => {
    const blob = new Blob([content], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const safe = (identifier || title || "artifact")
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
    a.download = `${safe || "artifact"}.html`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [content, identifier, title]);

  const onOpen = useCallback(() => {
    const blob = new Blob([content], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank", "noopener,noreferrer");
    // Revoke after a tick so the new window has time to load the blob.
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }, [content]);

  return (
    <div
      className="my-2 overflow-hidden rounded-md border border-line bg-bg-1"
      data-testid="artifact-view"
    >
      <div className="flex items-center justify-between gap-2 border-b border-line bg-bg-2 px-3 py-1.5">
        <div className="flex items-center gap-2 min-w-0">
          <svg
            width="13"
            height="13"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
            className="shrink-0 text-ink-dim"
          >
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <path d="M3 9h18" />
          </svg>
          <span className="truncate text-[12px] font-medium text-ink">
            {title || "Design artifact"}
          </span>
          {!closed && (
            <span className="shrink-0 text-[10px] text-ink-faint italic">rendering…</span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={onCopy}
            disabled={!closed}
            aria-label={copied ? "Copied" : "Copy HTML"}
            className="rounded border border-line bg-bg-1 px-2 py-0.5 text-[11px] text-ink-dim hover:text-ink hover:bg-bg-3 disabled:opacity-40"
          >
            {copied ? "Copied" : "Copy"}
          </button>
          <button
            type="button"
            onClick={onDownload}
            disabled={!closed}
            aria-label="Download HTML file"
            className="rounded border border-line bg-bg-1 px-2 py-0.5 text-[11px] text-ink-dim hover:text-ink hover:bg-bg-3 disabled:opacity-40"
          >
            Download
          </button>
          <button
            type="button"
            onClick={onOpen}
            disabled={!closed}
            aria-label="Open in new window"
            className="rounded border border-line bg-bg-1 px-2 py-0.5 text-[11px] text-ink-dim hover:text-ink hover:bg-bg-3 disabled:opacity-40"
          >
            Open
          </button>
        </div>
      </div>
      <iframe
        data-testid="artifact-frame"
        title={title || `artifact-${identifier}`}
        sandbox={SANDBOX}
        srcDoc={content}
        className="block h-[480px] w-full border-0 bg-white"
      />
    </div>
  );
}
