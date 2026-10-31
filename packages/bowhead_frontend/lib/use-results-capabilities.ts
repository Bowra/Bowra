"use client";

import { useEffect, useSyncExternalStore } from "react";

import { appFetch } from "@/lib/base-path";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";

const WORKSPACE_DEV_OVERRIDE_KEY = "bowhead.feature.results_workspace";
const REFRESH_INTERVAL_MS = 60_000;
const LOAD_DEADLINE_MS = 10_000;
const FAILURE_RETRY_DELAYS_MS = [5_000, 10_000, 20_000] as const;

export type ResultsCapabilities = {
  workspace: boolean;
  completeList: boolean;
  scaleNav: boolean;
  interactiveHtml?: boolean;
  resolved: boolean;
  error: "unavailable" | "timeout" | "auth" | null;
};

const DISABLED_SNAPSHOT: ResultsCapabilities = {
  workspace: false,
  completeList: false,
  scaleNav: false,
  interactiveHtml: false,
  resolved: false,
  error: null,
};

let snapshot = DISABLED_SNAPSHOT;
let loadState: "idle" | "loading" | "loaded" = "idle";
let refreshTimer: number | null = null;
let activeScope: string | null = null;
let scopeCanLoad = false;
let activeRequest: AbortController | null = null;
let loadTimer: ReturnType<typeof setTimeout> | null = null;
let failureRetryTimer: ReturnType<typeof setTimeout> | null = null;
let failureRetryAttempt = 0;
const listeners = new Set<() => void>();

function readDevelopmentOverride(): boolean | null {
  if (process.env.NODE_ENV === "production" || typeof window === "undefined") {
    return null;
  }
  try {
    const value = window.localStorage?.getItem?.(WORKSPACE_DEV_OVERRIDE_KEY) ?? null;
    if (value === "1" || value === "true") return true;
    if (value === "0" || value === "false") return false;
  } catch {
    // Storage can be unavailable. Capability negotiation still fails closed.
  }
  return null;
}

function publish(next: ResultsCapabilities): void {
  if (
    snapshot.workspace === next.workspace
    && snapshot.completeList === next.completeList
    && snapshot.scaleNav === next.scaleNav
    && snapshot.interactiveHtml === next.interactiveHtml
    && snapshot.resolved === next.resolved
    && snapshot.error === next.error
  ) return;
  snapshot = next;
  listeners.forEach((listener) => listener());
}

function parseCapabilities(value: unknown): ResultsCapabilities | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const dto = value as Record<string, unknown>;
  if (
    dto.dto_version !== 1
    || typeof dto.workspace !== "boolean"
    || typeof dto.complete_list !== "boolean"
    || typeof dto.scale_nav !== "boolean"
    || dto.max_page_size !== 50
  ) return null;
  return {
    workspace: dto.workspace,
    completeList: dto.workspace && dto.complete_list,
    scaleNav: dto.workspace && dto.complete_list && dto.scale_nav,
    interactiveHtml: dto.interactive_html === true,
    resolved: true,
    error: null,
  };
}

class CapabilityAuthError extends Error {}

async function capabilityResponse(path: string, signal: AbortSignal): Promise<Response> {
  const response = await appFetch(path, { cache: "no-store", signal });
  if (response.status === 401 || response.status === 403) throw new CapabilityAuthError();
  return response;
}

async function negotiateCapabilities(signal: AbortSignal, override: boolean | null): Promise<ResultsCapabilities> {
  const response = await capabilityResponse("/api/v1/results/capabilities", signal);
  if (response.status === 404 || response.status === 405) {
    // Only a missing endpoint is evidence of an old backend. Failed health
    // requests must not turn an unknown workspace into an explicitly disabled one.
    let workspace = override;
    if (workspace === null) {
      signal.throwIfAborted();
      const health = await capabilityResponse("/health", signal);
      if (!health.ok) throw new Error("health unavailable");
      const body: unknown = await health.json();
      if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("invalid health");
      const flags = (body as Record<string, unknown>).feature_flags;
      if (flags !== undefined && (!flags || typeof flags !== "object" || Array.isArray(flags))) throw new Error("invalid flags");
      const flag = (flags as Record<string, unknown> | undefined)?.results_workspace;
      // Older health DTOs omit feature flags entirely when the feature is absent.
      if (flag !== undefined && typeof flag !== "boolean") throw new Error("invalid workspace flag");
      workspace = flag === true;
    }
    return { ...DISABLED_SNAPSHOT, workspace, resolved: true };
  }
  if (!response.ok) throw new Error("capabilities unavailable");
  const negotiated = parseCapabilities(await response.json());
  if (!negotiated) throw new Error("invalid capabilities");
  const workspace = override ?? negotiated.workspace;
  return {
    ...negotiated,
    workspace,
    completeList: workspace && negotiated.completeList,
    scaleNav: workspace && negotiated.scaleNav,
  };
}

function cancelLoad(): void {
  const request = activeRequest;
  activeRequest = null;
  if (loadTimer !== null) clearTimeout(loadTimer);
  loadTimer = null;
  loadState = "idle";
  request?.abort();
}

