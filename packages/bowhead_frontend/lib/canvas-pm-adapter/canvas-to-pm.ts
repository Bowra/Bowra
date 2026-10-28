/**
 * Forward adapter — ReportSection[] → PM doc tree (JSONContent).
 *
 * Data flow:
 *   sections (canvas-store) → canvasToPMDoc → JSONContent → editor.commands.setContent
 *
 * Handle three categories:
 *   - ATOMIC: { type: "section_<kind>", attrs: { sectionId, payload } }
 *     payload is the full section literal excluding id/kind
 *   - TEXT_BASED: { type: "section_<kind>", attrs: { sectionId, meta }, content: [...PM nodes from markdown] }
 *   - RECURSIVE: { type: "section_section", attrs: { sectionId, label, h2 }, content: [...recursive children] }
 */

import type { JSONContent } from "@tiptap/react";
import type { ReportSection } from "@/lib/types";
import {
  KIND_TO_NODE_NAME,
  isAtomicKind,
  isTextBasedKind,
  isRecursiveKind,
  type SectionKind,
} from "./schema";
import { stripLegacy } from "@/lib/citation-marks";
import { markdownToPMNodes } from "./markdown-pm";

const _EMPTY_DOC_NODE: JSONContent = {
  type: "section_narrative",
  attrs: { sectionId: "_empty", meta: {} },
  content: [{ type: "paragraph" }],
};

/**
 * report-engine Phase-1: collect the targetSectionIds of every block-level
 * `figure_ref` node anywhere in the tree (narrative/deck/callout children +
 * recursive sections). These figures render INLINE via the ref, so they're
 * skipped from top-level (below). Inline `[figure:..]` pills don't become
 * figure_ref nodes, so they don't land here → their figure stays top-level as
 * the scroll target.
 */
function collectFigureRefIds(nodes: JSONContent[]): Set<string> {
  const ids = new Set<string>();
  const walk = (n: JSONContent): void => {
    if (n.type === "figure_ref") {
      const tid = (n.attrs as { targetSectionId?: string } | undefined)?.targetSectionId;
      if (tid) ids.add(tid);
    }
    if (Array.isArray(n.content)) n.content.forEach(walk);
  };
  nodes.forEach(walk);
  return ids;
}

/**
 * Convert ReportSection[] to a PM doc.
 * Support empty arrays (return an empty paragraph to keep the doc valid).
 */
export function canvasToPMDoc(sections: ReportSection[]): JSONContent {
  if (!sections || sections.length === 0) {
    // Top-level doc content must be "section+"; an empty array is invalid.
    // Supply a narrative placeholder to keep the doc valid.
    return { type: "doc", content: [_EMPTY_DOC_NODE] };
  }
  const nodes = sections.map(sectionToPMNode);
  // report-engine Phase-1 weave: a figure referenced by a block `[figure:id]`
  // anchor renders inline via figure_ref → skip it at top-level so it isn't
  // ALSO rendered standalone (INV-1: this skips only the render projection; the
  // section stays canonical in the store and the edit round-trip leaves it
  // untouched). Unreferenced figures keep their top-level slot.
  const referenced = collectFigureRefIds(nodes);
  const content =
    referenced.size === 0
      ? nodes
      : nodes.filter((n) => {
          const sid = (n.attrs as { sectionId?: string } | undefined)?.sectionId;
          return !(sid && referenced.has(sid));
        });
  return { type: "doc", content: content.length ? content : [_EMPTY_DOC_NODE] };
}

/**
 * Single section → PM node JSON.
 */
/**
 * Normalize historical read-side aliases: currently only `sql_block` → `code_block` (2026-09-05).
 *
 * Semantically sql_block is a code_block with `lang="sql"`; Sprint ι comments already described a
 * legacy alias and 90-day grace (PRD §4.A.2), but **the read-side alias was never implemented**, sustaining
 * an entire parallel kind. Normalize here rather than falling into unknown: old content is already code,
 * so rendering it as a code block is **more accurate**, rather than degrading to "此段无法显示".
 *
 * Backend has a symmetric version (`canvas_store._normalize_legacy_kind`); this covers paths
 * bypassing backend projection (SSE replay, old locally cached sections).
 */
