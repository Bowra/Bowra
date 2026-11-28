"use client";

/** Matrix polling reprojects current file permissions even when seq is unchanged.
 * Active runs use the server interval; idle views refresh every 30 seconds.
 * Step 1 S6: during a run a poll sends since_seq with the projection digest held, and the server answers
 * with only the newer cells while nothing else of the viewer's projection changed. Deltas merge into the
 * snapshot; full responses replace it (revoked names/values included). A delta that does not fit the
 * snapshot, or fails to merge, reloads in full, and so does every run ending. Idle polls send no digest
 * and always get a full projection. 403/404 clears the snapshot.
 * Step 1 S9: the first full load of a matrix in this view tells the server it was opened; a load that fails
 * leaves that to the next one. Polls and later reloads never do. */

import { useCallback, useEffect, useRef } from "react";

import { MatrixApiError } from "./api-error";
import { fetchMatrixDelta, fetchMatrixSnapshot, type MatrixDelta } from "./snapshot-api";
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

/** Merge a delta into the snapshot held; false (nothing applied) when it does not fit or merging throws. */
function mergeDelta(delta: MatrixDelta): boolean {
  try {
    return applyMatrixDelta(delta.cells, delta.seq, delta.runs, delta.projectionDigest);
  } catch {
    return false;
  }
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
  // Step 1 S6: digest of the last full projection (deltas never change it).
  const digestRef = useRef<string | undefined>(undefined);
  const pollMsRef = useRef(DEFAULT_POLL_MS);
  const generation = useRef(0);
  const reloadToken = useRef(0);
  // Previous-tick active run_id set, maintained directly from delta payload (G-P1-1).
  const activeIdsRef = useRef<Set<string>>(new Set());
  // Step 1 S9: the matrix whose opening this view has reported.
  const openedRef = useRef<string | null>(null);
  const onUnavailableRef = useRef(onUnavailable);
  useEffect(() => {
    onUnavailableRef.current = onUnavailable;
  }, [onUnavailable]);

  // Snapshot updates (full set/delta flush) synchronize cursor.
  useEffect(() => {
    if (!snapshot) return;
    seqRef.current = Math.max(seqRef.current, snapshot.seq);
    digestRef.current = snapshot.projectionDigest;
    if (snapshot.pollMs) pollMsRef.current = snapshot.pollMs;
  }, [snapshot]);

  const reload = useCallback(async () => {
    if (!matrixId || !mounted.current) return;
    const gen = generation.current;
    // Per-call token (FE-P2-5): older out-of-order same-matrix reload response must not roll back
    // snapshot/seq cursor (confirmRun can overlap terminal polling reload).
    const token = ++reloadToken.current;
    const opened = openedRef.current !== matrixId;
    openedRef.current = matrixId;
    let snap: MatrixSnapshot;
    try {
      snap = await fetchMatrixSnapshot(matrixId, opened ? { opened: true } : undefined);
    } catch (e) {
      // A failed load records nothing on the server, so the next load of this matrix reports the opening.
      if (opened && openedRef.current === matrixId) openedRef.current = null;
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
    digestRef.current = snap.projectionDigest;
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
        // Only run polls ask for deltas; an idle poll is a full permission recheck.
        const res = await fetchMatrixDelta(matrixId, seqRef.current, active ? digestRef.current : undefined);
        if (stopped || gen !== generation.current) return;
        if (res.kind === "full") {
          // Full structural fallback uses reload token ordering (old response never overwrites newer reload, FE-P2-5).
          const token = ++reloadToken.current;
          if (stopped || gen !== generation.current || token !== reloadToken.current) return;
          seqRef.current = res.snapshot.seq;
          digestRef.current = res.snapshot.projectionDigest;
          if (res.snapshot.pollMs) pollMsRef.current = res.snapshot.pollMs;
          activeIdsRef.current = activeRunIds(snapshotRuns(res.snapshot));
          setMatrixSnapshot(res.snapshot);
          // Full response already contains runs; empty active set makes dependency false and naturally stops timer.
        } else {
          // The cursor moves only with a merged delta, so a failed merge never skips cells.
          const merged = mergeDelta(res.delta);
          if (merged) seqRef.current = Math.max(seqRef.current, res.delta.seq);
          const nowActive = activeRunIds(res.delta.runs);
          const prevActive = activeIdsRef.current;
          activeIdsRef.current = nowActive;
          if (nowActive.size === 0) {
            // All terminal → full final reload (repair cancelled rollback row deletion/structural drift), then stop timer.
            await reload();
            return;
          }
          if (!merged || [...prevActive].some((id) => !nowActive.has(id))) {
            // A delta that did not merge reloads in full. Active set shrinks but nonempty: one run terminal → full
            // final reload once. Either way **continue rearming** (no return; remaining runs still need polling,
            // review G-P1-1).
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
