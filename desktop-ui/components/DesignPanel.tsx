// desktop-ui/components/DesignPanel.tsx
//
// Design Library — browse, preview, and delete artifacts saved from chat via
// the "Save" button in ArtifactView. List is metadata-only; full HTML is
// fetched lazily when a card is opened, then rendered in the same sandboxed
// ArtifactView used in chat (read-only here — allowSave=false).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  Design,
  type SavedArtifact,
  type SavedArtifactSummary,
} from "@/api/client";
import { ArtifactView } from "@/components/design/ArtifactView";

const ARTIFACTS_KEY = ["design", "artifacts"] as const;

export function DesignPanel() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ARTIFACTS_KEY,
    queryFn: () => Design.listArtifacts(),
  });

  const detail = useQuery<SavedArtifact>({
    queryKey: ["design", "artifact", selectedId],
    queryFn: () => Design.getArtifact(selectedId as string),
    enabled: selectedId != null,
  });

  const del = useMutation({
    mutationFn: (id: string) => Design.deleteArtifact(id),
    onSuccess: (_res, id) => {
      if (selectedId === id) setSelectedId(null);
      void queryClient.invalidateQueries({ queryKey: ARTIFACTS_KEY });
    },
  });

  const artifacts = list.data?.artifacts ?? [];

  return (
    <div className="p-4" data-testid="design-panel">
      <header className="mb-3">
        <h2 className="text-lg font-semibold">Design Library</h2>
        <p className="text-sm text-ink-dim">
          HTML design artifacts you saved from chat. Open one to preview it in a
          sandboxed frame, or export it again.
        </p>
      </header>

      {list.isLoading && <p className="text-sm text-ink-dim">Loading…</p>}
      {list.isError && (
        <p className="text-sm text-err">Could not load saved artifacts.</p>
      )}
      {!list.isLoading && artifacts.length === 0 && (
        <p className="text-sm text-ink-dim" data-testid="design-empty">
          No saved artifacts yet. In a chat, generate a design and click
          <span className="font-medium"> Save</span> on its preview.
        </p>
      )}

      <ul className="space-y-1.5">
        {artifacts.map((a: SavedArtifactSummary) => (
          <li
            key={a.id}
            className="flex items-center justify-between gap-2 rounded border border-line bg-bg-1 px-3 py-2"
          >
            <button
              type="button"
              onClick={() => setSelectedId(a.id)}
              className="min-w-0 flex-1 text-left"
              data-testid="artifact-card"
            >
              <div className="truncate text-sm font-medium text-ink">{a.title}</div>
              <div className="truncate text-[11px] text-ink-dim">
                {[a.design_system, a.skill].filter(Boolean).join(" · ") || "—"}
                {"  ·  "}
                {new Date(a.created_at).toLocaleString()}
              </div>
            </button>
            <button
              type="button"
              onClick={() => del.mutate(a.id)}
              aria-label={`Delete ${a.title}`}
              className="shrink-0 rounded border border-line bg-bg-1 px-2 py-0.5 text-[11px] text-ink-dim hover:text-err hover:border-err/40"
            >
              Delete
            </button>
          </li>
        ))}
      </ul>

      {selectedId != null && detail.data && (
        <div className="mt-4">
          <ArtifactView
            title={detail.data.title}
            identifier={detail.data.identifier}
            content={detail.data.content}
            closed
            allowSave={false}
          />
        </div>
      )}
    </div>
  );
}