function normalizeLegacyKind(section: ReportSection): ReportSection {
  if (section.kind !== ("sql_block" as unknown as ReportSection["kind"])) return section;
  const legacy = section as unknown as {
    id: string;
    sql?: string;
    meta?: Record<string, string>;
  };
  return {
    id: legacy.id,
    kind: "code_block",
    call_id: legacy.meta?.call_id || `legacy_sql_${legacy.id}`,
    lang: "sql",
    code: legacy.sql || "",
    ...(legacy.meta ? { meta: legacy.meta } : {}),
  } as unknown as ReportSection;
}

export function sectionToPMNode(input: ReportSection): JSONContent {
  const section = normalizeLegacyKind(input);
  const kind = section.kind as SectionKind;

  if (isAtomicKind(kind)) {
    return atomicToPMNode(section);
  }
  if (isTextBasedKind(kind)) {
    return textBasedToPMNode(section);
  }
  if (isRecursiveKind(kind)) {
    return recursiveToPMNode(section);
  }
  // Sprint ι r2 P1 #4 hardening fallback: an unknown kind must not throw Tiptap
  // `RangeError: Unknown node type` (frontend older than DB after emergency image rollback).
  //
  // ⚠ Target changed from sql_block to dedicated unknown on 2026-09-05 for two reasons:
  //   ① The old path disguised **any** unrecognized content as SQL, implying actual code in the report;
  //   ② An ordinary content kind became a load-bearing safety net; retiring sql_block required dismantling protection first.
  // unknown is registered by UnknownExtension and **does not retire with business kinds**.
  console.warn(`[canvas-to-pm] unknown kind=${section.kind}, degrading to unknown`);
  const s = section as {
    code?: string;
    sql?: string;
    markdown?: string;
    text?: string;
  };
  const degraded = {
    id: (section as { id: string }).id,
    kind: "unknown" as const,
    original_kind: String(section.kind ?? ""),
    // Preserve readable content where possible; emergency rollback must not lose body text just because frontend is older.
    text_content: s.code || s.sql || s.markdown || s.text || "",
  };
  return atomicToPMNode(degraded as ReportSection);
}

// ─── Atomic ─────────────────────────────────────────────────

function atomicToPMNode(section: ReportSection): JSONContent {
  // Extract id into attrs.sectionId; all other fields enter attrs.payload
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { id, kind, ...payload } = section as ReportSection & { id: string; kind: string };
  return {
    type: KIND_TO_NODE_NAME[section.kind as SectionKind],
    attrs: {
      sectionId: id,
      payload,
    },
  };
}

// ─── Text-based (narrative / deck / callout) ────────────────

function textBasedToPMNode(section: ReportSection): JSONContent {
  // Get markdown field
  const md = "markdown" in section ? (section as { markdown: string }).markdown : "";
  // citation-audit M3 (review P1): **stripLegacy hook for the main canvas body**
  // (one of three named in tech-design §3.5 r3 full hook inventory: chat md.ts / here / formatInlineTokens).
  // Why here: before M3 removed citation-plugin four gen2 parsing passes, raw markers were
  // **hidden by CSS** (`.citation-num{font-size:0}` + `::before{content:attr(data-display)}`
  // showing formatted values). Removing decoration passes removes the sole mask. Historical SSE replay
  // (old canvas.section_done events containing markers pass through canvas-sse → canvas-store → this function)
  // leaks raw markers into the body unless baked. Backend egress always returns original DB text without baking;
  // all fallback protection depends on this seam.
  const childNodes = markdownToPMNodes(stripLegacy(md));

  // meta: narrative.unverified_numbers / narrative.source_refs / callout.label
  const meta: Record<string, unknown> = {};
  if (section.kind === "narrative") {
    const nm = section as Extract<ReportSection, { kind: "narrative" }>;
    if (nm.unverified_numbers && nm.unverified_numbers.length > 0) {
      meta.unverified_numbers = nm.unverified_numbers;
    }
    // W1-1 / W3-9: narrative source_refs are held in meta
    // (text-based nodes lack atomic payload, so use meta)
    if (nm.source_refs && nm.source_refs.length > 0) {
      meta.source_refs = nm.source_refs;
    }
  }
  if (section.kind === "callout") {
    const cl = section as Extract<ReportSection, { kind: "callout" }>;
    meta.label = cl.label;
  }

  // A1-2 (report-edit-audit P0-1 root fix): lossless round-trip; store all keys unrepresentable in Markdown
  // (turn_id/style/rev/superseded/empty unverified_numbers arrays etc.) in meta.extras,
  // restored unchanged by pmNodeToSection. Otherwise shallowDiff (key union) marks them deleted →
  // every real narrative always changed:true → read-only flush validation always save_failed
  // (confirmed cause of export rejection), while every initial edit-mode flush sends phantom empty PATCH requests, needlessly increasing rev.
  // (Atomic nodes naturally preserve all fields via payload spread; only text-based/recursive nodes have this pitfall.)
  const extras: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(section)) {
    if (k === "id" || k === "kind" || k === "markdown") continue;
    extras[k] = v;
  }
  if (Object.keys(extras).length > 0) meta.extras = extras;

  return {
    type: KIND_TO_NODE_NAME[section.kind as SectionKind],
    attrs: {
      sectionId: section.id,
      meta,
    },
    content: childNodes,
  };
}

