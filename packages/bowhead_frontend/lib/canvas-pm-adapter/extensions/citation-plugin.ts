/**
 * Citation Decoration Plugin — gen3 only (citation-audit M3-1).
 *
 * Two passes recognize only [N] (sources-section numbering), **no retired marker syntax is parsed**
 * (four parsing passes for [num:V:U(:src:S)] / [ref:S] / [chart:S] were removed with M3;
 * M1 stopped emission and M2 cleaned existing data, leaving no matches; any remaining DB markers are baked
 * into plain text by the permanent stripLegacy seam before rendering, so decorations need not understand legacy syntax):
 *   1. [N] → clickable chip (data-source-idx → canvas-context.sources[N-1])
 *   2. Numeric phrase adjacent to a valid [N] → source-linkage styling (citation-vnum)
 *
 * ⚠ Pass 2 only indicates a link between number and source, expressing neither truth, correctness nor successful verification.
 *
 * Plugin is view-only (does not contaminate doc.getJSON).
 */

import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { Extension } from "@tiptap/core";

import { CITE_MARKER_RE } from "@/lib/citation-marks";
import { getCurrentLocale, type Locale } from "@/lib/locale";
import { staticTranslator } from "@/lib/locale-messages";

// i18n: aria-labels are built at decoration time (ProseMirror plugin, not React →
// no useTranslations). Resolved via staticTranslator("citation_aria") against the
// current UI locale, like the rest of the canvas surface.
function ariaNumbered(n: number): string {
  return staticTranslator("citation_aria")("numbered", { n });
}
export function sourceLinkedAria(
  display: string,
  n: number,
  locale: Locale = getCurrentLocale(),
): string {
  return staticTranslator("citation_aria", locale)("source_linked", { display, n });
}

// Sprint agent-redesign-1 Phase F (2026-05-15) — numbered ref `[N]`.
// Resolves to canvas-context.sources[N-1]. Backend resolver rewrites legacy
// `[num:V:U:src:S]` / `[ref:CHUNK]` to this form.
// Constraint: at least 1 digit, no other chars inside brackets. Avoids matching
// markdown footnotes by allowing only N in [1..999]. False-positive risk on
// random `[5]` in user prose accepted — Phase F docs reserve `[N]` for citations.
const NUM_NUMBERED_RE = /\[(\d{1,3})\]/g;

/** `[cite:N]`: chat-path marker (Lead final_answer), occasionally leaking into canvas narrative.
 * Canvas has no cite resolver (it uses a chat turn-scoped registry, unbound in canvas),
 * so **do not render** it; hide it without deleting.
 *
 * Previously markdownToPMNodes stripped it directly from Markdown, the round-trip **read side**,
 * truncating the round-trip without degraded ⇒ user edits silently erased citation anchors
 * (observed on 116 dogfood, 2026-08-15). Hide via decorations instead: retain original text in doc and write-back,
 * just omit display, like figure-caption prefixes.
 *
 * Regex lives in lib/citation-marks (single frontend authority for citation markers, checked against backend
 * citation_marks.CITE_MARKER_RE by parity tests); the local copy used `\d`,
 * differing from backend `[0-9]` on fullwidth digits, the same pitfall as FIGURE_CAPTION_PREFIX_RE. */

/** Positions of all `[cite:N]` markers in text. Extracted as a pure function for testing:
 * decorations require a real Editor to run; the other matching logic here uses the same approach
 * (see findCitedNumberRanges). */
export function findCiteMarkerRanges(text: string): Array<{ from: number; to: number }> {
  const out: Array<{ from: number; to: number }> = [];
  if (!text || text.indexOf("[cite:") === -1) return out;
  CITE_MARKER_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = CITE_MARKER_RE.exec(text)) !== null) {
    out.push({ from: m.index, to: m.index + m[0].length });
  }
  return out;
}

export const CitationPluginKey = new PluginKey("citation");

