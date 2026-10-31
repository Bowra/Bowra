"use client";

import { useCallback, useRef, useState } from "react";
import type { CoachOperationOpen } from "./coach-operations";

export type CoachOperationView = { key: string; pointer: CoachOperationOpen };
type State = {
  scope: string | null;
  objects: CoachOperationView[];
  selectedKey: string | null;
  seen: Set<string>;
  history: string[];
};
const empty = (scope: string | null): State => ({
  scope,
  objects: [],
  selectedKey: null,
  seen: new Set(),
  history: [],
});

/** View state holds pointers, never domain data or write authority. */
export function useCoachOperationViews(scope: string | null) {
  const [state, setState] = useState(() => empty(scope));
  const current = useRef(state);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  if (current.current.scope !== scope) {
    current.current = empty(scope);
    setState(current.current);
  }
  const commit = useCallback((next: State) => {
    current.current = next;
    setState(next);
  }, []);
  const open = useCallback(
    (pointer: CoachOperationOpen, select: boolean) => {
      if (!scopeRef.current || current.current.seen.has(pointer.requestId))
        return false;
      const previous = current.current;
      const key = JSON.stringify([
        "coach_operation",
        scopeRef.current,
        pointer.reference,
      ]);
      const existing = previous.objects.find((item) => item.key === key);
      commit({
        ...previous,
        seen: new Set([...previous.seen, pointer.requestId]),
        objects: existing
          ? previous.objects.map((item) =>
              item.key === key ? { key, pointer } : item,
            )
          : [...previous.objects, { key, pointer }],
        selectedKey: select ? key : previous.selectedKey,
        history: select
          ? [...previous.history.filter((item) => item !== key), key]
          : previous.history,
      });
      return select;
    },
    [commit],
  );
  const select = useCallback(
    (key: string | null) => {
      if (key && !current.current.objects.some((item) => item.key === key))
        return;
      if (current.current.selectedKey !== key)
        commit({
          ...current.current,
          selectedKey: key,
          history: key
            ? [...current.current.history.filter((item) => item !== key), key]
            : current.current.history,
        });
    },
    [commit],
  );
  const close = useCallback(
    (key: string) => {
      const previous = current.current;
      if (!previous.objects.some((item) => item.key === key)) return;
      const objects = previous.objects.filter((item) => item.key !== key);
      const history = previous.history.filter((item) => item !== key);
      commit({
        ...previous,
        objects,
        history,
        selectedKey:
          previous.selectedKey === key
            ? (history.at(-1) ?? objects[0]?.key ?? null)
            : previous.selectedKey,
      });
    },
    [commit],
  );
  const visible = state.scope === scope ? state : empty(scope);
  return {
    objects: visible.objects,
    selectedKey: visible.selectedKey,
    open,
    select,
    close,
  };
}
