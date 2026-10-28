/**
 * Batch 1 2.1b (chart-table-ux) · generalized editorial language — presentation resolver.
 *
 * Contract = design D frame D5 (chart kind × protagonist × label-policy table), implemented row by row:
 *   - Protagonist (lead): element addressed by caption conclusion — bar / series / slice / cluster.
 *   - Fallback: caption matches category value/series name (longest match wins) → largest `sort_by` metric
 *     element (only when spec explicitly has sort_by — backend-declared sorting intent) → **null =
 *     all full-color (current behavior, fail-open)**. Frontend derivation only; no persistence or ChartSpec changes.
 *   - pie exception: choose largest slice without caption match (D5 largest slice or caption match — share-chart protagonist
 *     is structural); waterfall protagonist = endpoint total (structural, caption-independent).
 *   - heatmap / sankey have no lead (color scales/flow widths supply emphasis).
 *
 * One deviation from tech-design §2.1b interface: no `axisTitles` here — axis titles already implemented in
 * build-config builders (2.3); producing them again creates two sources of truth.
 */

import { roleOrderedDims } from "./stack-dims";
import { parseHexColor } from "./color";
import type { ChartSpec } from "./types";

export interface Presentation {
  lead:
    | { kind: "element"; index: number }
    | { kind: "dataset"; index: number }
    | null;
}

// review R2: formerly had `labelPolicy` (all-lead-big /
// endpoints-extrema / percent-center / none). In build-config it **appeared only as a default
// parameter, never read** — lineLabelIndexes independently labels line endpoints/extrema,
// afterDraw plugin independently draws pie center numbers. Its five unit tests checked a field flowing
// nowhere, incapable of catching user-visible regressions. Remove it; do not claim a nonexistent contract.

/** No hardcoded muted colors: interpolate 70% toward surface (retain 30% original color), naturally supporting dark themes.
 *
 * ★ Results reach 1.4–1.7:1, **below palette release gate 3:1**
 * (`chart_style_materializer._palette_validation`). Deliberate decision on 2026-09-10:
 * gate governs palette itself (every series readable without protagonist); muted supporting series provide **decorative context**,
 * with protagonist, axes, and numeric labels carrying interpretation. See `chart-style-customization-2026-09-06/prd.md` §8.2.
 *
 * **Do not casually add a 3:1 floor here**: fading capacity differs sharply by color (Rich Purple 44%, Rich Green 13%,
 * darkened Cyan only 1%), so a floor randomizes hierarchy by color. If supporting series must become readable,
 * use constant-luminance desaturation (unchanged contrast, hierarchy through chroma), rather than a floor.
 * Quantified comparison: `docs/sprints/brand-appearance-repair-2026-09-09/spike.md` §4.
 */
