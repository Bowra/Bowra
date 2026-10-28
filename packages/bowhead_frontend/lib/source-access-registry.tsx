"use client";

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { appFetch } from "@/lib/base-path";
import type { FilePreview } from "@/lib/api";
import { isViewerSourceAlias, type SourceRef } from "@/lib/canvas-pm-adapter/citation-types";
import { clearArtifactPreviewCache, evictArtifactPreview } from "@/lib/canvas-pm-adapter/artifact-preview-cache";
import { clearKbChunkCache } from "@/lib/canvas-pm-adapter/kb-chunk-cache";

/** Batch 2 six-state propagation (review F8). Registry interaction gate still accepts only linked_accessible;
 *  other new states have same weight as linked_unavailable (unclickable), distinguishable only in copy. */
export type SourceAccessStatus =
  | "checking"
  | "linked_accessible"
  | "linked_labeled"
  | "linked_restricted"
  | "linked_missing"
  | "unsupported"
  | "linked_unavailable";

export interface SourceAccessRecord {
  status: SourceAccessStatus;
  generation: number;
  source: SourceRef;
  preview: FilePreview | null;
}

interface SourceAccessContextValue {
  records: ReadonlyMap<string, SourceAccessRecord>;
  check: (source: SourceRef) => Promise<SourceAccessRecord>;
  revoke: (source: SourceRef) => void;
}

const SourceAccessContext = createContext<SourceAccessContextValue>({
  records: new Map(),
  check: async (source) => ({ status: "linked_unavailable", generation: 0, source: redactSource(source), preview: null }),
  revoke: () => {},
});

function aliasOf(source: SourceRef): string {
  const candidate = source.source_alias || source.id;
  return isViewerSourceAlias(candidate) ? candidate : "source-ref-v1:invalid";
}

/**
 * Cached-criteria fingerprint — must **exactly cover** every field read by early-return branch in `check`.
 *
 * Early return (no probe, directly linked_unavailable) reads three things: valid alias, `access_state`,
 * and presence of `safe_preview_url`; old cache keyed only by alias, reusing stale conclusions even when
 * same alias had changed criteria:
 *   - **Revoked access** mid-session (`access_state` becomes unavailable) → still shows prior accessible state,
 *     without redaction either (security impact);
 *   - **Granted access** mid-session → still shows prior unavailable state and **never requests again** (measured 0 fetches),
 *     unable to self-heal until refresh.
 * Criteria in key resolve both. Beyond alias, use only **presence** of `safe_preview_url`: URL can include
 * per-request signature parameters; using full string would recheck on every hover.
 */
function accessFingerprint(source: SourceRef): string {
  return [
    isViewerSourceAlias(source.source_alias || source.id) ? "1" : "0",
    source.access_state ?? "",
    source.safe_preview_url ? "1" : "0",
  ].join("|");
}

/**
 * Only these probe statuses **definitively deny access / establish absence**: unauthenticated (401), forbidden (403), missing (404), deleted (410).
 * Only these erase source identity (`onRedactAlias` rewrites canvas store). 5xx, 429, 408, network errors, and timeouts
 * are transient: show unavailable this time, but do not erase identity or cache; probe again on next hover — otherwise one glitch
 * makes source unreadable for whole session (redaction sets linked_unavailable, triggering `check` early return without future requests).
 */
const DENIED_PROBE_STATUSES: ReadonlySet<number> = new Set([401, 403, 404, 410]);

class DeniedProbeError extends Error {}

export function redactSource(source: SourceRef): SourceRef {
  return {
    id: aliasOf(source),
    source_alias: aliasOf(source),
    access_state: "linked_unavailable",
    generic_type: source.generic_type ?? "other_source",
    ordinal: source.ordinal,
  };
}

