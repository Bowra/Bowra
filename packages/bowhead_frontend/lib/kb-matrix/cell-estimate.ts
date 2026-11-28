/**
 * Document matrix step 1 (S3): cached single-cell estimates for the per-cell rerun buttons ("重跑此格 · 约 N 点额度").
 *
 * The key carries the column's prompt version and cross-check flag, so editing the question, hint, options or
 * cross-check prices the cell again; the file's character count is fixed when it joins the matrix. Entries live for
 * a few minutes in this page session. A failed estimate leaves the button without a price (the rerun still works).
 */

import { useEffect, useState } from "react";

import { estimateCell } from "./run-api";

const TTL_MS = 5 * 60_000;

interface Entry {
  at: number;
  credits: number | null;
  pending?: Promise<number | null>;
}

const cache = new Map<string, Entry>();

export function cellEstimateKey(matrixId: string, fileId: string, columnId: string, promptVersion?: number, crossVerify?: boolean): string {
  return [matrixId, fileId, columnId, promptVersion ?? 0, crossVerify ? 1 : 0].join(":");
}

function cached(key: string): Entry | undefined {
  const entry = cache.get(key);
  if (entry && !entry.pending && Date.now() - entry.at > TTL_MS) {
    cache.delete(key);
    return undefined;
  }
  return entry;
}

function load(key: string, matrixId: string, fileId: string, columnId: string): Promise<number | null> {
  const entry = cached(key);
  if (entry?.pending) return entry.pending;
  if (entry) return Promise.resolve(entry.credits);
  const pending = estimateCell(matrixId, fileId, columnId).then(
    (estimate) => estimate.credits_estimated,
    () => null,
  );
  cache.set(key, { at: Date.now(), credits: null, pending });
  void pending.then((credits) => {
    if (credits === null) cache.delete(key);
    else cache.set(key, { at: Date.now(), credits });
  });
  return pending;
}

/** Estimated credits to rerun one cell, or null while unknown; pass a null matrixId to skip (read-only viewers). */
export function useCellRerunCredits(
  matrixId: string | null,
  fileId: string | undefined,
  columnId: string | undefined,
  promptVersion?: number,
  crossVerify?: boolean,
): number | null {
  const key = matrixId && fileId && columnId ? cellEstimateKey(matrixId, fileId, columnId, promptVersion, crossVerify) : null;
  const [state, setState] = useState<{ key: string | null; credits: number | null }>(() => ({
    key,
    credits: key ? (cached(key)?.credits ?? null) : null,
  }));
  useEffect(() => {
    if (!key || !matrixId || !fileId || !columnId) return;
    let live = true;
    void load(key, matrixId, fileId, columnId).then((credits) => {
      if (live) setState({ key, credits });
    });
    return () => {
      live = false;
    };
  }, [key, matrixId, fileId, columnId]);
  return state.key === key ? state.credits : key ? (cached(key)?.credits ?? null) : null;
}

/** Tests only. */
export function _clearCellEstimateCache(): void {
  cache.clear();
}
