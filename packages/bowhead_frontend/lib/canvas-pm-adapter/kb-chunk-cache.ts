/** Citation content only deduplicates pending requests, scoped to the viewing space.
 * Every reopening reauthorizes on the server; historical chunks use their exact ID. */

"use client";

import { getActiveSpaceId } from "@/lib/space-storage";
import { useEffect, useRef, useState } from "react";
import { fetchKbFileChunk, fetchKbFileChunks, type KbChunkSummary } from "@/lib/kb-api";



interface Entry<T> {
  fileId: string;
  promise: Promise<T>;

}

// Promise-keyed so concurrent hovers on the same file/chunk dedupe onto a single
// in-flight request. Bounded LRU: a long-lived session that hovers citations
// across many KB files must not retain every file's full chunk content for the
// page lifetime — cap the entries and evict least-recently-used.
const _MAX_CACHED_FILES = 24;
const _MAX_CACHED_CHUNKS = 200;
// fileId → Map<chunkId, chunk>
const _fileChunkCache = new Map<string, Entry<Map<string, KbChunkSummary>>>();
// fileId\0chunkId → chunk | null (null = file no longer contains this chunk)
const _singleChunkCache = new Map<string, Entry<KbChunkSummary | null>>();

/** Evict all caches for a file (file-level lists and individual chunks). Knowledge base pages call this after reprocessing / deletion / scope changes. */
export function evictKbChunks(fileId: string): void {
  for (const [key, entry] of _fileChunkCache) {
    if (entry.fileId === fileId) _fileChunkCache.delete(key);
  }
  for (const [key, entry] of _singleChunkCache) {
    if (entry.fileId === fileId) _singleChunkCache.delete(key);
  }
}

export function clearKbChunkCache(): void {
  _fileChunkCache.clear();
  _singleChunkCache.clear();
}

function fresh<T>(cache: Map<string, Entry<T>>, key: string): Entry<T> | null {
  const entry = cache.get(key);
  if (!entry) return null;
  // Map preserves insertion order → delete+set moves to MRU end.
  cache.delete(key);
  cache.set(key, entry);
  return entry;
}

function remember<T>(cache: Map<string, Entry<T>>, key: string, entry: Entry<T>, cap: number): void {
  cache.delete(key);
  cache.set(key, entry);
  while (cache.size > cap) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

function track<T>(cache: Map<string, Entry<T>>, key: string, fileId: string, load: () => Promise<T>, cap: number): Promise<T> {
  const entry: Entry<T> = { fileId, promise: Promise.resolve() as Promise<T> };
  entry.promise = load().finally(() => {
    if (cache.get(key) === entry) cache.delete(key);
  });
  remember(cache, key, entry, cap);
  return entry.promise;
}

function loadFileChunkMap(fileId: string, spaceId?: string): Promise<Map<string, KbChunkSummary>> {
  const key = `${spaceId ?? ""}\0${fileId}`;
  const cached = fresh(_fileChunkCache, key);
  if (cached) return cached.promise;
  return track(
    _fileChunkCache,
    key,
    fileId,
    () => fetchKbFileChunks(fileId, spaceId).then((res) => {
      const map = new Map<string, KbChunkSummary>();
      for (const c of res.chunks) map.set(c.chunk_id, c);
      return map;
    }),
    _MAX_CACHED_FILES,
  );
}

function loadSingleChunk(fileId: string, chunkId: string, spaceId?: string): Promise<KbChunkSummary | null> {
  const key = `${spaceId ?? ""}\0${fileId}\0${chunkId}`;
  const cached = fresh(_singleChunkCache, key);
  if (cached) return cached.promise;
  return track(_singleChunkCache, key, fileId, () => fetchKbFileChunk(fileId, chunkId, spaceId), _MAX_CACHED_CHUNKS);
}

export type KbChunkState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; content: string; chunk: KbChunkSummary }
  | { status: "empty" }
  | { status: "error" };

/**
 * Resolve a single chunk's original text for the hover popover. Returns `idle` when
 * there isn't enough to fetch (missing file/chunk id) so callers fall back to
 * metadata-only rendering gracefully. `error`/`empty` are likewise non-alarming
 * — a report viewer who lacks the KB-file grant gets a 403 here and the popover
 * should degrade to metadata, not show a broken-looking note.
 *
 * `single`: fetch only this chunk (chat source cards). By default fetch the full file list (matrix and other callers needing multiple chunks per file).
 */
export function useKbChunkContent(
  fileId: string | undefined,
  chunkId: string | undefined,
  spaceId?: string,
  { single = false }: { single?: boolean } = {},
): KbChunkState {
  spaceId = spaceId ?? getActiveSpaceId() ?? undefined;
  const key = fileId && chunkId ? `${spaceId ?? ""}\0${fileId}\0${chunkId}` : null;
  const [state, setState] = useState<KbChunkState>({ status: "idle" });
  // The (fileId,chunkId) the committed `state` belongs to. Updated inside the
  // effect, so a render where `key` just changed but the effect hasn't re-run
  // yet sees a mismatch and reports loading — preventing a one-frame flash of
  // the previous chunk's passage under the new citation.
  const stateKey = useRef<string | null>(null);

  useEffect(() => {
    if (!key || !fileId || !chunkId) {
      setState({ status: "idle" });
      stateKey.current = null;
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    stateKey.current = key;
    const load = single
      ? loadSingleChunk(fileId, chunkId, spaceId)
      : loadFileChunkMap(fileId, spaceId).then((map) => map.get(chunkId) ?? null);
    load
      .then((chunk) => {
        if (cancelled) return;
        const content = (chunk?.content ?? "").trim();
        setState(chunk && content ? { status: "ready", content, chunk } : { status: "empty" });
      })
      .catch(() => {
        if (!cancelled) setState({ status: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [key, fileId, chunkId, spaceId, single]);

  if (stateKey.current !== key) return key ? { status: "loading" } : { status: "idle" };
  return state;
}
