"use client";

import { createContext, useContext, useState, type ReactNode } from "react";
import { WorkspaceSettingsCanvasBridge } from "@/lib/workspace-settings-canvas-bridge";

const Context = createContext<WorkspaceSettingsCanvasBridge | null>(null);

export function WorkspaceSettingsCanvasProvider({ children }: { children: ReactNode }) {
  const [bridge] = useState(() => new WorkspaceSettingsCanvasBridge());
  return <Context.Provider value={bridge}>{children}</Context.Provider>;
}

export function useWorkspaceSettingsCanvasBridge() {
  return useContext(Context);
}
