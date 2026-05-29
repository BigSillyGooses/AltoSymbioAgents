// desktop-ui/components/UpdateBanner.tsx — non-blocking "update is ready" prompt.
//
// Two modes, switched on whether the payload carries a `downloadUrl`:
//   • Auto: listens for IPC "update:downloaded" (re-emitted as
//     `onUpdateDownloaded`). Button reads "Restart now" and calls
//     `installUpdate()` → autoUpdater.quitAndInstall.
//   • Manual: listens for IPC "update:available" with a `downloadUrl`. Button
//     reads "Download v…" and calls `openExternal(downloadUrl)`. The user
//     downloads the new .exe and installs it themselves. Required for
//     unsigned builds where auto-install is blocked.
//
// Dismissal is session-scoped — the banner reappears on the next launch if
// the update is still pending.

import { useEffect } from "react";

import { useAppStore } from "@/stores/appStore";

export function UpdateBanner() {
  const updateReady = useAppStore((s) => s.updateReady);
  const setUpdateReady = useAppStore((s) => s.setUpdateReady);
  const updateBannerDismissed = useAppStore((s) => s.updateBannerDismissed);
  const setUpdateBannerDismissed = useAppStore((s) => s.setUpdateBannerDismissed);

  useEffect(() => {
    // window.electronAPI is only present in the Electron renderer; in tests
    // and on web previews we leave the subscription alone so the banner
    // stays driven purely by appStore state.
    const api = window.electronAPI;
    if (!api) return;
    const unsubs: Array<() => void> = [];
    if (api.onUpdateDownloaded) {
      unsubs.push(api.onUpdateDownloaded((info) => {
        setUpdateReady({ version: info.version });
      }));
    }
    if (api.onUpdateAvailable) {
      unsubs.push(api.onUpdateAvailable((info) => {
        // Only manual-mode payloads carry a downloadUrl. Auto mode also
        // fires update:available, but we ignore it and wait for the
        // companion update:downloaded so the button can offer a real
        // in-place restart rather than an external download.
        if (info.downloadUrl) {
          setUpdateReady({ version: info.version, downloadUrl: info.downloadUrl });
        }
      }));
    }
    return () => {
      for (const fn of unsubs) fn();
    };
  }, [setUpdateReady]);

  if (!updateReady || updateBannerDismissed) return null;

  const manual = Boolean(updateReady.downloadUrl);

  const handleAction = () => {
    if (manual && updateReady.downloadUrl) {
      window.electronAPI?.openExternal?.(updateReady.downloadUrl);
      return;
    }
    window.electronAPI?.installUpdate?.();
  };

  const dismiss = () => {
    setUpdateBannerDismissed(true);
  };

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="update-banner"
      className="flex items-center justify-between gap-3 px-4 py-2 border-b border-accent/40 bg-accent/10 text-sm text-ink"
    >
      <span className="truncate">
        {manual
          ? `Version ${updateReady.version} is available. Download to install.`
          : `Version ${updateReady.version} is ready. Restart to apply.`}
      </span>
      <div className="flex items-center gap-2 flex-shrink-0">
        <button
          type="button"
          className="btn-primary text-xs"
          onClick={handleAction}
        >
          {manual ? `Download v${updateReady.version}` : "Restart now"}
        </button>
        <button
          type="button"
          className="btn-ghost text-xs"
          onClick={dismiss}
        >
          Later
        </button>
      </div>
    </div>
  );
}
