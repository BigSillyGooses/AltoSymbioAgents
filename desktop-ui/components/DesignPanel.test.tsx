import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/api/client", () => ({
  Design: {
    listArtifacts: vi.fn(),
    getArtifact: vi.fn(),
    deleteArtifact: vi.fn(),
    saveArtifact: vi.fn(),
  },
}));

import { Design } from "@/api/client";
import { DesignPanel } from "./DesignPanel";

function makeQc(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderPanel() {
  return render(
    <QueryClientProvider client={makeQc()}>
      <DesignPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(Design.listArtifacts).mockResolvedValue({ artifacts: [] });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("DesignPanel", () => {
  it("shows the empty state when there are no saved artifacts", async () => {
    vi.mocked(Design.listArtifacts).mockResolvedValue({ artifacts: [] });
    const { container } = renderPanel();
    await waitFor(() => {
      expect(container.querySelector('[data-testid="design-empty"]')).not.toBeNull();
    });
  });

  it("lists saved artifacts and opens one in a sandboxed preview", async () => {
    vi.mocked(Design.listArtifacts).mockResolvedValue({
      artifacts: [
        {
          id: "a1",
          title: "My Landing",
          identifier: "my-landing",
          design_system: "linear-app",
          skill: "web-prototype",
          created_at: "2026-06-01T00:00:00Z",
        },
      ],
    });
    vi.mocked(Design.getArtifact).mockResolvedValue({
      id: "a1",
      title: "My Landing",
      identifier: "my-landing",
      design_system: "linear-app",
      skill: "web-prototype",
      created_at: "2026-06-01T00:00:00Z",
      content: "<!doctype html><h1>Saved</h1>",
    });

    const { container } = renderPanel();
    await waitFor(() => {
      expect(container.querySelector('[data-testid="artifact-card"]')).not.toBeNull();
    });
    expect(container.textContent).toContain("My Landing");

    await userEvent.click(
      container.querySelector('[data-testid="artifact-card"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      const frame = container.querySelector("iframe");
      expect(frame?.getAttribute("sandbox")).toBe("allow-scripts");
      expect(frame?.getAttribute("srcdoc")).toContain("Saved");
    });
    // Library previews are read-only — no Save button.
    expect(container.querySelector('button[aria-label="Save to library"]')).toBeNull();
  });

  it("deletes an artifact", async () => {
    vi.mocked(Design.listArtifacts).mockResolvedValue({
      artifacts: [
        {
          id: "a1",
          title: "My Landing",
          identifier: "my-landing",
          design_system: null,
          skill: null,
          created_at: "2026-06-01T00:00:00Z",
        },
      ],
    });
    vi.mocked(Design.deleteArtifact).mockResolvedValue({ ok: true, id: "a1" });

    const { container } = renderPanel();
    await waitFor(() => {
      expect(container.querySelector('[data-testid="artifact-card"]')).not.toBeNull();
    });
    await userEvent.click(
      container.querySelector('button[aria-label="Delete My Landing"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(Design.deleteArtifact).toHaveBeenCalledWith("a1");
    });
  });
});
