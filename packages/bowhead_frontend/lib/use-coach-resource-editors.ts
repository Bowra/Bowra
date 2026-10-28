"use client";

import { useCallback, useRef, useState } from "react";
import { randomUuid } from "./random-id";
import type { CoachResourceOpen } from "./coach-resource-editors";
import type { SkillEditorPort } from "@/components/skill-drawer";
import type { CoachOperationOpen } from "./coach-operations";

export type CoachResourceObject = {
  key: string; incarnation: string; pointer: CoachResourceOpen; title: string;
  editorRef: { current: SkillEditorPort | null };
};
type State = { scope: string | null; objects: CoachResourceObject[]; selectedKey: string | null; seen: Set<string> };
const empty = (scope: string | null): State => ({ scope, objects: [], selectedKey: null, seen: new Set() });

/** Identity and selection only; original editors retain their buffers and writers. */
export function useCoachResourceEditors(scope: string | null) {
  const [state, setState] = useState(() => empty(scope));
  const current = useRef(state);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  if (current.current.scope !== scope) { current.current = empty(scope); setState(current.current); }
  const commit = useCallback((next: State) => { current.current = next; setState(next); }, []);
  const open = useCallback((pointer: CoachResourceOpen, select = false) => {
    if (!scopeRef.current || current.current.seen.has(pointer.requestId)) return;
    const key = JSON.stringify(["coach_resource", scopeRef.current, pointer.reference.kind, pointer.reference.source_kind, pointer.reference.id]);
    const previous = current.current;
    const existing = previous.objects.find((object) => object.key === key);
    commit({ ...previous, seen: new Set([...previous.seen, pointer.requestId]), selectedKey: select ? key : previous.selectedKey,
      objects: existing ? previous.objects : [...previous.objects, { key, incarnation: randomUuid(), pointer, title: "", editorRef: { current: null } }] });
  }, [commit]);
  const select = useCallback((key: string | null) => {
    if (key && !current.current.objects.some((object) => object.key === key)) return;
    if (current.current.selectedKey !== key) commit({ ...current.current, selectedKey: key });
  }, [commit]);
  const close = useCallback((key: string, incarnation: string) => {
    const previous = current.current;
    if (!previous.objects.some((object) => object.key === key && object.incarnation === incarnation)) return;
    commit({ ...previous, objects: previous.objects.filter((object) => object.key !== key), selectedKey: previous.selectedKey === key ? null : previous.selectedKey });
  }, [commit]);
  const reload = useCallback((key: string, incarnation: string) => {
    const previous = current.current;
    if (!previous.objects.some((object) => object.key === key && object.incarnation === incarnation)) return;
    commit({ ...previous, objects: previous.objects.map((object) => object.key === key
      ? { ...object, incarnation: randomUuid(), editorRef: { current: null } } : object) });
  }, [commit]);
  const title = useCallback((key: string, incarnation: string, value: string) => {
    const previous = current.current;
    const original = previous.objects.find((object) => object.key === key && object.incarnation === incarnation);
    if (!original || original.title === value) return;
    commit({ ...previous, objects: previous.objects.map((object) => object === original ? { ...object, title: value } : object) });
  }, [commit]);
  const visible = state.scope === scope ? state : empty(scope);
  return { objects: visible.objects, selectedKey: visible.selectedKey, open, select, close, reload, title };
}

/**
 * Hosted Agent editors to reload after Coach wrote their draft (an `agent_change` receipt), each receipt
 * once. An editor holding unsaved input keeps it; Coach is not sent a message over such input anyway.
 */
export function agentEditorsToReload(
  operations: readonly CoachOperationOpen[],
  objects: readonly CoachResourceObject[],
  handled: Set<string>,
): CoachResourceObject[] {
  const reload: CoachResourceObject[] = [];
  for (const operation of operations) {
    const reference = operation.reference;
    if (reference.kind !== "agent_change" || handled.has(operation.requestId)) continue;
    handled.add(operation.requestId);
    for (const object of objects) {
      if (object.pointer.reference.kind !== "agent" || object.pointer.reference.id !== reference.agent_id) continue;
      const snapshot = object.editorRef.current?.capture?.();
      if (snapshot?.dirty || snapshot?.pending_write) continue;
      if (!reload.includes(object)) reload.push(object);
    }
  }
  return reload;
}
