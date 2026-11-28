"use client";

/**
 * The Canvas Agent editor's unsaved fields, as the trial chat needs them live (trial-tech-design §7, canvas ⑪):
 * the hint above the composer and the Send button's name say whether sending will save first. The hosted editor
 * reports; the chat subscribes. In-memory only, keyed by Agent.
 */

import { useSyncExternalStore } from "react";

import type { DraftFieldKey } from "@/lib/agent-trial";

const EMPTY: readonly DraftFieldKey[] = [];
const fields = new Map<string, readonly DraftFieldKey[]>();
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function reportTrialEditorDirtyFields(agentId: string, value: readonly DraftFieldKey[]): void {
  const previous = fields.get(agentId) ?? EMPTY;
  if (previous.length === value.length && previous.every((field, index) => field === value[index])) return;
  if (value.length) fields.set(agentId, [...value]);
  else fields.delete(agentId);
  for (const listener of [...listeners]) listener();
}

export function readTrialEditorDirtyFields(agentId: string | null | undefined): readonly DraftFieldKey[] {
  return agentId ? fields.get(agentId) ?? EMPTY : EMPTY;
}

export function useTrialEditorDirtyFields(agentId: string | null | undefined): readonly DraftFieldKey[] {
  return useSyncExternalStore(subscribe, () => readTrialEditorDirtyFields(agentId), () => EMPTY);
}
