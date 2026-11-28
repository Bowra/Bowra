"use client";

import { createContext, useCallback, useRef, useState } from "react";
import { randomUuid } from "./random-id";
import type { CoachResourceOpen } from "./coach-resource-editors";
import type { SkillEditorPort } from "@/components/skill-drawer";
import type { CoachChangeEffect, CoachOperationOpen } from "./coach-operations";

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

const effectKey = (reference: { agent_id: string; proposal_id: string }) =>
  JSON.stringify(["agent_change_effect", reference.agent_id, reference.proposal_id]);

/**
 * Hosted Agent editors to reload after Coach wrote their draft (an `agent_change` receipt), each receipt
 * once. An editor holding unsaved input keeps it; Coach is not sent a message over such input anyway.
 * `effects` are the same writes read from the run record (FEATURE_AGENT_RELEASE_BAR), so an editor also
 * refreshes when Coach did not call "open"; one write reloads once, whichever of the two arrives first.
 */
export function agentEditorsToReload(
  operations: readonly CoachOperationOpen[],
  objects: readonly CoachResourceObject[],
  handled: Set<string>,
  effects: readonly CoachChangeEffect[] = [],
): CoachResourceObject[] {
  const agents: string[] = [];
  for (const operation of operations) {
    const reference = operation.reference;
    if (reference.kind !== "agent_change" || handled.has(operation.requestId)) continue;
    handled.add(operation.requestId);
    if (handled.has(effectKey(reference))) continue;
    agents.push(reference.agent_id);
  }
  for (const { reference } of effects) {
    const key = effectKey(reference);
    if (handled.has(key)) continue;
    handled.add(key);
    const opened = operations.some((operation) => operation.reference.kind === "agent_change"
      && operation.reference.agent_id === reference.agent_id && operation.reference.proposal_id === reference.proposal_id);
    if (!opened) agents.push(reference.agent_id);
  }
  const reload: CoachResourceObject[] = [];
  for (const agentId of agents) {
    for (const object of objects) {
      if (object.pointer.reference.kind !== "agent" || object.pointer.reference.id !== agentId) continue;
      const snapshot = object.editorRef.current?.capture?.();
      if (snapshot?.dirty || snapshot?.pending_write) continue;
      if (!reload.includes(object)) reload.push(object);
    }
  }
  return reload;
}

/**
 * Where Coach's "open" of an operation shows. With FEATURE_AGENT_RELEASE_BAR an Agent draft write opens that Agent's
 * editor (slices-2-5 tech design §6 "Opening"), so its Chat receipt reads 已在右侧打开; the operation view it used to
 * open said both 已接受 and 「请在智能体工作台审阅并接受修改」 (2026-11-27 dogfood F36). Null keeps the operation view:
 * every other kind, and every open with the switch off. The open keeps its request id, so it opens once.
 */
export function agentEditorForOperationOpen(pointer: CoachOperationOpen, releaseBarOn: boolean): CoachResourceOpen | null {
  if (!releaseBarOn || pointer.reference.kind !== "agent_change") return null;
  const { requestId, turnId, userId, teamId, workspaceId, sessionId } = pointer;
  return { requestId, turnId, userId, teamId, workspaceId, sessionId,
    reference: { kind: "agent", id: pointer.reference.agent_id, revision: "current" }, parent: null };
}

/** Agents whose hosted editor is open in the Canvas; a Chat receipt for one of them only says so. */
export const CoachOpenAgentEditorsContext = createContext<ReadonlySet<string>>(new Set());

export function openAgentEditorIds(objects: readonly CoachResourceObject[]): ReadonlySet<string> {
  return new Set(objects.flatMap((object) => object.pointer.reference.kind === "agent" ? [object.pointer.reference.id] : []));
}
