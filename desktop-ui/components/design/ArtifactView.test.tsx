import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Mock the Design client so the Save button runs without hitting the network.
vi.mock("@/api/client", () => ({
  Design: { saveArtifact: vi.fn().mockResolvedValue({ id: "a1" }) },
}));

import { Design } from "@/api/client";
import { ArtifactView } from "./ArtifactView";
import { DEVICE_WIDTHS } from "./DeviceFrame";

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

  it("shows a placeholder (no iframe) while the artifact is still streaming", () => {
    const { container } = render(
      <ArtifactView title="Page" identifier="page" content="<html>" closed={false} />,
    );
    const copyBtn = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Copy HTML"]',
    );
    expect(copyBtn?.disabled).toBe(true);
    // The iframe is NOT mounted mid-stream — mounting it would reload per
    // token. A placeholder stands in until the closing tag arrives.
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector('[data-testid="artifact-pending"]')).not.toBeNull();
  });

  it("mounts the iframe only once the artifact is closed", () => {
    const { container, rerender } = render(
      <ArtifactView title="Page" identifier="page" content="<html>" closed={false} />,
    );
    expect(container.querySelector("iframe")).toBeNull();
    rerender(
      <ArtifactView title="Page" identifier="page" content={HTML} closed />,
    );
    expect(container.querySelector("iframe")?.getAttribute("srcdoc")).toBe(HTML);
  });

  it("switches the preview viewport when a device is selected", async () => {
    const { container } = render(
      <ArtifactView title="Page" identifier="page" content={HTML} closed />,
    );
    // Default desktop: no width constraint on the frame.
    let frame = container.querySelector('[data-testid="device-frame"]') as HTMLElement;
    expect(frame.getAttribute("data-device")).toBe("desktop");

    await userEvent.click(
      container.querySelector('button[aria-label="Mobile viewport"]') as HTMLButtonElement,
    );
    frame = container.querySelector('[data-testid="device-frame"]') as HTMLElement;
    expect(frame.getAttribute("data-device")).toBe("mobile");
    expect(frame.style.width).toBe(`${DEVICE_WIDTHS.mobile}px`);
  });

  it("saves the artifact to the library", async () => {
    const { container } = render(
      <ArtifactView
        title="Page"
        identifier="page"
        content={HTML}
        closed
        designSystem="linear-app"
        skill="web-prototype"
      />,
    );
    await userEvent.click(
      container.querySelector('button[aria-label="Save to library"]') as HTMLButtonElement,
    );
    await waitFor(() => {
      expect(Design.saveArtifact).toHaveBeenCalledWith({
        title: "Page",
        identifier: "page",
        content: HTML,
        design_system: "linear-app",
        skill: "web-prototype",
      });
    });
  });

  it("hides the Save button in read-only mode (library preview)", () => {
    const { container } = render(
      <ArtifactView title="Page" identifier="page" content={HTML} closed allowSave={false} />,
    );
    expect(container.querySelector('button[aria-label="Save to library"]')).toBeNull();
  });
});