/**
 * Source-linked number (citation-vnum) adjacency detection: numeric phrases beside valid [N].
 *
 * Persisted body numbers are plain text (gen3: `962.7 亿元 [1]`), so linkage relies on
 * adjacency detection. It means this number has a source beside it, **not** backend verification.
 * verified-edit-guard.ts:34 also imports this function (preventing users from altering cited numbers).
 *
 * Rules (conservative):
 *  - Numeric phrase starts with a digit: digits/thousands separators/decimals + optional unit suffix ≤4 characters (%, 亿元, pp,
 *    千万美元 etc.; impl review C-P1-4: binder can emit four-character suffixes, missed by window {1,3},
 *    leaving those numbers without edit-guard protection)
 *  - A numeric token requires a preceding boundary (line start/whitespace/CJK/punctuation, not [\w.]), or e-notation
 *    (`1.2e-4 % [1]`) falsely matches a garbage range from the exponent (confirmed C-P1-4)
 *  - Must be immediately followed by " [N]", with N ≤ numberedSources length (same [N] decoration guard)
 *  - Bare years (19xx/20xx ± "年") are unmarked: year citations support claims rather than verified numbers
 */
const CITED_NUM_RE = /(?<![\w.])([+\-−]?\d[\d,，.]*(?:e[+-]?\d+)?(?:\s?[^\s\[\]]{1,4})?)\s\[(\d{1,3})\]/g;
const BARE_YEAR_RE = /^[+\-−]?(19|20)\d{2}\s?\u5e74?$/; // \u5e74 =「年」

export function findCitedNumberRanges(
  text: string,
  numberedLen: number,
): Array<{ from: number; to: number; n: number }> {
  const out: Array<{ from: number; to: number; n: number }> = [];
  if (!text || numberedLen <= 0) return out;
  CITED_NUM_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = CITED_NUM_RE.exec(text)) !== null) {
    const n = parseInt(m[2], 10);
    if (n < 1 || n > numberedLen) continue;
    const tok = m[1];
    if (BARE_YEAR_RE.test(tok)) continue;
    out.push({ from: m.index, to: m.index + tok.length, n });
  }
  return out;
}

/** source-accessibility c2: `[cite:N]` resolution (label required, anchor only for chart_section). */
export interface ResolvedCite {
  label: string;
  anchorId?: string;
}

/** (sectionId, N) → resolved result; missing → null (decorations still hide it, truthful degradation = current behavior). */
export type CiteResolver = (sectionId: string | null, n: number) => ResolvedCite | null;

/** source-accessibility §1.6 consumer half c2: build resolver from sections + turnCitations sidecar.
 *
 * Pure function (same approach as other find* functions here: decorations need a real Editor,
 * while decidable parts are tested as pure functions). N in `[cite:N]` is **turn-scoped**: locate
 * turn_id via sectionId, then find seq==N among that turn rows.
 *
 * Only accept OWNED-family rows carrying safe_label; backend already stripped non-OWNED
 * labels/anchors per batch 1 ownership criteria (citation_presentation). Frontend repeats no permission check
 * and never displays any other fields. */
/** review F10: empty resolver is a module constant; resolver identity keys the plugin decoration
 * cache. Rebuilding sections in legacy reports (no sidecar =100% existing data) with a fresh
 * () => null would invalidate the cache painstakingly recovered in #9. */
export const NULL_CITE_RESOLVER: CiteResolver = () => null;

export function buildCiteResolver(
  sections: ReadonlyArray<{ id?: string; turn_id?: string }>,
  turnCitations: Readonly<Record<string, ReadonlyArray<{ seq: number; safe_label?: string; safe_anchor_id?: string }>>> | undefined,
): CiteResolver {
  if (!turnCitations || Object.keys(turnCitations).length === 0) return NULL_CITE_RESOLVER;
  const turnBySection = new Map<string, string>();
  const visibleIds = new Set<string>();
  for (const s of sections) {
    if (!s.id) continue;
    visibleIds.add(s.id);
    if (s.turn_id) turnBySection.set(s.id, s.turn_id);
  }
  const rowsByTurn = new Map<string, Map<number, ResolvedCite>>();
  for (const [turnId, rows] of Object.entries(turnCitations)) {
    const bySeq = new Map<number, ResolvedCite>();
    for (const row of rows ?? []) {
      if (typeof row?.seq !== "number" || typeof row.safe_label !== "string" || !row.safe_label) continue;
      const resolved: ResolvedCite = { label: row.safe_label };
      // review F1: retain anchors only within sections **actually rendered in this view**; hidden sections are
      // stripped from DTO server-side, superseded sections live in their own nested views rather than this layer,
      // or an inert citation badge appears (same chat-side P1 just fixed). Losing anchors must retain
      // labels: degrade to hover-only chips, truthful and still informative.
      if (typeof row.safe_anchor_id === "string" && row.safe_anchor_id && visibleIds.has(row.safe_anchor_id)) {
        resolved.anchorId = row.safe_anchor_id;
      }
      bySeq.set(row.seq, resolved);
    }
    if (bySeq.size) rowsByTurn.set(turnId, bySeq);
  }
  if (!rowsByTurn.size) return NULL_CITE_RESOLVER;
  return (sectionId, n) => {
    if (!sectionId) return null;
    const turnId = turnBySection.get(sectionId);
    if (!turnId) return null;
    return rowsByTurn.get(turnId)?.get(n) ?? null;
  };
}