export function mutedOf(color: string, surface: string): string {
  const c = parseHex(color);
  const s = parseHex(surface);
  if (!c || !s) return color;
  const t = 0.7;
  const mixed = c.map((cv, i) => Math.round(cv + (s[i] - cv) * t));
  return `#${mixed.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/** R7: use shared parser. Old implementation accepted only six-digit hex; production build compressed `--c-surface: #ffffff`
 *  to `#fff` ⇒ `parseHex` returned null ⇒ `mutedOf` returned unchanged ⇒ **protagonist-layer 30/70
 *  fading completely ineffective in production** (dev does not compress, so never visible locally). */
function parseHex(color: string): number[] | null {
  return parseHexColor(color);
}

/** Caption names protagonist — **earliest mention wins, longest at same position** ("华东" vs "华东大区"). */
/** Single-character categories do not match: review showed "减" in "整体略有下降，环比减弱" matched
 *  category `减`, stealing lead and bypassing sort_by fallback. Require at least two characters. */
const MIN_MATCH_LENGTH = 2;

/**
 * Captions **enumerating** most candidates emphasize no single one — no protagonist.
 *
 * 116 corpus measurements (631 charts with subtitles, 87 selected leads): **17 captions are merely lists**,
 * where longest-name rule randomly bolds one and mutes all others:
 *   · `pie 2026年品牌营业额 Top 3 占比` "金辉福 60.2% | 六福珠宝 21.8% | 阿迪达斯 18.0%"
 *     → highlights `六福珠宝` (middle slice) merely because its name has 4 characters;
 *   · `dual_axis 2026年品牌客单价 Top 20` "客单价 = 营业额 / 客流量" — subtitle is a **formula**,
 *     yet selects `营业额` as protagonist;
 *   · Real device on 116 (2026-09-05, 10-series category trend): subtitle lists all ten categories,
 *     bolding `护肤美妆用品` and muting other nine, while body says "女装一骑绝尘".
 *
 * Criterion: ≥2 mention positions **and** more than half of all candidates. After canceling lead, each branch has a suitable fallback
 * (bar/hbar to `sort_by` maximum, pie to largest slice, line family to all full-color).
 *
 * ⚠ Count **distinct mention positions**, not matched candidates. Existing case
 * "华东大区领跑全国" includes both `华东` and `华东大区`, but both identify
 * one mention (start position 0); candidate counts misclassify as enumeration and fail that case.
 */
function isEnumeration(text: string, candidates: Array<string | null | undefined>): boolean {
  const uniq = [...new Set(candidates.map((c) => (c ?? "").trim()))].filter((c) => c.length >= MIN_MATCH_LENGTH);
  if (uniq.length < 2) return false;
  const mentions = new Set(uniq.map((c) => text.indexOf(c)).filter((i) => i >= 0));
  return mentions.size >= 2 && mentions.size * 2 > uniq.length;
}

function matchLongest(text: string, candidates: Array<string | null | undefined>): number | null {
  if (isEnumeration(text, candidates)) return null;
  let best = -1;
  let bestPos = Number.POSITIVE_INFINITY;
  let bestLen = 0;
  candidates.forEach((raw, i) => {
    const name = (raw ?? "").trim();
    if (name.length < MIN_MATCH_LENGTH) return;
    const pos = text.indexOf(name);
    if (pos < 0) return;
    // Earliest mention wins; choose longest when starting at same position (`华东` vs `华东大区`).
    if (pos < bestPos || (pos === bestPos && name.length > bestLen)) {
      best = i;
      bestPos = pos;
      bestLen = name.length;
    }
  });
  return best >= 0 ? best : null;
}

function argmax(values: Array<number | null | undefined>): number | null {
  let best = -1;
  for (let i = 0; i < values.length; i += 1) {
    const v = values[i];
    if (typeof v !== "number" || Number.isNaN(v)) continue;
    if (best < 0 || v > (values[best] as number)) best = i;
  }
  return best >= 0 ? best : null;
}

export function resolvePresentation(spec: ChartSpec, captionText: string | null): Presentation {
  const text = (captionText ?? spec.subtitle ?? "").trim();
  const rows = spec.rows ?? [];
  const cols = spec.columns ?? [];
  const catCol = cols.find((c) => c.type === "categorical" || c.type === "geo" || c.type === "date");
  const numCols = cols.filter((c) => c.type === "numeric");
  const catValues = catCol ? rows.map((r) => String(r[catCol.key] ?? "")) : [];

  switch (spec.kind) {
    case "bar":
    case "hbar": {
      // D5 bar/hbar row: protagonist = caption-matched bar; otherwise sort_by maximum
      const hit = text ? matchLongest(text, catValues) : null;
      if (hit !== null) return { lead: { kind: "element", index: hit } };
      if (spec.sort_by) {
        const metric = numCols.find((c) => c.key === spec.sort_by) ?? numCols[0];
        const idx = metric ? argmax(rows.map((r) => r[metric.key] as number | null)) : null;
        if (idx !== null) return { lead: { kind: "element", index: idx } };
      }
      return { lead: null };
    }
    case "stacked_bar": {
      // D5 stacked_bar row: protagonist = caption-matched stacked segment (= series).
      // Two series-name sources: pivot (second category-column values) or multiple numeric-column headers.
      const catCols = roleOrderedDims(cols.filter((c) => c.type === "categorical" || c.type === "geo"));
      const seriesNames = catCols.length >= 2
        ? [...new Set(rows.map((r) => String(r[catCols[1].key] ?? "")))]
        : numCols.map((c) => c.header);
      const hit = text ? matchLongest(text, seriesNames) : null;
      return hit !== null
        ? { lead: { kind: "dataset", index: hit } }
        : { lead: null };
    }
    case "line":
    case "area_stacked": {
      // D5 line/area row: protagonist = caption-matched series; labels always endpoints + extrema only
      const hit = text ? matchLongest(text, numCols.map((c) => c.header)) : null;
      return {
        lead: hit !== null ? { kind: "dataset", index: hit } : null };
    }
    case "dual_axis":
    case "combo": {
      // D5 dual_axis/combo row: caption-matched bar-side column (dataset order = bars first)
      const hit = text ? matchLongest(text, numCols.map((c) => c.header)) : null;
      return hit !== null
        ? { lead: { kind: "dataset", index: hit } }
        : { lead: null };
    }
    case "pie": {
      // D5 pie row: largest slice (or caption match); central main number
      const hit = text ? matchLongest(text, catValues) : null;
      const numCol = numCols[0];
      const maxIdx = numCol ? argmax(rows.map((r) => r[numCol.key] as number | null)) : null;
      const idx = hit ?? maxIdx;
      return idx !== null
        ? { lead: { kind: "element", index: idx } }
        : { lead: null };
    }
    case "scatter": {
      // D5 scatter row: protagonist = caption-matched group (large solid points + group name), others hollow
      // Same semantics as `buildScatter` / `normalize-chart-for-rendering`: accept geo when categorical absent.
      // Missing this prevents geo-only scatter (province column only) from choosing lead even when named in caption.
      const groupCol = cols.find((c) => c.type === "categorical")
        ?? cols.find((c) => c.type === "geo");
      const groups = groupCol ? [...new Set(rows.map((r) => String(r[groupCol.key] ?? "")))] : [];
      const hit = text ? matchLongest(text, groups) : null;
      return hit !== null
        ? { lead: { kind: "dataset", index: hit } }
        : { lead: null };
    }
    case "waterfall":
      // D5 waterfall row: protagonist = endpoint total (structural; Σ always final element)
      return { lead: { kind: "element", index: rows.length } };
    default:
      // heatmap / sankey / kpi / table: no lead concept
      return { lead: null };
  }
}
