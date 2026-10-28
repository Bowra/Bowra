"use client";

/**
 * kb-matrix M1 — hover citation preview (level 1 of three-level source interaction).
 * Anchored to the cell's cite glyph; shows a 3-4 line passage with the
 * supporting sentence highlighted, or the degrade note when the excerpt
 * cannot be located (no chunk_id / no verbatim match). Content resolves via
 * useKbChunkContent (exact historical chunk, reauthorized on opening).
 */

import { useTranslations } from "next-intl";

import { useKbChunkContent } from "@/lib/canvas-pm-adapter/kb-chunk-cache";
import type { MatrixCellVM } from "@/lib/kb-matrix/types";

/** approx-match threshold (Hypothesis degradation-ladder approach: highlight only sufficiently close matches, never force one). */
const APPROX_MIN_RATIO = 0.72;

function approxFindStripped(stripped: string, probe: string): number {
  // Normalized sliding window: highest character-position match ratio (anchors snippets lightly rewritten by the LLM).
  // O(n×m), but m≤80 and chunk ≤~24k chars, acceptable on the interaction path.
  if (probe.length < 12 || stripped.length < probe.length) return -1;
  let bestAt = -1;
  let bestScore = 0;
  const limit = stripped.length - probe.length;
  const step = probe.length > 40 ? 2 : 1; // Coarse scan suffices to anchor long probes.
  for (let at = 0; at <= limit; at += step) {
    let same = 0;
    for (let j = 0; j < probe.length; j++) {
      if (stripped[at + j] === probe[j]) same++;
    }
    const score = same / probe.length;
    if (score > bestScore) {
      bestScore = score;
      bestAt = at;
    }
  }
  return bestScore >= APPROX_MIN_RATIO ? bestAt : -1;
}

/**
 * normalize → exact substring → approx-match sliding window (M2 level 3, tech design §7.5;
 * fulfills registered M1 decision 4) → null (caller shows paragraph-level fallback note).
 */
export function locateExcerpt(
  content: string,
  excerpt: string | undefined,
): { before: string; hit: string; after: string } | null {
  if (!excerpt) return null;
  const probe = excerpt.replace(/\s+/g, "").slice(0, 80);
  if (!probe) return null;
  // Build an index of the content with whitespace stripped → map back to raw offsets.
  const rawToStripped: number[] = [];
  let stripped = "";
  for (let i = 0; i < content.length; i++) {
    const ch = content[i];
    if (!/\s/.test(ch)) {
      rawToStripped.push(i);
      stripped += ch;
    }
  }
  let at = stripped.indexOf(probe);
  if (at === -1) at = approxFindStripped(stripped, probe);
  if (at === -1) return null;
  const start = rawToStripped[at];
  const end = rawToStripped[Math.min(at + probe.length - 1, rawToStripped.length - 1)] + 1;
  const ctx = 90;
  return {
    before: (start > ctx ? "…" : "") + content.slice(Math.max(0, start - ctx), start),
    hit: content.slice(start, end),
    after: content.slice(end, Math.min(content.length, end + ctx)) + (end + ctx < content.length ? "…" : ""),
  };
}

export interface CitePopoverProps {
  anchor: DOMRect;
  fileId: string;
  fileName: string;
  cell: MatrixCellVM;
}

export function CitePopover({ anchor, fileId, fileName, cell }: CitePopoverProps) {
  const t = useTranslations("kbMatrix");
  const chunk = useKbChunkContent(fileId, cell.chunkId, undefined, { single: true });
  const located = chunk.status === "ready" ? locateExcerpt(chunk.content, cell.sourceExcerpt) : null;

  const left = Math.max(8, Math.min(anchor.right - 288, (typeof window !== "undefined" ? window.innerWidth : 1440) - 296));
  const top = anchor.bottom + 6;

  return (
    <div className="mx-citepop" style={{ left, top }} data-testid="kb-matrix-citepop">
      <div className="frag">
        {located ? (
          <>
            {located.before}
            <span className="mx-hl">{located.hit}</span>
            {located.after}
          </>
        ) : chunk.status !== "error" && cell.sourceExcerpt ? (
          <span className="mx-hl">{cell.sourceExcerpt}</span>
        ) : (
          <span>{t("cite_unlocated")}</span>
        )}
      </div>
      <div className="src">
        {fileName}
        {cell.page != null ? ` · ${t("page_n", { n: cell.page })}` : ""}
      </div>
    </div>
  );
}