function ariaCite(n: number, label: string): string {
  return staticTranslator("citation_aria")("cite_labeled", { n, label });
}

interface CitationExtOptions {
  /** ar1 P1-3 fix: callback to read current numberedSources.length so we don't
   * decorate `[N]` when there's no backing entry (would show empty popover). */
  getNumberedSourcesLength: () => number;
  /** c2: read current `[cite:N]` resolver (report-view memoizes from sections+turnCitations,
   * passed through a ref like getNumberedSourcesLength). Always null means hide all as before. */
  getCiteResolver: () => CiteResolver | null;
  /** Canvas batch 3: restricted / missing / unsupported [N] sources (zero-based index set). Matching chips carry
   * data-source-denied, styled outlined + struck through (same as chat badges). report-view memoizes from the same source table; identity change means data changed. */
  getNumberedSourcesDenied: () => ReadonlySet<number>;
}

const NO_DENIED: ReadonlySet<number> = new Set();

/** Canvas batch 3: restricted / missing / unsupported source-table indexes (same criterion as chat badge is-denied). */
export function deniedSourceIndexes(entries: ReadonlyArray<Record<string, unknown>> | undefined): ReadonlySet<number> {
  if (!entries?.length) return NO_DENIED;
  const out = new Set<number>();
  entries.forEach((entry, idx) => {
    const state = entry?.access_state;
    if (state === "linked_restricted" || state === "linked_missing" || state === "unsupported") out.add(idx);
  });
  return out.size ? out : NO_DENIED;
}

