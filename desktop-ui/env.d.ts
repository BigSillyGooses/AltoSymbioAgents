/// <reference types="vite/client" />

import type { SidecarStatus } from "../desktop-shell/sidecar-types";

export type BootstrapProgressEvent =
  | { step: number; pct: number; phase?: string; message?: string; error?: undefined }
  | { step: number; pct?: undefined; error: { label: string; cause: string; logPath: string } };

export interface SidecarInfo {
  port: number;
  token: string;
}

export interface ElectronAPI {
  getSidecarInfo: () => Promise<SidecarInfo | null>;
  restartSidecar: () => Promise<SidecarInfo>;
  selectFolder: () => Promise<string | null>;
  selectWorkspaceFolder: () => Promise<string | null>;
  selectFiles: (filters?: { name: string; extensions: string[] }[]) => Promise<string[]>;
  saveFileDialog: (
    suggestedName: string,
    content: string,
  ) => Promise<{ ok: boolean; path?: string; cancelled?: boolean; error?: string }>;
  exportPdf: (
    html: string,
    suggestedName: string,
  ) => Promise<{ ok: boolean; path?: string; cancelled?: boolean; error?: string }>;
  openExternal: (url: string) => Promise<void>;
  getAppVersion: () => Promise<string>;
  getUserDataPath: () => Promise<string>;
  isBootstrapped: () => Promise<boolean>;
  recheckBootstrap: () => Promise<boolean>;
  getPlatform: () => Promise<NodeJS.Platform>;
  startBootstrap: () => Promise<
    | { ok: true }
    | { ok: false; error: { label: string; cause: string } }
  >;
  onBootstrapProgress: (
    handler: (event: BootstrapProgressEvent) => void,
  ) => () => void;
  onBootstrapDone: (handler: () => void) => () => void;
  resetBin: () => Promise<{ ok: true; removed: string }>;
  openBootstrapLogs: () => Promise<{ ok: true; path: string }>;
  onSidecarStatus: (handler: (status: SidecarStatus) => void) => () => void;
  onUpdateAvailable: (
    handler: (info: { version: string; notesUrl?: string; downloadUrl?: string }) => void,
  ) => () => void;
  onUpdateDownloaded: (handler: (info: { version: string }) => void) => () => void;
  installUpdate: () => Promise<void>;
}

declare global {
  interface Window {
    electronAPI: ElectronAPI;
  }
}

export {};
