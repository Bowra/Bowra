"use client";

import { useEffect, useSyncExternalStore } from "react";

import { listAccessRequests } from "./access-requests-api";
import { fetchApprovals, fetchScheduledJobs } from "./api";
import { fetchFeedbackPendingCount } from "./feedback-candidates-api";
import { getAuthCapabilities } from "./invite-api";
import type { SettingsCounterKey } from "./settings-destinations";
import { onSettingsNavCountsChanged } from "./settings-nav-signal";

/**
 * Shared data of the sidebar settings directory (P2 batch 4): row-end counters and the deployment facts
 * behind local-only rows. One module-level store serves every mounted directory (expanded sidebar, icon
 * rail, mobile drawer), so they never request the same thing twice:
 * - counters are fetched at most once per TTL for the same viewer, concurrent callers share the request;
 * - a page that changes a count (deciding an approval) calls `invalidateSettingsNavCounts()`;
 * - deployment capabilities are fetched once per page load.
 * A failed request leaves that counter unset (no pill), it is retried after the TTL.
 */
export const SETTINGS_NAV_COUNTS_TTL_MS = 45_000;

export type SettingsCounters = Partial<Record<SettingsCounterKey, number>>;

export interface SettingsCountScope {
  /** Viewer and capability fingerprint: counts never carry over to another user or role. */
  key: string;
  approvals: boolean;
  scheduled: boolean;
  accessRequests: boolean;
  feedbackLoop: boolean;
}

interface CountsState {
  scopeKey: string | null;
  fetchedAt: number;
  counts: SettingsCounters;
  inflight: Promise<void> | null;
  version: number;
}

let state: CountsState = { scopeKey: null, fetchedAt: 0, counts: {}, inflight: null, version: 0 };
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of Array.from(listeners)) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

const EMPTY: SettingsCounters = {};

/** Fetches the counters unless a fresh result (or a request in flight) exists for the same scope. */
export function refreshSettingsNavCounts(scope: SettingsCountScope, now = Date.now()): Promise<void> {
  if (state.scopeKey === scope.key) {
    if (state.inflight) return state.inflight;
    if (state.fetchedAt && now - state.fetchedAt < SETTINGS_NAV_COUNTS_TTL_MS) return Promise.resolve();
  } else {
    state = { ...state, scopeKey: scope.key, counts: {}, fetchedAt: 0, inflight: null };
  }
  const key = scope.key;
  const next: SettingsCounters = {};
  const jobs: Promise<unknown>[] = [];
  const track = (counter: SettingsCounterKey, request: Promise<number>) =>
    jobs.push(request.then((value) => (next[counter] = value)).catch(() => undefined));
  if (scope.scheduled) {
    track("scheduled_active", fetchScheduledJobs().then((d) => d.items.filter((job) => job.status === "active").length));
  }
  if (scope.approvals) track("approvals_pending", fetchApprovals("pending").then((items) => items.length));
  if (scope.feedbackLoop) track("feedback_pending", fetchFeedbackPendingCount().then((d) => d.count));
  if (scope.accessRequests) {
    track("access_requests_pending", listAccessRequests("pending").then((d) => d.items.length));
  }
  const inflight = Promise.all(jobs).then(() => {
    if (state.scopeKey !== key) return;
    state = { ...state, counts: next, fetchedAt: Date.now(), inflight: null };
    emit();
  });
  state = { ...state, inflight };
  return inflight;
}

/** Marks the counters stale; mounted directories refetch right away. */
export function invalidateSettingsNavCounts(): void {
  state = { ...state, fetchedAt: 0, version: state.version + 1 };
  emit();
}

onSettingsNavCountsChanged(invalidateSettingsNavCounts);

export function useSettingsNavCounts(scope: SettingsCountScope, enabled: boolean, refreshKey: string): SettingsCounters {
  const snapshot = useSyncExternalStore(
    subscribe,
    () => (state.scopeKey === scope.key ? state.counts : EMPTY),
    () => EMPTY,
  );
  const version = useSyncExternalStore(subscribe, () => state.version, () => 0);
  const { key, approvals, accessRequests, feedbackLoop, scheduled } = scope;
  useEffect(() => {
    if (!enabled) return;
    void refreshSettingsNavCounts({ key, approvals, accessRequests, feedbackLoop, scheduled });
  }, [accessRequests, enabled, feedbackLoop, key, refreshKey, scheduled, approvals, version]);
  return snapshot;
}

export interface DeploymentFacts {
  local: boolean;
  openapi: boolean;
}

let deploymentRequest: Promise<DeploymentFacts | null> | null = null;
let deploymentFacts: DeploymentFacts | null = null;

/**
 * Deployment mode facts, requested once per page load and shared with the settings sidebar.
 * Resolves to `null` when they cannot be read; callers then keep their own fallback.
 */
export function loadDeploymentFacts(): Promise<DeploymentFacts | null> {
  deploymentRequest ??= getAuthCapabilities()
    .then((capabilities) => {
      const local = capabilities?.deployment_mode === "local";
      // Same fallback as SettingsShell: an older gateway without the field follows the deployment mode.
      deploymentFacts = { local, openapi: capabilities?.team_openapi ?? local };
      emit();
      return deploymentFacts;
    })
    .catch(() => {
      // Local-only rows stay hidden; retry on the next page load.
      return null;
    });
  return deploymentRequest;
}

/** Deployment mode facts for local-only rows; requested once per page load and shared. */
export function useDeploymentFacts(enabled: boolean): DeploymentFacts {
  const facts = useSyncExternalStore(subscribe, () => deploymentFacts, () => null);
  useEffect(() => {
    if (!enabled) return;
    void loadDeploymentFacts();
  }, [enabled]);
  return facts ?? DEFAULT_DEPLOYMENT;
}

const DEFAULT_DEPLOYMENT: DeploymentFacts = { local: false, openapi: false };

/** Test helper: forget all cached state. */
export function resetSettingsNavCountsForTests(): void {
  state = { scopeKey: null, fetchedAt: 0, counts: {}, inflight: null, version: 0 };
  deploymentRequest = null;
  deploymentFacts = null;
  emit();
}
