import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RagPanel } from "./RagPanel";
import { useAppStore } from "@/stores/appStore";

vi.mock("@/api/client", () => ({
  Rag: {
    status: vi.fn(),
    indexFolder: vi.fn(),
    clear: vi.fn(),
    searchHybrid: vi.fn(),
  },
  Memory: {
    semanticAvailable: vi.fn(),
  },
  Web: {
    status: vi.fn(),
    fetchToRag: vi.fn(),
  },
}));

import { Memory, Rag, Web } from "@/api/client";

const READY_STATUS = { status: "ready" as const, port: 4242, token: "t" };
const RESET_STATE = useAppStore.getState();

beforeEach(() => {
  useAppStore.setState({ sidecarStatus: READY_STATUS, toasts: [] }, false);
  vi.mocked(Rag.status).mockResolvedValue({ total_chunks: 0 });
  vi.mocked(Memory.semanticAvailable).mockResolvedValue({ available: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useAppStore.setState(RESET_STATE, true);
});

describe("RagPanel web section", () => {
  it("prompts to enable web research in Settings when off", async () => {
    vi.mocked(Web.status).mockResolvedValue({
      available: true, stealth_available: false, enabled: false,
    });
    render(<RagPanel />);
    // findByText rejects if the node is absent, so reaching here asserts it.
    expect(await screen.findByText(/Turn on .* in Settings/i)).toBeTruthy();
  });

  it("adds a web page and shows a friendly success toast", async () => {
    vi.mocked(Web.status).mockResolvedValue({
      available: true, stealth_available: false, enabled: true,
    });
    vi.mocked(Web.fetchToRag).mockResolvedValue({
      chunks_added: 2, url: "https://example.com", title: "Example",
    });
    render(<RagPanel />);

    const input = await screen.findByPlaceholderText("https://example.com/article");
    await userEvent.type(input, "https://example.com");
    await userEvent.click(screen.getByRole("button", { name: /Add page/i }));

    await waitFor(() =>
      expect(Web.fetchToRag).toHaveBeenCalledWith("https://example.com"),
    );
    await waitFor(() =>
      expect(
        useAppStore.getState().toasts.some((t) => /Added .*Example/.test(t.text)),
      ).toBe(true),
    );
  });

  it("surfaces a plain-language error toast on failure", async () => {
    vi.mocked(Web.status).mockResolvedValue({
      available: true, stealth_available: false, enabled: true,
    });
    vi.mocked(Web.fetchToRag).mockResolvedValue({
      error: "That address is private or internal — refused for safety.",
      reason: "blocked_host",
    });
    render(<RagPanel />);

    const input = await screen.findByPlaceholderText("https://example.com/article");
    await userEvent.type(input, "http://10.0.0.1");
    await userEvent.click(screen.getByRole("button", { name: /Add page/i }));

    await waitFor(() =>
      expect(
        useAppStore.getState().toasts.some((t) => /refused for safety/.test(t.text)),
      ).toBe(true),
    );
  });
});
