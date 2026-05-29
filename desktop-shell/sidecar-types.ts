// desktop-shell/sidecar-types.ts — shared type definitions for the sidecar.
//
// Lives in its own file (with zero project-internal imports) so the renderer
// can `import type { SidecarStatus }` from "../desktop-shell/sidecar-types"
// without dragging the rest of sidecar.ts (and its transitive imports — e.g.
// ./bootstrap/bin_manager) into tsconfig.web.json's resolution graph. The
// main-process sidecar.ts re-exports these for backwards compatibility with
// any internal callers that import "./sidecar" by name.

export type SidecarStatus =
  | { status: "starting" }
  | { status: "ready"; port: number; token: string }
  | { status: "crashed"; code: number | null; signal: NodeJS.Signals | null; error?: string }
  | { status: "stopped" };

export interface SidecarInfo {
  port: number;
  token: string;
}
