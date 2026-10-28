/**
 * Keeps the Agent version workbench fresh (tech-design §6 "Freshness"): refetch when a workbench tab is
 * activated and when the page becomes visible again; poll while a comparison is active or while a merged
 * CI proposal waits for its candidate; never poll while the page is hidden.
 */
import { useEffect, useRef, useState } from "react";

import { CI_MERGE_POLL_WINDOW_MS, type CiMerge } from "./agent-ci-merges";
import type { AgentVersionWorkbench } from "./agents-api";

export const WORKBENCH_POLL_MS = 5000;

const ACTIVE_EXECUTION = new Set(["queued", "running", "finalizing"]);

export function workbenchExecutionActive(workbench: Pick<AgentVersionWorkbench, "active_execution_summary"> | null): boolean {
  return ACTIVE_EXECUTION.has(workbench?.active_execution_summary?.status ?? "");
}

/**
 * Whether the workbench should poll: a comparison is queued, running or finalizing, or a CI proposal was
 * merged less than ten minutes ago and its candidate hasn't appeared (`waiting` holds only those).
 */
export function workbenchShouldPoll(
  workbench: Pick<AgentVersionWorkbench, "active_execution_summary"> | null,
  waiting: readonly Pick<CiMerge, "mergedAt">[],
  now: number,
): boolean {
  if (workbenchExecutionActive(workbench)) return true;
  return waiting.some((merge) => now - merge.mergedAt < CI_MERGE_POLL_WINDOW_MS);
}

/**
 * Wall-clock time for {@link workbenchShouldPoll}, advanced when the earliest open merge window ends. It must not
 * depend on refresh results: while refreshes fail nothing on screen changes, and the ten-minute window still has
 * to close.
 */
export function useMergeWindowClock(waiting: readonly Pick<CiMerge, "mergedAt">[]): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const current = Date.now();
    const ends = waiting.map((merge) => merge.mergedAt + CI_MERGE_POLL_WINDOW_MS).filter((end) => end > current);
    if (!ends.length) {
      // Every window has ended by the wall clock; catch up once if the last reading still saw one open.
      if (waiting.some((merge) => now - merge.mergedAt < CI_MERGE_POLL_WINDOW_MS)) setNow(current);
      return undefined;
    }
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(...ends) - current);
    return () => window.clearTimeout(timer);
  }, [now, waiting]);
  return now;
}

function pageVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

export function useWorkbenchRefresh({ refresh, activationKey, polling }: {
  /** Quiet refetch; failures keep what is on screen. */
  refresh: () => unknown;
  /** Changes when the Owner activates another workbench tab; each change refetches once. */
  activationKey: string;
  polling: boolean;
}) {
  const refreshRef = useRef(refresh);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);
  const [visible, setVisible] = useState(pageVisible);

  const mountedKey = useRef(activationKey);
  useEffect(() => {
    if (mountedKey.current === activationKey) return;
    mountedKey.current = activationKey;
    void refreshRef.current();
  }, [activationKey]);

  useEffect(() => {
    const onVisibility = () => {
      const next = pageVisible();
      setVisible(next);
      if (next) void refreshRef.current();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  useEffect(() => {
    if (!polling || !visible) return undefined;
    const timer = window.setInterval(() => void refreshRef.current(), WORKBENCH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [polling, visible]);
}