export function SourceAccessRegistryProvider({
  children,
  scopeKey = "default",
  onRedactAlias,
}: {
  children: React.ReactNode;
  scopeKey?: string;
  onRedactAlias?: (sourceAlias: string) => void;
}) {
  const [records, setRecords] = useState<Map<string, SourceAccessRecord>>(new Map());
  const generations = useRef(new Map<string, number>());
  const inFlight = useRef(new Map<string, { fingerprint: string; promise: Promise<SourceAccessRecord> }>());
  // Cache resolved outcomes. `inFlight` deduplicates only **concurrent** calls and deletes upon resolution ⇒ formerly each
  // hover of same reference reset state to "checking" and **issued another network request** (20-row preview),
  // showing a spinner every hover, then enlarged content making popover jump.
  // Use ref rather than `records` state: state makes `check` a dependency and recreates it on every record change,
  // while consumer effects depend on `check` ⇒ infinite rechecks.
  //
  // Still index by alias (`revoke` must clear all source caches at once), but **store criteria fingerprint** and compare
  // on hit: changed fingerprint means a different determination, invalidating old conclusion. See `accessFingerprint`.
  const resolved = useRef(new Map<string, { fingerprint: string; record: SourceAccessRecord }>());
  // A per-provider epoch prevents a response started in one report/team scope
  // from becoming current after the provider is reused for another scope.  A
  // per-alias generation alone is insufficient because scope reset clears that
  // map and the next request can reuse generation 1.
  const scopeEpoch = useRef(0);

  const revoke = useCallback((source: SourceRef) => {
    const alias = aliasOf(source);
    const generation = (generations.current.get(alias) ?? 0) + 1;
    generations.current.set(alias, generation);
    inFlight.current.delete(alias);
    resolved.current.delete(alias); // Revocation invalidates immediately; determine again on next hover
    evictArtifactPreview(alias);
    clearArtifactPreviewCache();
    // KB chunk cache keys actual KB file IDs, while here only report aliases (source-ref-v1:…) exist, without matching IDs,
    // so clear entire cache.
    clearKbChunkCache();
    onRedactAlias?.(alias);
    setRecords((current) => {
      const next = new Map(current);
      next.set(alias, { status: "linked_unavailable", generation, source: redactSource(source), preview: null });
      return next;
    });
  }, [onRedactAlias]);

  const check = useCallback(async (source: SourceRef): Promise<SourceAccessRecord> => {
    const alias = aliasOf(source);
    const fingerprint = accessFingerprint(source);
    // Concurrent deduplication also respects criteria: changed criteria require **a separate request**,
    // otherwise access revoked/granted during probe returns conclusion based on old criteria.
    const existing = inFlight.current.get(alias);
    if (existing && existing.fingerprint === fingerprint) return existing.promise;
    // All criteria derive from source itself; predetermined unavailable sources go directly unavailable, **without checking**.
    // Passing through checking makes useSourceAccess see unavailable source not linked_unavailable and revoke it;
    // revoke clears cache and changes canvas store ⇒ new source object ⇒ panel effect rechecks ⇒ checking again,
    // creating infinite report-page updates until recovery mode crashes in.
    if (!isViewerSourceAlias(source.source_alias || source.id)
      || source.access_state !== "linked_accessible"
      || !source.safe_preview_url) {
      // Still invalidate this alias's in-flight probes and old conclusions: delayed probe started under old criteria
      // must not restore an unavailable source to accessible or bring back old preview.
      const generation = (generations.current.get(alias) ?? 0) + 1;
      generations.current.set(alias, generation);
      inFlight.current.delete(alias);
      const unavailable: SourceAccessRecord = {
        status: "linked_unavailable",
        generation,
        source: redactSource(source),
        preview: null,
      };
      resolved.current.set(alias, { fingerprint, record: unavailable });
      evictArtifactPreview(alias);
      clearArtifactPreviewCache();
      clearKbChunkCache();
      onRedactAlias?.(alias);
      setRecords((current) => current.get(alias)?.status === "linked_unavailable"
        ? current
        : new Map(current).set(alias, unavailable));
      return unavailable;
    }
    // Reuse resolved determination directly, **without resetting state or another request**.
    // Cache invalidates on revoke / scope reset / **criteria changes**.
    const cached = resolved.current.get(alias);
    if (cached && cached.fingerprint === fingerprint) return cached.record;
    const requestScopeEpoch = scopeEpoch.current;
    const generation = (generations.current.get(alias) ?? 0) + 1;
    generations.current.set(alias, generation);
    const checking: SourceAccessRecord = {
      status: "checking",
      generation,
      source: redactSource(source),
      preview: null,
    };
    setRecords((current) => new Map(current).set(alias, checking));

    // Cache only **deterministic** outcomes (successful probe); early returns already handled above. Never cache probe failures; allow retry on next hover;
    // only definitive denial/absence erases identity (see DENIED_PROBE_STATUSES), never transient failures.
    let cacheable = true;
    let transient = false;
    const previewUrl = source.safe_preview_url;
    const promise = (async (): Promise<SourceAccessRecord> => {
      try {
        const separator = previewUrl.includes("?") ? "&" : "?";
        const response = await appFetch(`${previewUrl}${separator}mode=auto&max_rows=20`, {
          credentials: "include",
        });
        if (!response.ok) {
          if (DENIED_PROBE_STATUSES.has(response.status)) throw new DeniedProbeError(`source probe ${response.status}`);
          throw new Error(`source probe ${response.status}`);
        }
        const preview = await response.json() as FilePreview;
        if (scopeEpoch.current !== requestScopeEpoch || generations.current.get(alias) !== generation) {
          return { ...checking, status: "linked_unavailable" };
        }
        return { status: "linked_accessible", generation, source, preview };
      } catch (error) {
        // Erase source identity only on definitive denial/absence; all others (transient statuses, network errors, timeouts, response parse failures) are transient.
        cacheable = false;
        transient = !(error instanceof DeniedProbeError);
        return { ...checking, status: "linked_unavailable" };
      }
    })();
    inFlight.current.set(alias, { fingerprint, promise });
    const result = await promise;
    if (scopeEpoch.current === requestScopeEpoch && generations.current.get(alias) === generation) {
      inFlight.current.delete(alias);
      if (cacheable) resolved.current.set(alias, { fingerprint, record: result });
      if (result.status === "linked_unavailable" && !transient) {
        evictArtifactPreview(alias);
        clearArtifactPreviewCache();
        clearKbChunkCache();
        onRedactAlias?.(alias);
      }
      setRecords((current) => new Map(current).set(alias, result));
    }
    return result;
  }, [onRedactAlias]);

  useEffect(() => () => {
    scopeEpoch.current += 1;
    generations.current.clear();
    inFlight.current.clear();
    resolved.current.clear();
    clearArtifactPreviewCache();
    clearKbChunkCache();
  }, []);

  useEffect(() => {
    scopeEpoch.current += 1;
    generations.current.clear();
    inFlight.current.clear();
    resolved.current.clear();
    clearArtifactPreviewCache();
    clearKbChunkCache();
    setRecords(new Map());
  }, [scopeKey]);

  const value = useMemo(() => ({ records, check, revoke }), [records, check, revoke]);
  return <SourceAccessContext.Provider value={value}>{children}</SourceAccessContext.Provider>;
}

export function useSourceAccess(source: SourceRef | undefined): SourceAccessRecord | null {
  const { records, revoke } = useContext(SourceAccessContext);
  const alias = source ? aliasOf(source) : "";
  const record = source ? records.get(alias) : undefined;
  const sourceUnavailable = source !== undefined
    && (!isViewerSourceAlias(source.source_alias || source.id) || source.access_state !== "linked_accessible");
  useEffect(() => {
    if (source && sourceUnavailable && record?.status !== "linked_unavailable") revoke(source);
  }, [record?.status, revoke, source, sourceUnavailable]);
  if (!source) return null;
  if (sourceUnavailable) {
    return {
      status: "linked_unavailable",
      generation: record?.generation ?? 0,
      source: redactSource(source),
      preview: null,
    };
  }
  if (record?.status === "linked_accessible") return { ...record, source };
  return record ?? {
    status: source.access_state ?? "linked_unavailable",
    generation: 0,
    source: source.access_state === "linked_accessible" ? source : redactSource(source),
    preview: null,
  };
}

export function useSourceAccessActions(): Pick<SourceAccessContextValue, "check" | "revoke"> {
  const { check, revoke } = useContext(SourceAccessContext);
  return { check, revoke };
}