export const CitationExtension = Extension.create<CitationExtOptions>({
  name: "citation",

  addOptions() {
    return {
      getNumberedSourcesLength: () => 0,
      getCiteResolver: () => null,
      getNumberedSourcesDenied: () => NO_DENIED,
    };
  },

  addProseMirrorPlugins() {
    const getN = this.options.getNumberedSourcesLength;
    const getCiteResolver = this.options.getCiteResolver;
    const getDenied = this.options.getNumberedSourcesDenied;
    // #9 perf: cache the last DecorationSet; reuse it for selection-only transactions (doc unchanged +
    // numberedLen unchanged), avoiding full descendants traversal + six regex rescans on every keystroke/cursor move.
    // Recompute only on doc edits or source-count changes, a major cause of large-report editing lag.
    let cacheDoc: import("@tiptap/pm/model").Node | null = null;
    let cacheN = -1;
    let cacheResolver: CiteResolver | null | undefined;
    let cacheDenied: ReadonlySet<number> | undefined;
    let cacheSet = DecorationSet.empty;
    return [
      new Plugin({
        key: CitationPluginKey,
        props: {
          handleKeyDown(_view, event) {
            if (event.defaultPrevented) return false;
            if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") {
              return false;
            }
            if (typeof document === "undefined") return false;
            // M3-1: only kind=numbered remains ([N] chip + citation tick); gen2 kind=num
            // decorations were removed with the four parsing passes.
            // c2: anchored [cite:N] chips have equivalent keyboard behavior (Enter/Space → synthetic click,
            // routed to CitationHoverManager scrolling branch).
            const selector = "[data-citation-kind='numbered'], [data-citation-kind='cite'][data-cite-anchor]";
            const target =
              (event.target as HTMLElement | null)?.closest(selector) ??
              (document.activeElement as HTMLElement | null)?.closest(selector);
            if (!target) return false;
            event.preventDefault();
            target.dispatchEvent(
              new MouseEvent("click", {
                bubbles: true,
                cancelable: true,
                view: window,
              }),
            );
            return true;
          },
          decorations(state) {
            const numberedLen = getN();
            const citeResolver = getCiteResolver();
            const denied = getDenied();
            // #9: doc + numberedLen + resolver all unchanged → reuse previous result
            // (selection/click etc. do not rescan; resolver is memoized, so identity change means data changed)
            if (state.doc === cacheDoc && numberedLen === cacheN && citeResolver === cacheResolver && denied === cacheDenied) return cacheSet;
            const decos: Decoration[] = [];
            // c2: descendants traversal is preorder; text nodes are visited after their section ancestors;
            // all schema body text lives within section nodes with sectionId attrs.
            let currentSectionId: string | null = null;
            state.doc.descendants((node, pos) => {
              const sid = (node.attrs as { sectionId?: unknown } | undefined)?.sectionId;
              if (typeof sid === "string" && sid) currentSectionId = sid;
              if (!node.isText || !node.text) return;
              const text = node.text;

              // 0. [cite:N]: c2 (source-accessibility consumer half); resolution by
              // (containing section turn_id, N) from turnCitations sidecar → render a chip
              // (same hide-literal + ::before data-display mechanism as [N]; tooltip uses
              // bh-tip; section targets carry data-cite-anchor and click scrolls there, delegated to
              // CitationHoverManager). Unresolved → **hide as before** (truthful degradation = current behavior).
              // Hide-without-deleting round-trip constraint remains: neither decoration alters original text.
              for (const r of findCiteMarkerRanges(text)) {
                const n = parseInt(text.slice(r.from + 6, r.to - 1), 10);
                const resolved = Number.isFinite(n) && citeResolver ? citeResolver(currentSectionId, n) : null;
                if (resolved) {
                  // review F8: role/tabindex only for chips **with an activation path** (anchored
                  // click/keyboard scrolling); unanchored data_ref chips are hover-only information. Declaring them
                  // buttons with no response is an accessibility lie (Tab reaches them, Enter does nothing).
                  decos.push(
                    Decoration.inline(pos + r.from, pos + r.to, {
                      class: "citation-num citation-cite bh-tip",
                      "data-citation-kind": "cite",
                      "data-display": `[${n}]`,
                      "data-tip": resolved.label,
                      "aria-label": ariaCite(n, resolved.label),
                      ...(resolved.anchorId
                        ? { "data-cite-anchor": resolved.anchorId, role: "button", tabindex: "0" }
                        : {}),
                    }),
                  );
                } else {
                  decos.push(
                    Decoration.inline(pos + r.from, pos + r.to, {
                      class: "citation-cite-marker",
                    }),
                  );
                }
              }

              // 1. [N] — Phase F numbered ref (sources_resolver output).
              // Clickable chip; popover reads data-source-idx to look up canvas-context.sources[N-1].
              let m: RegExpExecArray | null;
              NUM_NUMBERED_RE.lastIndex = 0;
              while ((m = NUM_NUMBERED_RE.exec(text)) !== null) {
                const from = pos + m.index;
                const to = from + m[0].length;
                const n = parseInt(m[1], 10);
                if (n < 1 || n > 999) continue;
                // ar1 P1-3 fix: only decorate `[N]` when there's a backing
                // numberedSources entry. Else `[1]` markdown footnotes / Chinese
                // list notation `第[3]个` get spurious clickable chips with empty
                // popover. Falls back to plain text on legacy turns.
                if (n > numberedLen) continue;
                decos.push(
                  Decoration.inline(from, to, {
                    class: "citation-num citation-num--numbered",
                    "data-citation-kind": "numbered",
                    "data-source-idx": String(n - 1),
                    "data-display": `[${n}]`,
                    role: "button",
                    tabindex: "0",
                    "aria-label": ariaNumbered(n),
                    ...(denied.has(n - 1) ? { "data-source-denied": "" } : {}),
                  }),
                );
              }

              // 2. Numeric phrase adjacent to valid [N] → source-linkage styling (citation-vnum, literal text
              // stays visible) + same click/hover behavior as [N] (data-citation-kind=numbered
              // → SourcePanel / rc-prov popover). Product decision retained; see file header.
              for (const r of findCitedNumberRanges(text, numberedLen)) {
                decos.push(
                  Decoration.inline(pos + r.from, pos + r.to, {
                    class: "citation-vnum",
                    "data-citation-kind": "numbered",
                    "data-source-idx": String(r.n - 1),
                    "data-display": text.slice(r.from, r.to),
                    role: "button",
                    tabindex: "0",
                    "aria-label": sourceLinkedAria(text.slice(r.from, r.to), r.n),
                  }),
                );
              }
            });
            cacheSet = DecorationSet.create(state.doc, decos);
            cacheDoc = state.doc;
            cacheN = numberedLen;
            cacheResolver = citeResolver;
            cacheDenied = denied;
            return cacheSet;
          },
        },
      }),
    ];
  },
});
