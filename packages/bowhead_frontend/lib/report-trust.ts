/**
 * report-trust — legacy-section statistics for SourceLinkageStrip.
 *
 * **Single semantics** after citation-audit M1-4 (gen2 marker retirement):
 *   verified   = valid [N] citations (N ≤ sources entries; [N] references sources[N-1])
 *   unverified = narrative.unverified_numbers count (deterministic backend structured field)
 *   percent    = verified / (verified + unverified); no claims → null (strip hidden)
 *
 * Old gen2 markers (`[num:V:U:src:S]` check / `[num:V:U]` amber) no longer count — emitters stopped
 * writing; M2 migrated existing markers by baking plain values. Unmigrated historical reports use pct=null (strip shows
 * source counts only, no misleading 0%). BE `report_trust.py` shares semantics (except kill-switch legacy
 * mode).
 *
 * Scan: recursively collect all string values in section objects and apply regex individually — never scan entire
 * JSON.stringify output: serialized array literals (e.g. {"a":[5]}) falsely match
 * [N] regex.
 */

import type { ReportSection } from "@/lib/types";

const NUMBERED_RE = /\[(\d{1,3})\]/g;

export interface ReportTrustStats {
  verified: number;
  unverified: number;
  sources: number;
  /** 0-100 integer; null when no claims at all (strip hides). */
  verifiedPct: number | null;
}

/** Recursively collect every string value in objects/arrays. */
function collectStrings(value: unknown, out: string[]): void {
  if (typeof value === "string") {
    out.push(value);
    return;
  }
  if (Array.isArray(value)) {
    for (const v of value) collectStrings(v, out);
    return;
  }
  if (value && typeof value === "object") {
    for (const v of Object.values(value as Record<string, unknown>)) {
      collectStrings(v, out);
    }
  }
}

/** Reasonable upper bound for `unverified_total` — beyond it field is corrupted; use list length.
 *  Same as backend `report_trust._UNVERIFIED_TOTAL_SANE_MAX`. */
const UNVERIFIED_TOTAL_SANE_MAX = 1_000_000;

/** Unverified-number count in this section (trust-strip denominator). Same semantics as backend `unverified_count`. */
export function unverifiedCount(section: Record<string, unknown>): number {
  const list = section.unverified_numbers;
  if (!Array.isArray(list) || list.length === 0) return 0;
  const total = section.unverified_total;
  if (typeof total !== "number" || !Number.isInteger(total)) return list.length;
  if (total < list.length || total > UNVERIFIED_TOTAL_SANE_MAX) return list.length;
  return total;
}

export function computeReportTrust(sections: ReportSection[]): ReportTrustStats {
  let verified = 0;
  let unverified = 0;
  let sources = 0;
  const strings: string[] = [];
  const numberedCandidates: number[] = [];

  const walk = (list: ReportSection[]) => {
    for (const s of list) {
      if (s.kind === "sources") {
        sources = s.entries?.length ?? sources;
        continue;
      }
      if (s.kind === "section") {
        if (Array.isArray(s.body)) walk(s.body);
        continue;
      }
      collectStrings(s, strings);
      if ((s.kind === "narrative" || s.kind === "insight_card") && Array.isArray(s.unverified_numbers)) {
        // insight_card: impl review SEC-P1 — composer now deterministically computes this field for card text.
        // ⚠ Every kind added here must mount UnverifiedNumbersNote in its rendering path, otherwise
        // strip counts it without a body warning, and `scrollToFirstUnverified` cannot navigate.
        // Denominator uses `unverified_total` (actual count), not list length: backend truncates list to
        // 10/20 **only to render fewer entries**; denominator based on list would count 30 unverified as 10 ⇒ percentage falsely
        // increases trust. A metric that lies optimistically is worse than no metric.
        //
        // ⚠ Match backend `report_trust.unverified_count` **rule for rule** (re-review comparison found two discrepancies):
        //   · Empty list always counts 0 regardless of total — `patch_section` shallow merge can leave stale total,
        //     yielding no corresponding display content while percentage collapses (100% → 3%);
        //   · total must be integral, at least list length, and within upper bound (1e21 can be constructed);
        //     any failure falls back to list length — fallback only toward less trust.
        // Keep both sides synchronized on changes; each has tests using same fixtures.
        unverified += unverifiedCount(s as Record<string, unknown>);
      }
    }
  };
  walk(sections);

  for (const text of strings) {
    NUMBERED_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = NUMBERED_RE.exec(text)) !== null) {
      numberedCandidates.push(parseInt(m[1], 10));
    }
  }
  // [N] validity: N ≤ sources entries ([N] without backing entry is ordinary text,
  // citation-plugin does not decorate it either)
  verified += numberedCandidates.filter((n) => n >= 1 && n <= sources).length;

  const total = verified + unverified;
  // impl review C-P2-2: no percentage if verified==0 — unmigrated content (gen2 ✓ no longer counted,
  // old unverified fields retained) would show misleading 0%; still display unverified counts (hide % only).
  const pctNull = total === 0 || verified === 0;
  return {
    verified,
    unverified,
    sources,
    verifiedPct: pctNull ? null : Math.round((verified / total) * 100),
  };
}

/** Click "N 个数字待核对" → scroll to first unverified number in body. */
export function scrollToFirstUnverified(root: HTMLElement | Document = document): boolean {
  // citation-audit M3: old selector first half `.citation-num--no-src` represented gen2 sourceless-marker
  // amber-state class; sole emitter (citation-plugin NUM_OLD_RE pass) removed in this batch → never
  // matches. `.v2-narrative-unverified` retains live emitters (canvas/sections/narrative.tsx,
  // extensions/text-based.tsx); functionality unaffected.
  const el = root.querySelector<HTMLElement>(".canvas-report-article .v2-narrative-unverified");
  if (!el) return false;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  return true;
}
