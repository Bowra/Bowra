"use client";

import type { LiteActionReceipt } from "@/lib/config-copilot-lite";

export const WORKSPACE_SETTINGS_SAVED_EVENT = "bowhead:workspace-settings-saved";
export const WORKSPACE_METADATA_SAVED_EVENT = "bowhead:workspace-metadata-saved";
export const WORKSPACE_INSTRUCTIONS_SAVED_EVENT = "bowhead:workspace-instructions-saved";

export interface WorkspaceSettingsSavedTarget {
  teamId: string;
  workspaceId: string;
  receipt?: LiteActionReceipt;
}

/** An invalidation pointer after a real receipt, never candidate values or a save command. */
export function notifyWorkspaceSettingsSaved(target: WorkspaceSettingsSavedTarget): void {
  window.dispatchEvent(new CustomEvent(WORKSPACE_SETTINGS_SAVED_EVENT, { detail: target }));
}

export function notifyWorkspaceMetadataSaved(target: WorkspaceSettingsSavedTarget): void {
  window.dispatchEvent(new CustomEvent(WORKSPACE_METADATA_SAVED_EVENT, { detail: target }));
}

export function notifyWorkspaceInstructionsSaved(target: WorkspaceSettingsSavedTarget): void {
  window.dispatchEvent(new CustomEvent(WORKSPACE_INSTRUCTIONS_SAVED_EVENT, { detail: target }));
}
