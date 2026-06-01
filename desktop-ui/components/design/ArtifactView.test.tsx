import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ArtifactView } from "./ArtifactView";

const HTML = "<!doctype html><html><body><h1>Hi</h1></body></html>";

// jsdom ships neither navigator.clipboard nor URL.createObjectURL/revoke.
// Stub the unavailable browser APIs (NOT the data) so the export handlers run.
function stubBrowserApis() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const createObjectURL = vi.fn(() => "blob:mock-url");
  const revokeObjectURL = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
  return { writeText, createObjectURL };
}

beforeEach(() => {
  stubBrowserApis();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ArtifactView", () => {
  it("renders the HTML in a sandboxed iframe with the locked-down attribute set", () => {
    const { container } = render(
      <ArtifactView title="Page" identifier="page" content={HTML} closed />,
    );
    const frame = container.querySelector("iframe");
    expect(frame).not.toBeNull();
    // Load-bearing security contract: scripts allowed, but NOT same-origin
    // (so the artifact can't reach the app origin / auth token).
    expect(frame?.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame?.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(frame?.getAttribute("srcdoc")).toBe(HTML);
  });

  it("copies the HTML to the clipboard", async () => {
    const { writeText } = stubBrowserApis();
    const { container } = render(
      <ArtifactView title="Page" identifier="page" content={HTML} closed />,
    );
    const copyBtn = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Copy HTML"]',
    );
    expect(copyBtn).not.toBeNull();
    await userEvent.click(copyBtn as HTMLButtonElement);
    expect(writeText).toHaveBeenCalledWith(HTML);
  });

  it("creates a blob URL when downloading", async () => {
    const { createObjectURL } = stubBrowserApis();
    const { container } = render(
      <ArtifactView title="Page" identifier="page" content={HTML} closed />,
    );
    const downloadBtn = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Download HTML file"]',
    );
    await userEvent.click(downloadBtn as HTMLButtonElement);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("disables export while the artifact is still streaming", () => {
    const { container } = render(
      <ArtifactView title="Page" identifier="page" content="<html>" closed={false} />,
    );
    const copyBtn = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Copy HTML"]',
    );
    expect(copyBtn?.disabled).toBe(true);
    // The preview iframe still renders the partial document.
    expect(container.querySelector("iframe")?.getAttribute("srcdoc")).toBe("<html>");
  });
});
