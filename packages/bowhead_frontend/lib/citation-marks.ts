/**
 * citation-marks — single frontend citation-marker module (citation-audit M3), matching backend
 * `bowhead_core/citation_marks.py`.
 *
 * gen3 value [N] is the sole live format. This module has **one** responsibility:
 *
 *   `stripLegacy(text)` — permanent fallback seam baking every retired marker syntax into plain text
 *   (`[num:…]` → value+unit, **units must survive**; `[ref:]`/`[chart:]`/`[fact:]`/
 *   `[bi:]`/`[memory:]` → remove).
 *
 * Why retain after emitter removal (M3-5): M2 cleaned existing database content (all six production marker families
 * asserted zero), but **historical SSE replay** still emits marked old text (22 `lead.final.payload`
 * events + 74 `canvas.section_done` events), and image-revert / imported external content can
 * reintroduce it. Rendering must never expose raw markers.
 *
 * ⚠ **Semantics must exactly match backend `citation_marks.strip_legacy`** — both sides' golden tests
 * interlock via `__fixtures__/citation-parity.json` (mode="bake"). Changing either side's
 * output shape fails both test suites.
 *
 * History (debt 5 frontend, M3 consolidation): replaces three implementations with different semantics —
 * `md.ts::stripInlineNumMarkers` (`_formatNumDisplay` conventions),
 * `format-num.ts::stripMarkersForPlainText` (`formatNumValue` conventions: CNY scaled to
 * `¥1.55亿`), and `format-inline-tokens.ts::formatInlineTokens` (chart formatter
 * conventions). Same `[num:155031947.26:CNY]` produced three outputs, all differing from backend bake
 * (`155031947.26元`, actual M2 persisted shape). Now one convention.
 */

