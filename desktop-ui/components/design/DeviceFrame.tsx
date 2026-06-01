// desktop-ui/components/design/DeviceFrame.tsx
//
// Constrains an artifact preview to a device viewport width so a generated
// design can be checked responsively (desktop / tablet / mobile). Pure CSS —
// no image/SVG library (the +10% renderer bundle guard would object). The
// "chrome" is a rounded border + a subtle top bar; the point is the WIDTH
// constraint (real responsive preview), not photoreal skeuomorphism.

import type { ReactNode } from "react";

export type DeviceKind = "desktop" | "tablet" | "mobile";

// Logical CSS widths. Desktop is unconstrained (fills the column); tablet and
// mobile match common breakpoints so a `@media` query in the artifact fires
// the way it would on a real device.
export const DEVICE_WIDTHS: Record<DeviceKind, number | null> = {
  desktop: null,
  tablet: 834,
  mobile: 390,
};

interface DeviceFrameProps {
  device: DeviceKind;
  children: ReactNode;
}

export function DeviceFrame({ device, children }: DeviceFrameProps) {
  const width = DEVICE_WIDTHS[device];

  // Desktop: no chrome, fill the column.
  if (width == null) {
    return (
      <div data-testid="device-frame" data-device={device} className="w-full">
        {children}
      </div>
    );
  }

  // Tablet / mobile: center a width-capped frame with a thin device chrome.
  return (
    <div className="flex w-full justify-center bg-bg-2 py-3">
      <div
        data-testid="device-frame"
        data-device={device}
        className="overflow-hidden rounded-2xl border border-line bg-bg-1 shadow-sm"
        style={{ width, maxWidth: "100%" }}
      >
        <div className="flex items-center justify-center border-b border-line bg-bg-2 py-1">
          <span className="h-1 w-10 rounded-full bg-line" aria-hidden="true" />
        </div>
        {children}
      </div>
    </div>
  );
}
