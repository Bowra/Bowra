/**
 * Artifact data-preview cache for citation hover popovers (original data rows).
 *
 * Sibling of kb-chunk-cache.ts. A bi_query citation carries its rows inline
 * (SourceRef.preview_rows, folded by the backend from the tool result's
 * sample_rows) because its `tl_…` artifact_id is a datarc ledger ref that 404s
 * on /files. But a source backed by a *persisted* bowhead artifact (`art_…`)
 * has NO inline rows — the data is fetchable. To show the original rows on hover
 * WITHOUT a navigation/jump, we lazily fetch that artifact's row preview
 * (GET /api/v1/files/{id}/preview?mode=rows) once per artifact, cache it, and
 * resolve subsequent hovers instantly.
 *
 * Errors degrade silently to metadata-only (like KB's 403 path): a `tl_` ledger
 * ref or a viewer without the grant gets a 404/403 here and the popover falls
 * back to its non-data affordances rather than showing a broken note.
 */

"use client";

import { useEffect, useRef, useState } from "react";
import { fetchFilePreview } from "@/lib/api";

export interface ArtifactPreview {
  columns: string[];
  rows: Record<string, string>[];
  total: number | null;
}

const _PREVIEW_MAX_ROWS = 5;
const _PREVIEW_MAX_COLS = 6;
const _PREVIEW_CELL_CHARS = 80;

// artifactId → Promise<ArtifactPreview>. Promise-keyed so concurrent hovers on
// the same artifact dedupe onto one in-flight request. Bounded LRU so a long
// canvas session hovering many sources doesn't retain every preview forever.
const _MAX_CACHED = 32;
const _cache = new Map<string, Promise<ArtifactPreview>>();

export function evictArtifactPreview(key: string): void {
  _cache.delete(key);
}

export function clearArtifactPreviewCache(): void {
  _cache.clear();
}

function _touch(id: string, p: Promise<ArtifactPreview>): void {
  _cache.delete(id);
  _cache.set(id, p);
  while (_cache.size > _MAX_CACHED) {
    const oldest = _cache.keys().next().value;
    if (oldest === undefined) break;
    _cache.delete(oldest);
  }
}

function _cell(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = typeof v === "string" ? v : String(v);
  return s.length <= _PREVIEW_CELL_CHARS ? s : s.slice(0, _PREVIEW_CELL_CHARS) + "…";
}

function _normalize(
  rows: Record<string, unknown>[] | null | undefined,
  columns: string[] | null | undefined,
  total: number | null | undefined,
): ArtifactPreview {
  const src = (rows ?? []).slice(0, _PREVIEW_MAX_ROWS);
  // columns: prefer the API's, else union of row keys — capped.
  let cols = (columns ?? []).map(String);
  if (cols.length === 0) {
    for (const r of src) {
      for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k);
      if (cols.length >= _PREVIEW_MAX_COLS) break;
    }
  }
  cols = cols.slice(0, _PREVIEW_MAX_COLS);
  const outRows = src.map((r) => {
    const o: Record<string, string> = {};
    for (const c of cols) o[c] = _cell(r[c]);
    return o;
  });
  return { columns: cols, rows: outRows, total: typeof total === "number" ? total : null };
}

function loadPreview(artifactId: string): Promise<ArtifactPreview> {
  const cached = _cache.get(artifactId);
  if (cached) {
    _touch(artifactId, cached);
    return cached;
  }
  const p = fetchFilePreview(artifactId, { mode: "rows", maxRows: _PREVIEW_MAX_ROWS })
    .then((preview) => {
      if (preview.error) throw new Error(preview.error);
      return _normalize(preview.rows, preview.columns, preview.total_rows);
    })
    .catch((e) => {
      // Don't pin a failure forever — drop so the next hover can retry.
      _cache.delete(artifactId);
      throw e;
    });
  _touch(artifactId, p);
  return p;
}

export type ArtifactPreviewState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; preview: ArtifactPreview }
  | { status: "empty" }
  | { status: "error" };

/** Bowhead-persisted artifacts use the `art_` prefix; a `tl_` ledger ref (datarc
 *  bi_query) is NOT fetchable, so we never attempt /preview for it (it 404s and
 *  its rows arrive inline via SourceRef.preview_rows instead). */
export function isFetchableArtifact(artifactId: string | undefined | null): boolean {
  return typeof artifactId === "string" && artifactId.startsWith("art_");
}

/**
 * Resolve a persisted artifact's original row preview for the hover popover. Returns
 * `idle` when there is nothing fetchable (missing/non-`art_` id) so callers fall
 * back to their non-data affordances. `error`/`empty` are non-alarming — degrade
 * to metadata, don't render a broken-looking note.
 */
export function useArtifactPreview(
  artifactId: string | undefined | null,
): ArtifactPreviewState {
  const key = isFetchableArtifact(artifactId) ? (artifactId as string) : null;
  const [state, setState] = useState<ArtifactPreviewState>({ status: "idle" });
  // The artifactId the committed `state` belongs to; prevents a one-frame flash
  // of the previous artifact's rows under a newly-hovered citation.
  const stateKey = useRef<string | null>(null);

  useEffect(() => {
    if (!key) {
      setState({ status: "idle" });
      stateKey.current = null;
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    stateKey.current = key;
    loadPreview(key)
      .then((preview) => {
        if (cancelled) return;
        setState(preview.rows.length ? { status: "ready", preview } : { status: "empty" });
      })
      .catch(() => {
        if (!cancelled) setState({ status: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [key]);

  if (stateKey.current !== key) return key ? { status: "loading" } : { status: "idle" };
  return state;
}
