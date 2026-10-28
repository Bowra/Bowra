"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  clearSettingsCanvasTargetInUrl,
  readSettingsCanvasTarget,
  readSettingsCanvasSelection,
  writeSettingsCanvasTargetInUrl,
  settingsCanvasKey,
  settingsCanvasTargetMatches,
  type SettingsCanvasTarget,
} from "./settings-canvas-target";

/** First page host: only the current authorized Space, without admitting a Chat target. */
export function useSettingsCanvas({
  enabled,
  scope,
  canManageSpace,
  searchParams,
  sessionId,
}: {
  enabled: boolean;
  scope: { userId: string; teamId: string; workspaceId: string } | null;
  canManageSpace: boolean;
  searchParams: URLSearchParams;
  sessionId: string | null;
}) {
  const [targets, setTargets] = useState<SettingsCanvasTarget[]>([]);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const target = targets.find((item) => settingsCanvasKey(item) === activeKey) ?? targets.at(0) ?? null;
  const [selected, setSelected] = useState(false);
  const toolRequestsRef = useRef(new Set<string>());
  const consumedRef = useRef<string | null>(null);
  const sessionRef = useRef(sessionId);
  const scopeKey = scope ? JSON.stringify([scope.userId, scope.teamId, scope.workspaceId]) : null;
  const request = readSettingsCanvasTarget(searchParams);
  const requestKey = request ? settingsCanvasKey(request) : null;
  const authorized = enabled && canManageSpace && scope !== null;
  const visibleTarget = authorized && scope && target && settingsCanvasTargetMatches(target, scope) ? target : null;
  const current = useRef({ targets, target, selected, authorized, scope });
  current.current = { targets, target, selected, authorized, scope };
  const commit = useCallback((nextTargets: SettingsCanvasTarget[], nextTarget: SettingsCanvasTarget | null, nextSelected: boolean) => {
    current.current = { ...current.current, targets: nextTargets, target: nextTarget, selected: nextSelected };
    setTargets(nextTargets);
    setActiveKey(nextTarget ? settingsCanvasKey(nextTarget) : null);
    setSelected(nextSelected);
  }, []);

  const close = useCallback(
    (key?: string) => {
      const previous = current.current;
      if (key && !previous.targets.some((item) => settingsCanvasKey(item) === key)) return;
      const remaining = key ? previous.targets.filter((item) => settingsCanvasKey(item) !== key) : [];
      const nextTarget = previous.target && remaining.includes(previous.target) ? previous.target : remaining.at(-1) ?? null;
      const nextSelected = previous.selected && nextTarget !== null;
      commit(remaining, nextTarget, nextSelected);
      consumedRef.current = null;
      const live = readSettingsCanvasTarget(new URLSearchParams(window.location.search));
      // A background disposal cannot clear another page's current route.
      if (live && previous.targets.some((item) => settingsCanvasKey(item) === settingsCanvasKey(live))
        && !remaining.some((item) => settingsCanvasKey(item) === settingsCanvasKey(live))) {
        if (nextTarget) writeSettingsCanvasTargetInUrl(nextTarget, nextSelected);
        else clearSettingsCanvasTargetInUrl();
      }
    },
    [commit],
  );

  useEffect(() => {
    if (!target || visibleTarget) return;
    close();
  }, [target, visibleTarget, close]);

  useEffect(() => {
    const previous = sessionRef.current;
    sessionRef.current = sessionId;
    // A first send may assign the new conversation. An unrelated conversation detaches this view.
    if (previous !== null && previous !== sessionId) close();
  }, [sessionId, close]);

  useEffect(() => {
    if (!requestKey) {
      consumedRef.current = null;
      return;
    }
    const requestSelected = readSettingsCanvasSelection(searchParams);
    const requestIdentity = JSON.stringify([requestKey, requestSelected]);
    if (!authorized || !scope || !request || consumedRef.current === requestIdentity) return;
    const liveParams = new URLSearchParams(window.location.search);
    const live = readSettingsCanvasTarget(liveParams);
    if (!live || settingsCanvasKey(live) !== requestKey) return;
    if (readSettingsCanvasSelection(liveParams) !== requestSelected) return;
    consumedRef.current = requestIdentity;
    if (!settingsCanvasTargetMatches(request, scope)) return;
    const previous = current.current.targets;
    const retained = previous.find((item) => settingsCanvasKey(item) === requestKey);
    commit(retained ? previous : [...previous, request], retained ?? request, requestSelected);
  }, [authorized, scopeKey, requestKey, searchParams, commit]);

  return {
    open: (pointer: SettingsCanvasTarget, requestId: string, select = true) => {
      const previous = current.current;
      const requestKey = `${requestId}:${settingsCanvasKey(pointer)}`;
      if (toolRequestsRef.current.has(requestKey)) return;
      if (!previous.authorized || !previous.scope || !settingsCanvasTargetMatches(pointer, previous.scope)) return;
      toolRequestsRef.current.add(requestKey);
      if (previous.targets.some((item) => settingsCanvasKey(item) === settingsCanvasKey(pointer))) return;
      const activate = !previous.target || (select && !previous.selected);
      commit([...previous.targets, pointer], activate ? pointer : previous.target, activate ? select : previous.selected);
      if (activate && select) writeSettingsCanvasTargetInUrl(pointer, true);
    },
    targets: authorized && scope ? targets.filter((item) => settingsCanvasTargetMatches(item, scope)) : [],
    target: visibleTarget,
    selected: Boolean(visibleTarget && selected),
    select: useCallback((key?: string) => {
      const previous = current.current;
      const next = key ? previous.targets.find((item) => settingsCanvasKey(item) === key) : previous.target;
      if (!previous.authorized || !previous.scope || !next || !settingsCanvasTargetMatches(next, previous.scope)) return;
      commit(previous.targets, next, true);
      writeSettingsCanvasTargetInUrl(next, true);
    }, [commit]),
    deselect: useCallback(() => {
      const previous = current.current;
      commit(previous.targets, previous.target, false);
      const live = readSettingsCanvasTarget(new URLSearchParams(window.location.search));
      if (live && previous.target && settingsCanvasKey(live) === settingsCanvasKey(previous.target))
        writeSettingsCanvasTargetInUrl(previous.target, false);
    }, [commit]),
    close,
  };
}
