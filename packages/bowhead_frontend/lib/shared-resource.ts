"use client";

/**
 * Shared browser-side resources — single ownership + single cache per resource.
 *
 * Several independent components need the same data (the sidebar and the mobile
 * drawer both list sessions; the space picker and every space-scoped page both
 * need the space list). Each of them used to own a `useEffect` + `fetch` pair,
 * so a cold load asked the gateway N times for one answer, and each copy could
 * settle at a different time — a second, visible re-render on slow networks.
 *
 * A shared resource inverts that: the resource owns the request, components
 * subscribe. Concurrent readers share one in-flight promise, a settled value is
 * reused for `ttlMs`, and refresh triggers (interval, window focus, custom
 * events, explicit invalidation) fire once for all subscribers.
 *
 * This generalises the single-flight + TTL cache that `fetchMe` already applies
 * to `/auth/me` (see `lib/auth-api.ts`); prefer this module for anything new.
 *
 * Keys carry the request scope. Anything that changes the *answer* — the active
 * team, the active space — belongs in the key, so switching context reads a
 * different cache entry instead of a stale one.
 */

import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

export type SharedResourceStatus = "idle" | "loading" | "success" | "error";

export interface SharedResourceSnapshot<T> {
  status: SharedResourceStatus;
  data: T | undefined;
  error: Error | null;
  /**
   * A first load is in flight. A background refresh of data already on screen
   * deliberately does NOT set this — consumers keep rendering what they have
   * instead of blinking a spinner, and stay un-rendered when nothing changed.
   */
  loading: boolean;
}

export interface SharedResourceOptions<T> {
  /** Diagnostic name; also namespaces nothing else — keys are per-resource. */
  name: string;
  fetcher: (key: string) => Promise<T>;
  /** How long a settled value is reused without a new request. */
  ttlMs: number;
  /** Refresh cadence while at least one component subscribes. 0 disables. */
  pollMs?: number;
  /** Refresh when the tab regains focus (subject to `ttlMs`). */
  refreshOnFocus?: boolean;
  /**
   * Window events that invalidate every entry and refresh the subscribed ones.
   * Used for "the server state just changed" broadcasts.
   */
  invalidateOnEvents?: readonly string[];
  /**
   * Of `invalidateOnEvents`, the ones that change the request **context** (a team switch): an answer already in
   * flight belongs to the old context and is discarded even on a first load. Every other invalidation only means
   * "the server state moved", so a first load still shows the answer in flight while the refresh runs (see
   * `Entry.firstLoadFallback`).
   */
  contextEvents?: readonly string[];
  /**
   * Window delay applied to invalidation-driven refreshes so a burst of
   * broadcasts (turn done fires several) costs one request. Default 50ms.
   */
  coalesceMs?: number;
  /**
   * Decides whether a fresh response carries anything new. When it does not,
   * the previous snapshot is kept — identical object, so subscribers do not
   * re-render. Without it, a poll that returns the same list still churns every
   * subscriber (the composer subscribes to the session list, and re-rendering
   * the chat input every 10 seconds for an unchanged list is pure waste).
   */
  equals?: (previous: T, next: T) => boolean;
}

const DEFAULT_COALESCE_MS = 50;

const IDLE: SharedResourceSnapshot<never> = {
  status: "idle",
  data: undefined,
  error: null,
  loading: false,
};

interface Entry<T> {
  key: string;
  snapshot: SharedResourceSnapshot<T>;
  /** Wall-clock of the last settled (success or error) result. */
  settledAtMs: number;
  inFlight: Promise<T> | null;
  generation: number;
  /**
   * Oldest generation whose answer may still fill the entry while nothing is on screen: set when an invalidation
   * that did not change the context supersedes a first load; any of those requests (the first or its refreshes) may
   * land first, and the current request replaces it when it lands. null after a context change or a settled load.
   *
   * 116 dogfood (1.5.25, phone cold load): `/auth/me` took 10s, then the session list's first `/sessions` answered
   * 200 — but a "server state changed" broadcast had bumped the generation while it was in flight, so the answer was
   * dropped and the drawer stayed on the skeleton for the whole (slow) refresh round trip, 40s and more.
   */
  firstLoadFallback: number | null;
  listeners: Set<() => void>;
}

export interface SharedResource<T> {
  readonly name: string;
  /** Cached value if fresh, otherwise the in-flight or a new request. */
  read(key: string): Promise<T>;
  /** Always goes to the network, but still single-flight per key. */
  refresh(key: string): Promise<T>;
  /** Cached snapshot without triggering a request. */
  peek(key: string): SharedResourceSnapshot<T>;
  /** Drop cached values (one key, or every key when omitted). */
  invalidate(key?: string): void;
  /** Invalidate, then refresh every key that still has subscribers. */
  invalidateAndRefresh(key?: string): void;
  subscribe(key: string, listener: () => void): () => void;
  /** Test seam: drop all state, timers and listeners. */
  reset(): void;
}

