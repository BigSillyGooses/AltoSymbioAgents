import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { DeviceFrame, DEVICE_WIDTHS } from "./DeviceFrame";

afterEach(() => cleanup());

describe("DeviceFrame", () => {
  it("desktop renders children without a width constraint", () => {
    const { container } = render(
      <DeviceFrame device="desktop">
        <div data-testid="child">x</div>
      </DeviceFrame>,
    );
    const frame = container.querySelector('[data-testid="device-frame"]');
    expect(frame?.getAttribute("data-device")).toBe("desktop");
    // No inline width style on the desktop frame.
    expect((frame as HTMLElement | null)?.style.width || "").toBe("");
    expect(container.querySelector('[data-testid="child"]')).not.toBeNull();
  });

  it("mobile constrains to the mobile viewport width", () => {
    const { container } = render(
      <DeviceFrame device="mobile">
        <div>x</div>
      </DeviceFrame>,
    );
    const frame = container.querySelector('[data-testid="device-frame"]') as HTMLElement;
    expect(frame.getAttribute("data-device")).toBe("mobile");
    expect(frame.style.width).toBe(`${DEVICE_WIDTHS.mobile}px`);
  });

  it("tablet constrains to the tablet viewport width", () => {
    const { container } = render(
      <DeviceFrame device="tablet">
        <div>x</div>
      </DeviceFrame>,
    );
    const frame = container.querySelector('[data-testid="device-frame"]') as HTMLElement;
    expect(frame.style.width).toBe(`${DEVICE_WIDTHS.tablet}px`);
  });
});