function publishFailure(error: NonNullable<ResultsCapabilities["error"]>): void {
  if (error === "auth" || !snapshot.resolved) {
    publish({ ...DISABLED_SNAPSHOT, error });
  }
}

function cancelFailureRetry(resetAttempt: boolean): void {
  if (failureRetryTimer !== null) clearTimeout(failureRetryTimer);
  failureRetryTimer = null;
  if (resetAttempt) failureRetryAttempt = 0;
}

function scheduleFailureRetry(): void {
  if (
    typeof window === "undefined"
    || !scopeCanLoad
    || listeners.size === 0
    || failureRetryTimer !== null
  ) return;
  const delay = FAILURE_RETRY_DELAYS_MS[failureRetryAttempt];
  if (delay === undefined) return;
  failureRetryAttempt += 1;
  failureRetryTimer = setTimeout(() => {
    failureRetryTimer = null;
    refreshWhenVisible();
  }, delay);
}

async function ensureLoaded(force = false): Promise<void> {
  if (!scopeCanLoad) return;
  const override = readDevelopmentOverride();
  if (!force && loadState === "loaded") {
    if (snapshot.resolved && override !== null && snapshot.workspace !== override) {
      publish(override
        ? { ...snapshot, workspace: true, resolved: true }
        : { ...DISABLED_SNAPSHOT, resolved: true });
    }
    return;
  }
  if (loadState === "loading") return;
  cancelFailureRetry(false);
  if (override === false) {
    loadState = "loaded";
    failureRetryAttempt = 0;
    publish({ ...DISABLED_SNAPSHOT, resolved: true });
    return;
  }
  loadState = "loading";
  if (!snapshot.resolved) publish(DISABLED_SNAPSHOT);
  const request = new AbortController();
  activeRequest = request;
  loadTimer = setTimeout(() => {
    if (activeRequest !== request) return;
    cancelLoad();
    loadState = "loaded";
    publishFailure("timeout");
    scheduleFailureRetry();
  }, LOAD_DEADLINE_MS);
  try {
    const next = await negotiateCapabilities(request.signal, override);
    if (activeRequest === request) {
      failureRetryAttempt = 0;
      publish(next);
    }
  } catch (error) {
    if (activeRequest === request) {
      const failure = error instanceof CapabilityAuthError ? "auth" : "unavailable";
      publishFailure(failure);
      if (failure !== "auth") scheduleFailureRetry();
    }
  } finally {
    if (activeRequest === request) {
      if (loadTimer !== null) clearTimeout(loadTimer);
      loadTimer = null;
      activeRequest = null;
      loadState = "loaded";
    }
  }
}

export function retryResultsCapabilities(): Promise<void> {
  return ensureLoaded(true);
}

function refreshWhenVisible(): void {
  if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
  void ensureLoaded(true);
}

function startRuntimeRefresh(): void {
  if (typeof window === "undefined" || refreshTimer !== null) return;
  refreshTimer = window.setInterval(refreshWhenVisible, REFRESH_INTERVAL_MS);
  window.addEventListener("focus", refreshWhenVisible);
  document.addEventListener("visibilitychange", refreshWhenVisible);
}

function stopRuntimeRefresh(): void {
  if (typeof window === "undefined") return;
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  cancelFailureRetry(false);
  if (!snapshot.resolved) cancelLoad();
  window.removeEventListener("focus", refreshWhenVisible);
  document.removeEventListener("visibilitychange", refreshWhenVisible);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) startRuntimeRefresh();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) stopRuntimeRefresh();
  };
}

function useScopedSnapshot() {
  const auth = useOptionalAuthSession();
  const scope = auth
    ? JSON.stringify([auth.status, auth.me?.id, auth.me?.team_id, auth.me?.auth_session_generation])
    : "standalone";
  // Mask a previous principal's snapshot during render, before effects have
  // had a chance to abort/reload the shared request.
  const value = useSyncExternalStore(
    subscribe,
    () => activeScope === scope ? snapshot : DISABLED_SNAPSHOT,
    () => DISABLED_SNAPSHOT,
  );
  return { value, scope, auth };
}

export function useResultsCapabilities(): ResultsCapabilities {
  const { value, scope, auth } = useScopedSnapshot();
  const canLoad = !auth || auth.status === "authenticated";
  const revalidateAuth = auth?.revalidateCapabilities;
  useEffect(() => {
    if (activeScope !== scope) {
      cancelLoad();
      cancelFailureRetry(true);
      activeScope = scope;
      publish(DISABLED_SNAPSHOT);
    }
    scopeCanLoad = canLoad;
    if (canLoad) void ensureLoaded();
  }, [canLoad, scope]);
  useEffect(() => {
    if (value.error === "auth") void revalidateAuth?.();
  }, [revalidateAuth, value.error]);
  return value;
}

export function useResultsCapabilitiesSnapshot(): ResultsCapabilities {
  return useScopedSnapshot().value;
}

export function resetResultsCapabilitiesForTests(): void {
  stopRuntimeRefresh();
  cancelLoad();
  cancelFailureRetry(true);
  activeScope = null;
  scopeCanLoad = false;
  snapshot = DISABLED_SNAPSHOT;
  loadState = "idle";
  listeners.clear();
}