export function createSharedResource<T>(
  options: SharedResourceOptions<T>,
): SharedResource<T> {
  const entries = new Map<string, Entry<T>>();
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let coalesceTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingAllKeys = false;
  let pendingKeys = new Set<string>();
  let invalidationListenersAttached = false;
  let subscriberListenersAttached = false;

  const entryFor = (key: string): Entry<T> => {
    let entry = entries.get(key);
    if (!entry) {
      entry = {
        key,
        snapshot: IDLE as SharedResourceSnapshot<T>,
        settledAtMs: 0,
        inFlight: null,
        generation: 0,
        firstLoadFallback: null,
        listeners: new Set(),
      };
      entries.set(key, entry);
    }
    return entry;
  };

  const publish = (entry: Entry<T>, next: SharedResourceSnapshot<T>) => {
    entry.snapshot = next;
    for (const listener of [...entry.listeners]) listener();
  };

  const isFresh = (entry: Entry<T>): boolean =>
    entry.snapshot.status === "success" &&
    Date.now() - entry.settledAtMs < options.ttlMs;

  const start = (entry: Entry<T>): Promise<T> => {
    if (entry.inFlight) return entry.inFlight;
    const generation = entry.generation;
    // Only announce loading while there is nothing to show. A background
    // refresh of data already on screen must not wake every subscriber.
    if (entry.snapshot.data === undefined) {
      publish(entry, { ...entry.snapshot, loading: true });
    }
    const request = options
      .fetcher(entry.key)
      .then((data) => {
        if (entry.generation !== generation) {
          // Superseded. A first load still shows this answer (not marked fresh, so the newer request or the next
          // read replaces it); with data on screen, or after a context change, it is dropped.
          if (
            entry.firstLoadFallback !== null &&
            generation >= entry.firstLoadFallback &&
            entry.snapshot.data === undefined
          ) {
            publish(entry, { status: "success", data, error: null, loading: false });
          }
          return data;
        }
        entry.firstLoadFallback = null;
        entry.settledAtMs = Date.now();
        const previous = entry.snapshot;
        if (
          previous.status === "success" &&
          previous.data !== undefined &&
          previous.error === null &&
          options.equals?.(previous.data, data)
        ) {
          // Nothing changed — keep the snapshot identity so no one re-renders.
          return previous.data;
        }
        publish(entry, {
          status: "success",
          data,
          error: null,
          loading: false,
        });
        return data;
      })
      .catch((cause: unknown) => {
        if (entry.generation !== generation) throw cause;
        const error = cause instanceof Error ? cause : new Error(String(cause));
        entry.settledAtMs = Date.now();
        publish(entry, {
          // A failed refresh keeps the last good value on screen; `error` tells
          // the component the freshest attempt did not land.
          status: entry.snapshot.data === undefined ? "error" : "success",
          data: entry.snapshot.data,
          error,
          loading: false,
        });
        throw error;
      });
    entry.inFlight = request;
    const clear = () => {
      if (entry.inFlight === request) entry.inFlight = null;
    };
    request.then(clear, clear);
    return request;
  };

  const read = (key: string): Promise<T> => {
    // Also covers one-shot readers that never subscribe (readSpaces()).
    attachInvalidationListeners();
    const entry = entryFor(key);
    if (entry.inFlight) return entry.inFlight;
    if (isFresh(entry)) return Promise.resolve(entry.snapshot.data as T);
    return start(entry);
  };

  const refresh = (key: string): Promise<T> => {
    const entry = entryFor(key);
    if (entry.inFlight) return entry.inFlight;
    return start(entry);
  };

  const invalidate = (key?: string, keepFirstLoad = false) => {
    const targets = key === undefined ? [...entries.values()] : [entries.get(key)];
    for (const entry of targets) {
      if (!entry) continue;
      // A first load in flight may still fill an empty entry (`firstLoadFallback`) unless the context changed.
      if (!keepFirstLoad) entry.firstLoadFallback = null;
      else if (entry.snapshot.data === undefined) entry.firstLoadFallback ??= entry.generation;
      // Bump first: a response already in flight must not repopulate the cache.
      entry.generation += 1;
      entry.inFlight = null;
      entry.settledAtMs = 0;
    }
  };

  const runPendingRefresh = () => {
    coalesceTimer = null;
    const all = pendingAllKeys;
    const keys = pendingKeys;
    pendingAllKeys = false;
    pendingKeys = new Set();
    for (const entry of entries.values()) {
      if (entry.listeners.size === 0) continue;
      if (!all && !keys.has(entry.key)) continue;
      void refresh(entry.key).catch(() => {
        // Subscribers read the error from the snapshot.
      });
    }
  };

  const invalidateAndRefresh = (key?: string, keepFirstLoad = false) => {
    invalidate(key, keepFirstLoad);
    if (key === undefined) pendingAllKeys = true;
    else pendingKeys.add(key);
    if (coalesceTimer !== null) return;
    const delay = options.coalesceMs ?? DEFAULT_COALESCE_MS;
    if (delay <= 0) {
      runPendingRefresh();
      return;
    }
    coalesceTimer = setTimeout(runPendingRefresh, delay);
  };

  const refreshSubscribedStale = () => {
    for (const entry of entries.values()) {
      if (entry.listeners.size === 0 || isFresh(entry)) continue;
      void refresh(entry.key).catch(() => {});
    }
  };

  const hasSubscribers = (): boolean => {
    for (const entry of entries.values()) {
      if (entry.listeners.size > 0) return true;
    }
    return false;
  };

  const onFocus = () => {
    if (document.visibilityState !== "visible") return;
    refreshSubscribedStale();
  };
  const onInvalidateEvent = (event: Event) =>
    invalidateAndRefresh(undefined, !(options.contextEvents ?? []).includes(event.type));

  /**
   * Invalidation listeners outlive the subscribers on purpose. A "the server
   * state changed" broadcast can fire while nothing is mounted (a settings
   * surface renames a space, then unmounts); dropping it would leave the cache
   * serving a value the app already knows is wrong on the next mount.
   */
  const attachInvalidationListeners = () => {
    if (invalidationListenersAttached || typeof window === "undefined") return;
    invalidationListenersAttached = true;
    for (const name of options.invalidateOnEvents ?? []) {
      window.addEventListener(name, onInvalidateEvent);
    }
  };

  /** Refresh machinery that is only meaningful while something is watching. */
  const attachSubscriberListeners = () => {
    if (subscriberListenersAttached || typeof window === "undefined") return;
    subscriberListenersAttached = true;
    if (options.pollMs) {
      pollTimer = setInterval(() => {
        // One timer for the whole resource, no matter how many components read it.
        for (const entry of entries.values()) {
          if (entry.listeners.size === 0) continue;
          void refresh(entry.key).catch(() => {});
        }
      }, options.pollMs);
    }
    if (options.refreshOnFocus) window.addEventListener("focus", onFocus);
  };

  const detachSubscriberListeners = () => {
    if (!subscriberListenersAttached) return;
    subscriberListenersAttached = false;
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (options.refreshOnFocus) window.removeEventListener("focus", onFocus);
  };

  return {
    name: options.name,
    read,
    refresh,
    // Non-creating: `peek` runs during render (useSyncExternalStore), which must
    // not mutate the entry map.
    peek: (key) => entries.get(key)?.snapshot ?? (IDLE as SharedResourceSnapshot<T>),
    invalidate,
    invalidateAndRefresh,
    subscribe(key, listener) {
      const entry = entryFor(key);
      entry.listeners.add(listener);
      attachInvalidationListeners();
      attachSubscriberListeners();
      return () => {
        entry.listeners.delete(listener);
        if (!hasSubscribers()) detachSubscriberListeners();
      };
    },
    reset() {
      detachSubscriberListeners();
      if (invalidationListenersAttached) {
        invalidationListenersAttached = false;
        for (const name of options.invalidateOnEvents ?? []) {
          window.removeEventListener(name, onInvalidateEvent);
        }
      }
      if (coalesceTimer !== null) {
        clearTimeout(coalesceTimer);
        coalesceTimer = null;
      }
      pendingAllKeys = false;
      pendingKeys = new Set();
      entries.clear();
    },
  };
}