// ─── Recursive (kind="section") ─────────────────────────────

function recursiveToPMNode(section: ReportSection): JSONContent {
  const sec = section as Extract<ReportSection, { kind: "section" }>;
  // A1-2: same lossless round-trip as textBasedToPMNode; keep wrapper extra keys in meta.extras.
  const extras: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(sec)) {
    if (k === "id" || k === "kind" || k === "label" || k === "h2" || k === "body") continue;
    extras[k] = v;
  }
  return {
    type: "section_section",
    attrs: {
      sectionId: sec.id,
      label: sec.label,
      h2: sec.h2,
      meta: Object.keys(extras).length > 0 ? { extras } : {},
    },
    content: (sec.body || []).map(sectionToPMNode),
  };
}

// ─── Invariant preventing content loss (feedback-0713 #7) ─────────────────────────

/**
 * Whether doc carries substantive content: any non-whitespace text node or atomic section node
 * (chart/image/kpi etc., without text children but with payload).
 *
 * Used by report-view setContent rehydration gate: reject an empty server-side initialDoc
 * when the current editor is nonempty (losing content is less acceptable than one display sync mismatch).
 */
export function pmDocHasContent(doc: JSONContent | null | undefined): boolean {
  if (!doc) return false;
  let found = false;
  const walk = (n: JSONContent | undefined): void => {
    if (!n || found) return;
    if (n.type === "text" && typeof n.text === "string" && n.text.trim()) {
      found = true;
      return;
    }
    // review P2: visual atoms without section_ prefixes (figure_ref anchors / Markdown images)
    // also carry content; a valid figure_ref-only doc must not count as empty (which wrongly blocks cross-device updates).
    if (n.type === "figure_ref" || n.type === "image") {
      found = true;
      return;
    }
    const kids = Array.isArray(n.content) ? n.content : [];
    // Atomic sections without children count as content when carrying payload;
    // sections with children (narrative etc.) depend on actual text inside.
    if (typeof n.type === "string" && n.type.startsWith("section_") && kids.length === 0) {
      found = true;
      return;
    }
    kids.forEach(walk);
  };
  walk(doc);
  return found;
}

/**
 * Pure decision function for setContent content-loss prevention (report-view rehydration effect).
 *
 * Reject if target doc is empty && current editor is nonempty && local content is not explicitly discarded (forced).
 * forced=true means explicit user discard (cancel editing / conflict "重新载入" / history rollback):
 * blocking the server version then lets debounce-sync PATCH resurrect stale content on the next edit
 * (review P1), so explicit intent outranks the defensive gate.
 */
export function shouldRefuseSetContent(
  nextDoc: JSONContent | null | undefined,
  currentDoc: JSONContent | null | undefined,
  forced: boolean,
): boolean {
  if (forced) return false;
  return !pmDocHasContent(nextDoc) && pmDocHasContent(currentDoc);
}
