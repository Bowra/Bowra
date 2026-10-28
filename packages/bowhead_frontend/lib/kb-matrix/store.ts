/**
 * kb-matrix — external store for the active matrix snapshot.
 *
 * Pattern mirrors lib/plan-store.ts (hand-rolled useSyncExternalStore, the
 * repo's store idiom — PRD FR-24 shape, M1 skeleton). Rows/cells are replaced
 * immutably per patch so memoized rows skip re-render; M2 adds the polling
 * diff + flash bookkeeping on top of the same store without restructuring.
 */

import { useSyncExternalStore } from "react";

import type { MatrixCellVM, MatrixRunVM, MatrixSnapshot } from "./types";
import { snapshotRuns } from "./types";

let _snapshot: MatrixSnapshot | null = null;
const _listeners = new Set<() => void>();

// M2 FR-24: poll callback → buffer → ~100ms batch flush; 480ms pulse at completion.
const FLUSH_MS = 100;
const FLASH_MS = 480;
let _pending: Map<string, MatrixCellVM> = new Map();
let _pendingRuns: MatrixRunVM[] | undefined;
let _pendingSeq: number | undefined;
let _flushTimer: ReturnType<typeof setTimeout> | null = null;
const _flashTimers = new Set<ReturnType<typeof setTimeout>>();

// M3 §4.5 terminal stickiness (PRD-P1-1): staggered run completions let the server's 5-terminal-run window evict
// runs the user has not seen yet; retain observed terminal runs locally. Clear on matrix switch/unmount.
const _RUN_TERMINAL = new Set<MatrixRunVM["status"]>(["succeeded", "failed", "cancelled"]);
const _seenTerminalRuns = new Map<string, MatrixRunVM>();

function _emit(): void {
  for (const l of Array.from(_listeners)) l();
}

/** server runs ∪ locally observed terminal runs (server order first, sticky runs appended). */
function _mergeRuns(serverRuns: MatrixRunVM[]): MatrixRunVM[] {
  for (const r of serverRuns) {
    if (_RUN_TERMINAL.has(r.status)) _seenTerminalRuns.set(r.runId, r);
  }
  const serverIds = new Set(serverRuns.map((r) => r.runId));
  const merged = [...serverRuns];
  for (const [id, r] of _seenTerminalRuns) {
    if (!serverIds.has(id)) merged.push(r);
  }
  return merged;
}

export function setMatrixSnapshot(snap: MatrixSnapshot | null): void {
  // Matrix switch/unmount → discard unflushed deltas and pulse timers (unmount cleanup, impl review).
  _pending = new Map();
  _pendingRuns = undefined;
  _pendingSeq = undefined;
  if (_flushTimer) {
    clearTimeout(_flushTimer);
    _flushTimer = null;
  }
  for (const t of _flashTimers) clearTimeout(t);
  _flashTimers.clear();
  if (snap === null || snap.matrixId !== _snapshot?.matrixId) {
    _seenTerminalRuns.clear();
  }
  _snapshot = snap === null ? null : { ...snap, runs: _mergeRuns(snapshotRuns(snap)) };
  _emit();
}

/** Delta-protocol entry: cells keyed `fileId:columnId`; buffer, then apply together.
 * Mark running/queued→done cells flashing; clear after FLASH_MS (pulse animation). */
export function applyMatrixDelta(cells: Record<string, MatrixCellVM>, seq: number, runs?: MatrixRunVM[]): void {
  for (const [key, cell] of Object.entries(cells)) _pending.set(key, cell);
  _pendingSeq = seq;
  if (runs !== undefined) _pendingRuns = runs;
  if (_flushTimer) return;
  _flushTimer = setTimeout(_flushPending, FLUSH_MS);
}

function _flushPending(): void {
  _flushTimer = null;
  if (!_snapshot || (_pending.size === 0 && _pendingRuns === undefined && _pendingSeq === undefined)) return;
  const byFile = new Map<string, Map<string, MatrixCellVM>>();
  const flashKeys: Array<{ fileId: string; columnId: string }> = [];
  for (const [key, cell] of _pending) {
    const sep = key.indexOf(":");
    if (sep < 0) continue;
    const fileId = key.slice(0, sep);
    const columnId = key.slice(sep + 1);
    const prev = _snapshot.rows.find((r) => r.fileId === fileId)?.cells[columnId];
    const landed = cell.status === "done" || cell.status === "needs_review";
    const wasInFlight = !prev || prev.status === "queued" || prev.status === "running";
    const flash = landed && wasInFlight;
    if (flash) flashKeys.push({ fileId, columnId });
    const bag = byFile.get(fileId) ?? new Map<string, MatrixCellVM>();
    bag.set(columnId, flash ? { ...cell, flash: true } : cell);
    byFile.set(fileId, bag);
  }
  _pending = new Map();
  const runs = _pendingRuns;
  const seq = _pendingSeq;
  _pendingRuns = undefined;
  _pendingSeq = undefined;
  _snapshot = {
    ..._snapshot,
    seq: seq ?? _snapshot.seq,
    // runs = server list ∪ local sticky terminal runs (PRD-P1-1); sync deprecated `run` compatibility field.
    ...(runs !== undefined ? { runs: _mergeRuns(runs), run: runs[0] ?? null } : {}),
    rows:
      byFile.size === 0
        ? _snapshot.rows
        : _snapshot.rows.map((r) => {
            const bag = byFile.get(r.fileId);
            if (!bag) return r;
            const cells = { ...r.cells };
            for (const [columnId, cell] of bag) cells[columnId] = cell;
            return { ...r, cells };
          }),
  };
  _emit();
  if (flashKeys.length > 0) {
    const t = setTimeout(() => {
      _flashTimers.delete(t);
      if (!_snapshot) return;
      _snapshot = {
        ..._snapshot,
        rows: _snapshot.rows.map((r) => {
          let changed = false;
          const cells = { ...r.cells };
          for (const fk of flashKeys) {
            if (fk.fileId !== r.fileId) continue;
            const c = cells[fk.columnId];
            if (c?.flash) {
              cells[fk.columnId] = { ...c, flash: false };
              changed = true;
            }
          }
          return changed ? { ...r, cells } : r;
        }),
      };
      _emit();
    }, FLASH_MS);
    _flashTimers.add(t);
  }
}

/** Immutable single-cell patch — only the touched row changes reference. */
export function patchMatrixCell(fileId: string, columnId: string, cell: MatrixCellVM): void {
  if (!_snapshot) return;
  _snapshot = {
    ..._snapshot,
    rows: _snapshot.rows.map((r) =>
      r.fileId === fileId ? { ...r, cells: { ...r.cells, [columnId]: cell } } : r,
    ),
  };
  _emit();
}

/** Row-level patch (M1 Mark verified maps verify_status onto every done cell). */
export function patchMatrixRow(
  fileId: string,
  fn: (row: MatrixSnapshot["rows"][number]) => MatrixSnapshot["rows"][number],
): void {
  if (!_snapshot) return;
  _snapshot = {
    ..._snapshot,
    rows: _snapshot.rows.map((r) => (r.fileId === fileId ? fn(r) : r)),
  };
  _emit();
}

function _subscribe(cb: () => void): () => void {
  _listeners.add(cb);
  return () => _listeners.delete(cb);
}

function _get(): MatrixSnapshot | null {
  return _snapshot;
}

export function useMatrixSnapshot(): MatrixSnapshot | null {
  return useSyncExternalStore(_subscribe, _get, _get);
}