export interface UseSharedResourceOptions {
  /** Skip subscription and fetching entirely (route/viewport gating). */
  enabled?: boolean;
  /**
   * Force a network refresh whenever this value changes. Mirrors the existing
   * `refreshKey` idiom in the session hooks.
   */
  refreshToken?: unknown;
}

export interface UseSharedResourceResult<T>
  extends SharedResourceSnapshot<T> {
  refresh: () => void;
}

/**
 * Subscribe a component to a shared resource. Mounting N components against the
 * same key issues one request, and all of them observe the same snapshot.
 */
export function useSharedResource<T>(
  resource: SharedResource<T>,
  key: string | null,
  options: UseSharedResourceOptions = {},
): UseSharedResourceResult<T> {
  const enabled = options.enabled !== false && key !== null;

  const subscribe = useCallback(
    (listener: () => void) => {
      if (!enabled || key === null) return () => {};
      return resource.subscribe(key, listener);
    },
    [enabled, key, resource],
  );

  const getSnapshot = useCallback(() => {
    if (!enabled || key === null) return IDLE as SharedResourceSnapshot<T>;
    return resource.peek(key);
  }, [enabled, key, resource]);

  const getServerSnapshot = useCallback(
    () => IDLE as SharedResourceSnapshot<T>,
    [],
  );

  const snapshot = useSyncExternalStore(
    subscribe,
    getSnapshot,
    getServerSnapshot,
  );

  useEffect(() => {
    if (!enabled || key === null) return;
    void resource.read(key).catch(() => {
      // The snapshot carries the error; unhandled rejections would be noise.
    });
  }, [enabled, key, resource]);

  const refreshToken = options.refreshToken;
  const lastRefreshToken = useRef(refreshToken);
  useEffect(() => {
    if (!enabled || key === null) return;
    if (lastRefreshToken.current === refreshToken) return;
    lastRefreshToken.current = refreshToken;
    void resource.refresh(key).catch(() => {});
  }, [enabled, key, refreshToken, resource]);

  const refresh = useCallback(() => {
    if (key === null) return;
    void resource.refresh(key).catch(() => {});
  }, [key, resource]);

  return { ...snapshot, refresh };
}
