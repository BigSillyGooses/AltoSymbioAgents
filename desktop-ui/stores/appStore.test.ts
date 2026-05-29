import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAppStore } from "./appStore";

const RESET_STATE = useAppStore.getState();

beforeEach(() => {
  vi.useFakeTimers();
  // Snapshot reset so each test starts from a clean store.
  useAppStore.setState({
    toasts: [],
    activeChat: null,
  }, false);
});

afterEach(() => {
  vi.useRealTimers();
  // Restore the original action references in case anything tampered with them.
  useAppStore.setState(RESET_STATE, true);
});

describe("pushToast", () => {
  it("adds a toast and auto-dismisses it after the configured delay", () => {
    useAppStore.getState().pushToast({ kind: "info", text: "hello" });
    expect(useAppStore.getState().toasts).toHaveLength(1);
    expect(useAppStore.getState().toasts[0].text).toBe("hello");

    // Info toasts auto-dismiss after 4000 ms (errors/warnings stick for 8000).
    vi.advanceTimersByTime(3999);
    expect(useAppStore.getState().toasts).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(useAppStore.getState().toasts).toHaveLength(0);
  });

  it("keeps error toasts visible for the longer 8000 ms window", () => {
    useAppStore.getState().pushToast({ kind: "error", text: "boom" });
    vi.advanceTimersByTime(4000);
    expect(useAppStore.getState().toasts).toHaveLength(1);
    vi.advanceTimersByTime(4000);
    expect(useAppStore.getState().toasts).toHaveLength(0);
  });
});

describe("appendChatToken", () => {
  it("trims the streaming buffer once it exceeds the 1 000 000 char cap", () => {
    useAppStore.getState().startChatStream("conv-1");

    // Just under the MAX. Append should not trim.
    useAppStore.getState().appendChatToken("a".repeat(999_999));
    expect(useAppStore.getState().activeChat?.buffer.length).toBe(999_999);

    // Crossing MAX (next.length === 1_000_001) should drop the head and keep
    // KEEP=500_000 chars from the tail.
    useAppStore.getState().appendChatToken("bb");
    expect(useAppStore.getState().activeChat?.buffer.length).toBe(500_000);
    // The kept tail should end in the most recently appended bytes.
    expect(useAppStore.getState().activeChat?.buffer.endsWith("bb")).toBe(true);
  });
});

