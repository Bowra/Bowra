"use client";

/** Matrix polling reprojects current file permissions even when seq is unchanged.
 * Active runs use the server interval; idle views refresh every 30 seconds.
 * Full responses replace revoked names/values. Older servers may still send
 * deltas, which retain the terminal full reload. 403/404 clears the snapshot. */

import { useCallback, useEffect, useRef } from "react";

import { MatrixApiError } from "./api-error";
import { fetchMatrixDelta, fetchMatrixSnapshot } from "./snapshot-api";
import { applyMatrixDelta, setMatrixSnapshot } from "./store";
import type { MatrixRunVM, MatrixSnapshot } from "./types";
import { snapshotRuns } from "./types";

const DEFAULT_POLL_MS = 3000;

function runIsActive(r: MatrixRunVM): boolean {
  // No active polling for paused/frozen: admin triggers resume, cells never advance themselves (M2 predicate applied per run).
  return r.status === "running" || r.status === "cancelling";
}

function activeRunIds(runs: MatrixRunVM[]): Set<string> {
  return new Set(runs.filter(runIsActive).map((r) => r.runId));
}

function anyRunActive(snap: MatrixSnapshot | null): boolean {
  return snap != null && snapshotRuns(snap).some(runIsActive);
}

function isGoneError(e: unknown): boolean {
  return e instanceof MatrixApiError && (e.status === 404 || e.status === 403);
}

export function useMatrixPolling(
  matrixId: string | null,
  snapshot: MatrixSnapshot | null,
  onUnavailable?: () => void,
): { reload: () => Promise<void> } {
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
    };
  }, []);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seqRef = useRef(0);
  const pollMsRef = useRef(DEFAULT_POLL_MS);
  const generation = useRef(0);
  const reloadToken = useRef(0);
  // Previous-tick active run_id set, maintained directly from delta payload (G-P1-1).
  const activeIdsRef = useRef<Set<string>>(new Set());
  const onUnavailableRef = useRef(onUnavailable);
  useEffect(() => {
    onUnavailableRef.current = onUnavailable;
  }, [onUnavailable]);

  // Snapshot updates (full set/delta flush) synchronize cursor.
  useEffect(() => {
    if (!snapshot) return;
    seqRef.current = Math.max(seqRef.current, snapshot.seq);
    if (snapshot.pollMs) pollMsRef.current = snapshot.pollMs;
  }, [snapshot]);

  const reload = useCallback(async () => {
    if (!matrixId || !mounted.current) return;
    const gen = generation.current;
    // Per-call token (FE-P2-5): older out-of-order same-matrix reload response must not roll back
    // snapshot/seq cursor (confirmRun can overlap terminal polling reload).
    const token = ++reloadToken.current;
    let snap: MatrixSnapshot;
    try {
      snap = await fetchMatrixSnapshot(matrixId);
    } catch (e) {
      // 404/403 → null snapshot + unavailable callback (never throw; neutral unavailable view, S-P2-4).
      if (isGoneError(e)) {
        if (gen === generation.current && token === reloadToken.current) {
          setMatrixSnapshot(null);
          onUnavailableRef.current?.();
        }
        return;
      }
      throw e;
    }
    if (gen !== generation.current || token !== reloadToken.current) return; // Already switched away/superseded by newer reload.
    seqRef.current = snap.seq;
    if (snap.pollMs) pollMsRef.current = snap.pollMs;
    setMatrixSnapshot(snap);
  }, [matrixId]);

  const active = anyRunActive(snapshot);

  useEffect(() => {
    generation.current += 1;
    const gen = generation.current;
    if (!matrixId) {
      if (timer.current) {
        clearTimeout(timer.current);
        timer.current = null;
      }
      return;
    }
    let stopped = false;
    activeIdsRef.current = snapshot ? activeRunIds(snapshotRuns(snapshot)) : new Set();
    const tick = async () => {
      if (stopped || gen !== generation.current) return;
      try {
        const res = await fetchMatrixDelta(matrixId, seqRef.current);
        if (stopped || gen !== generation.current) return;
        if (res.kind === "full") {
          // Full structural fallback uses reload token ordering (old response never overwrites newer reload, FE-P2-5).
          const token = ++reloadToken.current;
          if (stopped || gen !== generation.current || token !== reloadToken.current) return;
          seqRef.current = res.snapshot.seq;
          if (res.snapshot.pollMs) pollMsRef.current = res.snapshot.pollMs;
          activeIdsRef.current = activeRunIds(snapshotRuns(res.snapshot));
          setMatrixSnapshot(res.snapshot);
          // Full response already contains runs; empty active set makes dependency false and naturally stops timer.
        } else {
          seqRef.current = Math.max(seqRef.current, res.delta.seq);
          applyMatrixDelta(res.delta.cells, res.delta.seq, res.delta.runs);
          const nowActive = activeRunIds(res.delta.runs);
          const prevActive = activeIdsRef.current;
          activeIdsRef.current = nowActive;
          if (nowActive.size === 0) {
            // All terminal → full final reload (repair cancelled rollback row deletion/structural drift), then stop timer.
            await reload();
            return;
          }
          if ([...prevActive].some((id) => !nowActive.has(id))) {
            // Active set shrinks but nonempty: one run terminal → full final reload once, then **continue rearming**
            // (no return; remaining runs still need polling, review G-P1-1).
            await reload();
          }
        }
      } catch (e) {
        if (isGoneError(e)) {
          // Matrix deleted/unauthorized: stop timer + null snapshot (FE-P2-11 dead-polling fix + S-P3-4).
          if (stopped || gen !== generation.current) return;
          setMatrixSnapshot(null);
          onUnavailableRef.current?.();
          return;
        }
        // Other errors silent catch (retry next tick), kb-files-manager pattern.
      }
      if (stopped || gen !== generation.current) return;
      timer.current = setTimeout(tick, active ? pollMsRef.current : Math.max(30_000, pollMsRef.current));
    };
    timer.current = setTimeout(tick, active ? pollMsRef.current : Math.max(30_000, pollMsRef.current));
    return () => {
      stopped = true;
      if (timer.current) {
        clearTimeout(timer.current);
        timer.current = null;
      }
    };
    // Rebuild polling only on active-run presence/matrix switch, avoiding timer rescheduling on every cell update.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matrixId, active, reload]);

  return { reload };
}