// ── Retired syntax (strip/bake only, never emit) — exact counterparts of BE regexes ────────────────
// BE: sources_resolver._NUM_STANDARD_RE
const NUM_STANDARD_RE = /\[num:(-?\d+(?:\.\d+)?(?:e-?\d+)?):([^:\]#]*):src:([^\]#]+)(?:#highlight=([^\]]+))?\]/g;
// BE: sources_resolver._NUM_SMUSHED_RE (value and unit joined: `[num:11.91亿:src:S]`)
const NUM_SMUSHED_RE = /\[num:([^:\]#]+):src:([^\]#]+)(?:#highlight=([^\]]+))?\]/g;
// BE: citation_marks._NUM_PLAIN_RE
const NUM_PLAIN_RE = /\[num:([^:[\]]+?)(?::([^:[\]]*))?\]/g;
// BE: citation_marks._NUM_CATCHALL_RE (malformed-residue fallback, after passes 1–3)
// ⚠ Character class must exclude `[` — same BE ReDoS (see BE comment: multiple `[num:` starts → O(L²)
// backtracking). BE gateway event-loop variant is P0; same frontend pattern burns user browser main thread
// (long replay/import text → frozen page). Keep both synchronized; dense parity corpus locks equivalent output.
const NUM_CATCHALL_RE = /\[num:([^[\]]*)\]/g;
const REF_STRIP_RE = /\s?\[ref:[^\]]+\]/g;
const CHART_RE = /\s?\[chart:[^\]]+\]/g;
const FACT_RE = /\s?\[(?:fact|bi|memory):[^\]]+\]/g;

// BE: citation_marks._ASCII_UNIT_MAP (i18n-v1 enum units + µ-era ratio)
const ASCII_UNIT_MAP: Record<string, string> = {
  percent: "%",
  ratio: "倍",
  rate: "倍",
  CNY: "元",
  USD: "美元",
  EUR: "欧元",
  JPY: "日元",
  // Completed 2026-08-25, synchronized with BE (test_units_contract.py compares each currency)
  GBP: "英镑",
  HKD: "港币",
  TWD: "新台币",
  KRW: "韩元",
  SGD: "新加坡元",
  count: "",
  duration_days: "天",
  duration_hours: "小时",
  "%": "%",
  "‰": "‰",
};

// BE: citation_marks._CURRENCY_SCALE_UNITS — raw numeric value + currency-code combination,
// unscaled values + direct suffix bake unreadable outputs such as `7016893124元` (M3-1 production
// regression). Distinguish units such as `亿元`/`万元`/`%` that **already represent display shape**.
const CURRENCY_SCALE_UNITS = new Set(["CNY", "USD", "EUR", "JPY", "GBP", "HKD", "TWD", "KRW", "SGD"]);

const ASCII_RE = /^[\x00-\x7F]*$/;

/** Raw numeric value + currency code → scaled Chinese display, matching BE `_format_currency` exactly.
 *
 * BE uses `Decimal(float).quantize(ROUND_HALF_UP)` to replicate this function's `toFixed`
 * (Python f-string `.Nf` rounds half-**even**, unlike JS half-**away** at exact
 * binary ties — impl review confirmed `12500元` → BE 1.2万 / FE 1.3万).
 * Changing rounding here requires synchronized BE `_js_to_fixed` changes; dense parity tie sweep fails otherwise.
 */
function formatCurrency(value: string, unit: string): string {
  const suffix = ASCII_UNIT_MAP[unit];
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value}${suffix}`;
  const a = Math.abs(n);
  if (a >= 1_0000_0000) return `${(n / 1_0000_0000).toFixed(2)}亿${suffix}`;
  if (a >= 1_0000) return `${(n / 1_0000).toFixed(1)}万${suffix}`;
  if (Number.isInteger(n)) return `${withThousands(String(n))}${suffix}`;
  // ⚠ Small values also require toFixed then thousands separators, **never toLocaleString**: the two JS
  // APIs differ in rounding — toLocaleString follows decimal literal (1.005→1.01), while toFixed
  // follows exact binary float (1.005 is actually 1.00499…→1.00). BE Decimal(float) uses
  // binary values, matching only toFixed (dense comparison confirmed differences for 1.005/2.675
  // with toLocaleString).
  return `${withThousands(n.toFixed(2))}${suffix}`;
}

/** `"1234.50"` → `"1,234.50"` (thousands separators in integer part; counterpart of BE `f"{…:,}"`). */
function withThousands(s: string): string {
  const [intPart, frac] = s.split(".");
  const sign = intPart.startsWith("-") ? "-" : "";
  const digits = sign ? intPart.slice(1) : intPart;
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return frac === undefined ? `${sign}${grouped}` : `${sign}${grouped}.${frac}`;
}

/** Equivalent of Python `f"{v:g}"` (default 6 significant digits + trailing-zero removal).
 *  Only for scaling percent fractions; extreme scientific notation (|v|<1e-4) may differ between sides —
 *  outside fallback's realistic range; shared fixtures cover real shapes. */
function formatG(n: number): string {
  const s = String(parseFloat(n.toPrecision(6)));
  // Python `%g` follows C: exponent **padded to two digits** (`1e-07`), unlike JS (`1e-7`). BE persists,
  // FE only renders fallback remnants, so FE matches BE (dense comparison found `[num:1e-9:percent]` divergence).
  return s.replace(/e([+-])(\d)$/, "e$10$2");
}

/**
 * Render retired marker (value, unit) as plain text, **always retaining unit**.
 * Counterpart of BE `citation_marks.format_marker_value` (including percent-fraction ×100 semantics).
 */
/** `[cite:N]` — **chat-path** citation marker (Lead final_answer), occasionally leaking into canvas
 *  narrative. Canvas/exports lack cite resolver (references chat turn-scoped registry,
 *  dangling there), so rendering is noise ⇒ omit from all display surfaces.
 *
 *  ⚠ Match only `[cite:N]`, **never touch `[N]`** — canonical citation numbers must display normally.
 *  ⚠ Cross-language duplicate of backend `bowhead_core/citation_marks.py` `CITE_MARKER_RE`,
 *  compared literally from this file by `test_cite_marker_parity.py`.
 *  ⚠ `[0-9]` deliberately narrows scope, not `\d`: Python `\d` matches fullwidth ０-９, JS only ASCII
 *  — this difference split 648/986 FIGURE_CAPTION_PREFIX_RE cases before.
 *
 *  This module is frontend citation-marker **sole knowledge source** (see header's three replaced implementations):
 *  `[cite:N]` also had three variants (citation-plugin `\d{1,3}` / format-inline-tokens `\d+` /
 *  md.ts chip regex); first two consolidated here. */
export const CITE_MARKER_RE = /\[cite:[0-9]{1,3}\]/g;

/** Strip `[cite:N]` from display/export text (leave `[N]` unchanged). */
export function stripCiteMarkers(text: string): string {
  return text.replace(CITE_MARKER_RE, "");
}

/** Provenance tags — machine markers for validation, **never user-visible**.
 *  Lead prompt explicitly requires each claim to end with source tag, so models keep producing them;
 *  unstripped markers such as `[llm-std:general-background]` must not leak into rendered chat copy.
 *
 *  ⚠ Cross-language duplicate of backend `bowhead_core/provenance.py` `PROVENANCE_RE`,
 *  compared literally by `test_cite_marker_parity.py`.
 *  ⚠ **Exclude `data:`** (2026-08-26 review correction): `[data:art_xxx]` is **live syntax** rendered by
 *  downstream `md.ts` remark plugin into artifact chips, not a provenance tag;
 *  stripping it deletes artifact references.
 *  ⚠ Explicit ASCII classes, not `\w`/`\s` (engines differ; precedent above in this file). */
export const PROVENANCE_TAG_RE =
  /[ \t]*[（(][ \t]*\[(?:bi|kb|user|fact|llm-std|llm-rsn):[^\]|:]*(?:\|(?:bi|kb|user|fact|llm-std|llm-rsn):[^\]|:]*)*\][ \t]*[)）]|[ \t]*\[(?:bi|kb|user|fact|llm-std|llm-rsn):[^\]|:]*(?:\|(?:bi|kb|user|fact|llm-std|llm-rsn):[^\]|:]*)*\]/g;

/** Strip provenance tags with leading spaces; remove parentheses too when containing only tags. */
export function stripProvenanceTags(text: string): string {
  return text.replace(PROVENANCE_TAG_RE, "");
}

export function formatMarkerValue(value: string, unit: string): string {
  const v = (value ?? "").trim();
  const u = (unit ?? "").trim();
  if (!u) return v;
  // Bake percent **fraction shape** (|v| ≤ 1, i18n-v1 raw-value contract) ×100; already scaled shape
  // (`30:percent`) unchanged. `%` symbol shape never scales (`[num:1:%]` means 1%).
  if (u === "percent") {
    const fv = Number(v);
    if (Number.isFinite(fv) && Math.abs(fv) <= 1) return `${formatG(fv * 100)}%`;
    return `${v}%`;
  }
  if (CURRENCY_SCALE_UNITS.has(u)) return formatCurrency(v, u);
  // ⚠ Require hasOwn rather than `ASCII_UNIT_MAP[u]`: prototype lookup makes `[num:5:constructor]` /
  // `[num:5:__proto__]` render garbage such as `5function Object() { [native code] }`,
  // diverging from BE `.get()` (no prototype lookup; impl review P2-2). Unit fields come from replay/
  // import/paste = untrusted channels.
  const mapped = Object.hasOwn(ASCII_UNIT_MAP, u) ? ASCII_UNIT_MAP[u] : undefined;
  if (mapped !== undefined) return `${v}${mapped}`;
  if (ASCII_RE.test(u)) return `${v} ${u}`;
  return `${v}${u}`;
}

/**
 * Permanent fallback: bake every retired marker into plain text.
 *
 * Ordered passes (`:src:` shapes first — plain syntax is their prefix):
 *   1. Standard src → `formatMarkerValue(V, U)` (retain unit)
 *   2. Smushed src → value text unchanged (value+unit already display-shaped)
 *   3. Plain → `formatMarkerValue(V, U)`
 *   4. Catch-all malformed residue → value segment (**never bare first segment**, reviving P0 unit loss in fallback)
 *   5–7. `[ref:]` / `[chart:]` / `[fact:|bi:|memory:]` → remove
 *
 * Idempotent; unmarked text unchanged.
 */
export function stripLegacy(text: string): string {
  if (!text) return text;
  let out = text;
  if (out.indexOf("[num:") !== -1) {
    out = out.replace(NUM_STANDARD_RE, (_m, v: string, u: string) => formatMarkerValue(v, u));
    out = out.replace(NUM_SMUSHED_RE, (_m, valueAndUnit: string) => (valueAndUnit ?? "").trim());
    out = out.replace(NUM_PLAIN_RE, (_m, v: string, u?: string) => formatMarkerValue(v, u ?? ""));
    out = out.replace(NUM_CATCHALL_RE, (_m, body: string) => {
      const segs = (body ?? "").split(":").filter((s) => s !== "");
      if (segs.length === 0) return "";
      // Pure src residue such as `[num::src:tl]` — no salvageable value; remove entirely.
      if (segs[0] === "src") return "";
      const value = segs[0];
      const unit = segs.length > 1 && !segs[1].startsWith("src") ? segs[1] : "";
      return formatMarkerValue(value, unit);
    });
  }
  out = out.replace(REF_STRIP_RE, "");
  out = out.replace(CHART_RE, "");
  out = out.replace(FACT_RE, "");
  return out;
}
