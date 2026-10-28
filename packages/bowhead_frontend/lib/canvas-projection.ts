/**
 * Normalized canvas projection: reduce ReportSections to stable content, stripping bookkeeping/transient state
 * for two uses (canvas-comprehensive Batch 1):
 *   1. Final-state reconciliation drift counts (reconcileCanvasWithServer, AC-1.1c): project before and after,
 *      then diff; nonzero detects hidden emit/frame-loss bugs.
 *   2. Assert diff==0 for live completion versus F5 reload (WP-7): self-diff runtime A (live SSE reducer accumulation)
 *      against runtime B (F5 hydration directly loading server DTOs).
 *
 * **No golden assertions**: section ids are UUIDs (persistent within a session, different across runs), requiring self-diff.
 *
 * Exempt fields (EXEMPT_KEYS) are bookkeeping or potentially asymmetric between the two production paths:
 *   - `rev`: bumped by patches, a pure counter.
 *   - `superseded`: visual collapse state, **compared separately in projections** (excluded from contentHash to avoid duplicate signals).
 *   - `superseded_by_turn`: collapse bookkeeping (which turn collapsed it).
 *   - `turn_id`: server denormalizes it onto sections; live reducers may omit it, causing false drift.
 * FE ReportSection lacks server status/created_at/updated_at (those live on CanvasStore.Section,
 * absent after to_report_section). Streaming/buffer transient state lives in independent CanvasState Sets,
 * outside section objects, so projecting `sections[]` naturally excludes it. See sprint impl-notes.md.
 */

import type { ReportSection } from "./types";

export interface ProjectedSection {
  id: string;
  kind: string;
  orderIndex: number;
  superseded: boolean;
  contentHash: string;
}

const EXEMPT_KEYS = new Set([
  "rev",
  "superseded",
  "superseded_by_turn",
  "turn_id",
]);

/** Stable serialization: sort object keys + strip EXEMPT_KEYS; same content with different field order/bookkeeping yields the same string. */
function stableStringify(v: unknown): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v) ?? "null";
  if (Array.isArray(v)) return "[" + v.map(stableStringify).join(",") + "]";
  const obj = v as Record<string, unknown>;
  const keys = Object.keys(obj).filter((k) => !EXEMPT_KEYS.has(k)).sort();
  return "{" + keys.map((k) => JSON.stringify(k) + ":" + stableStringify(obj[k])).join(",") + "}";
}

/** FNV-1a 32-bit: deterministic, dependency-free drift-detection hash (not for security). */
function hashString(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16);
}

export function projectCanvasForDiff(sections: ReportSection[]): ProjectedSection[] {
  return sections.map((sec, i) => {
    const any = sec as unknown as Record<string, unknown>;
    return {
      id: String(any.id ?? ""),
      kind: String(any.kind ?? ""),
      orderIndex: i,
      superseded: Boolean(any.superseded),
      contentHash: hashString(stableStringify(sec)),
    };
  });
}

/** Symmetric diff count: +1 for any missing id or differing contentHash/superseded/orderIndex. Zero means identical. */
export function diffProjections(a: ProjectedSection[], b: ProjectedSection[]): number {
  const mapA = new Map(a.map((p) => [p.id, p]));
  const mapB = new Map(b.map((p) => [p.id, p]));
  let diff = 0;
  for (const [id, pa] of mapA) {
    const pb = mapB.get(id);
    if (
      !pb ||
      pb.contentHash !== pa.contentHash ||
      pb.superseded !== pa.superseded ||
      pb.orderIndex !== pa.orderIndex
    ) {
      diff++;
    }
  }
  for (const id of mapB.keys()) {
    if (!mapA.has(id)) diff++;
  }
  return diff;
}
