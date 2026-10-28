"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { randomUuid } from "./random-id";
import type { CreateSubagentOpenPayload } from "./types";
import type { ManualSubagentDraftDetail } from "./subagent-drafts-api";

export type AgentCanvasBinding = { draftId: string; version: number; dirty: boolean };
export interface AgentCanvasObject {
  key: string;
  incarnation: string;
  payload: CreateSubagentOpenPayload & { draft_id: string };
  initialDraft?: ManualSubagentDraftDetail;
  binding: AgentCanvasBinding | null;
  subtitle?: string;
  editorRef: { current: { requestClose: () => void } | null };
}
type State = { scopeKey: string | null; objects: AgentCanvasObject[]; selectedKey: string | null; selectionHistory: string[] };

/** Scoped view pointers only. Saved values and input remain in the retained domain editors. */
export function useAgentCanvasObjects(scopeKey: string | null, multiple: boolean) {
  const [state, setState] = useState<State>({ scopeKey, objects: [], selectedKey: null, selectionHistory: [] });
  const current = useRef(state);
  current.current = state;
  const scope = useRef(scopeKey);
  scope.current = scopeKey;
  const dismissed = useRef(new Set<string>());
  const commit = useCallback((next: State) => {
    current.current = next;
    setState(next);
  }, []);
  const clear = useCallback(() => {
    dismissed.current.clear();
    commit({ scopeKey: scope.current, objects: [], selectedKey: null, selectionHistory: [] });
  }, [commit]);
  useEffect(() => {
    if (current.current.scopeKey !== scopeKey) clear();
  }, [scopeKey, clear]);
  const open = useCallback(
    (payload: CreateSubagentOpenPayload, select = true, initialDraft?: ManualSubagentDraftDetail) => {
      if (!payload.draft_id || !scope.current) return;
      const key = JSON.stringify(["agent_creation_draft", scope.current, payload.draft_id]);
      if (!select && dismissed.current.has(key)) return;
      if (select) dismissed.current.delete(key);
      const previous =
        current.current.scopeKey === scope.current
          ? current.current
          : { scopeKey: scope.current, objects: [], selectedKey: null, selectionHistory: [] };
      const existing = previous.objects.find((object) => object.key === key);
      const object: AgentCanvasObject = existing
        ? {
            ...existing,
            payload: {
              ...existing.payload,
              ...payload,
              draft_title: existing.subtitle !== undefined ? existing.payload.draft_title : payload.draft_title || existing.payload.draft_title,
              draft_id: payload.draft_id,
              draft_version: Math.max(existing.payload.draft_version ?? 0, payload.draft_version ?? 0),
            },
          }
        : {
            key,
            incarnation: randomUuid(),
            payload: { ...payload, draft_id: payload.draft_id },
            initialDraft,
            binding: null,
            editorRef: { current: null },
          };
      // Single-object compatibility never disposes an editor through a background event.
      if (!multiple && previous.objects.length && !existing) return;
      commit({
        scopeKey: scope.current,
        objects: existing
          ? previous.objects.map((item) => (item.key === key ? object : item))
          : [...previous.objects, object],
        selectedKey: select || !previous.objects.length ? key : previous.selectedKey,
        selectionHistory: select || !previous.objects.length ? [...previous.selectionHistory.filter((item) => item !== key), key] : previous.selectionHistory,
      });
    },
    [commit, multiple],
  );
  const select = useCallback(
    (key: string | null) => {
      if (current.current.scopeKey !== scope.current) return;
      if (key && !current.current.objects.some((object) => object.key === key)) return;
      commit({ ...current.current, selectedKey: key,
        selectionHistory: key ? [...current.current.selectionHistory.filter((item) => item !== key), key] : current.current.selectionHistory });
    },
    [commit],
  );
  const close = useCallback(
    (key: string, incarnation: string) => {
      const previous = current.current;
      if (
        previous.scopeKey !== scope.current ||
        !previous.objects.some((object) => object.key === key && object.incarnation === incarnation)
      )
        return;
      dismissed.current.add(key);
      const index = previous.objects.findIndex((object) => object.key === key);
      const objects = previous.objects.filter((object) => object.key !== key);
      const selectionChanged = previous.selectedKey === key;
      const selectionHistory = previous.selectionHistory.filter((item) => item !== key);
      const selectedKey = selectionChanged
        ? (selectionHistory.at(-1) ?? objects[Math.min(index, objects.length - 1)]?.key ?? null)
        : previous.selectedKey;
      commit({
        ...previous,
        objects,
        selectedKey,
        selectionHistory,
      });
      return { selectionChanged, selectedDraftId: objects.find((object) => object.key === selectedKey)?.payload.draft_id ?? null };
    },
    [commit],
  );
  const bind = useCallback(
    (key: string, incarnation: string, binding: AgentCanvasBinding | null) => {
      const previous = current.current;
      if (previous.scopeKey !== scope.current) return;
      const original = previous.objects.find((object) => object.key === key && object.incarnation === incarnation);
      if (!original || (binding && binding.draftId !== original.payload.draft_id)) return;
      if (JSON.stringify(original.binding) === JSON.stringify(binding)) return;
      commit({
        ...previous,
        objects: previous.objects.map((object) => (object === original ? { ...object, binding } : object)),
      });
    },
    [commit],
  );
  const title = useCallback(
    (key: string, incarnation: string, label: string, subtitle = "") => {
      const previous = current.current;
      if (previous.scopeKey !== scope.current) return;
      const original = previous.objects.find((object) => object.key === key && object.incarnation === incarnation);
      if (!original || original.payload.draft_title === label && original.subtitle === subtitle) return;
      commit({
        ...previous,
        objects: previous.objects.map((object) =>
          object === original ? { ...object, subtitle, payload: { ...object.payload, draft_title: label } } : object,
        ),
      });
    },
    [commit],
  );
  const visible = state.scopeKey === scopeKey ? state : { objects: [], selectedKey: null };
  return { objects: visible.objects, selectedKey: visible.selectedKey, open, select, close, bind, title, clear };
}
