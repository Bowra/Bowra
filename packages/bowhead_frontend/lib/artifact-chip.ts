/**
 * chat-redesign p0-followup F4 — `[data:art_*]` artifact reference detector.
 *
 * Dogfood Q1 (2026-05-23) surfaced answers like
 *   "总盘子 [data:art_0fa494fbf141] ¥123.4 亿"
 * — raw token leaking to the user. PRD §4.5.2 calls for an inline chip
 * representation; this util splits a paragraph into text + token runs
 * that LeadNarrative renders as `<span class="bh-artifact-chip">`.
 *
 * The regex is tight: `[data:art_` + hex tail + `]`. It deliberately does
 * NOT match generic `[N]` citation chips (N15 dropped chat citation chips,
 * and a number-only payload doesn't start with `art_`).
 */

/** Compiled regex — match once per scan; capture artifact id (`art_xxxx`). */
export const ARTIFACT_TOKEN_RE = /\[data:(art_[a-f0-9]+)\]/gi;

/** prod-bug-bash-039 #6 (0.39.2): NEW citation marker `[cite:N]` (1-based seq).
 *  Backend `_resolve_citations()` rewrites `[data:art_xxx]` → `[cite:N]` via
 *  turn-scoped registry. FE chip parser matches BOTH formats so the visual
 *  stays consistent (legacy archived turns still have `[data:art_xxx]`).
 *  Full state plumbing (citations[] registry lookup) deferred to 0.39.3 —
 *  for 0.39.2, `[cite:N]` chip renders the same `[数据]` pill but click
 *  falls through to existing toast-dedup safety net (no DOM target match). */
export const CITE_TOKEN_RE = /\[cite:(\d+)\]/g;

export interface ArtifactTokenMatch {
  /** Matched id with the `art_` prefix preserved.
   *  For `[cite:N]` matches, this is `"cite:N"` (string form) — used as a
   *  pseudo-id; future 0.39.3 wiring will resolve via citations[N-1]. */
  artifactId: string;
  /** Whether this match is the new `[cite:N]` format (vs legacy `[data:art_xxx]`).
   *  ArtifactChip uses this to know which scroll path to take when 0.39.3
   *  wires the citations[] registry lookup. */
  isCiteMarker?: boolean;
  /** Match start index (inclusive). */
  start: number;
  /** Match end index (exclusive). */
  end: number;
}

/** Scan `text` and return every artifact reference match (both `[data:art_xxx]`
 *  legacy + `[cite:N]` new format), in source order. */
export function findArtifactTokens(text: string): ArtifactTokenMatch[] {
  const out: ArtifactTokenMatch[] = [];
  if (!text) return out;
  // Reset lastIndex so consecutive calls don't desync.
  const reData = new RegExp(ARTIFACT_TOKEN_RE.source, ARTIFACT_TOKEN_RE.flags);
  let m: RegExpExecArray | null;
  while ((m = reData.exec(text)) !== null) {
    out.push({
      artifactId: m[1],
      start: m.index,
      end: reData.lastIndex,
    });
  }
  // prod-bug-bash-039: also match new `[cite:N]` format.
  const reCite = new RegExp(CITE_TOKEN_RE.source, CITE_TOKEN_RE.flags);
  while ((m = reCite.exec(text)) !== null) {
    out.push({
      artifactId: `cite:${m[1]}`,  // pseudo-id; 0.39.3 wires citations[N-1] lookup
      isCiteMarker: true,
      start: m.index,
      end: reCite.lastIndex,
    });
  }
  // Sort by start position so render order matches source order regardless
  // of which regex matched first.
  out.sort((a, b) => a.start - b.start);
  return out;
}

/** Quote artifact ids for use in CSS attribute selectors — fall back to a
 *  manual escape when CSS.escape is unavailable (jsdom older builds). */
function _cssEscape(value: string): string {
  if (
    typeof CSS !== "undefined" &&
    typeof (CSS as { escape?: (s: string) => string }).escape === "function"
  ) {
    return (CSS as { escape: (s: string) => string }).escape(value);
  }
  // art_xxx ids are hex-tail only; restrict to safe charset to avoid the
  // need for a real escape table.
  return value.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

/**
 * Best-effort scroll-to-artifact behavior used by the chip onClick. Looks
 * for a DOM node tagged with `data-artifact-id` or `data-section-id`
 * matching the artifact id; calls scrollIntoView on the first match.
 *
 * No-op when no match is found — UI doesn't error, the chip just behaves
 * as a hover-only affordance in that case.
 */
export function scrollToArtifact(artifactId: string): boolean {
  if (typeof document === "undefined") return false;
  const id = _cssEscape(artifactId);
  const candidates = [
    `[data-artifact-id="${id}"]`,
    `[data-section-id="${id}"]`,
    `[id="${id}"]`,
  ];
  for (const sel of candidates) {
    const el = document.querySelector(sel) as HTMLElement | null;
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      return true;
    }
  }
  return false;
}
