/**
 * Backward adapter — PM doc tree (JSONContent) → SectionPatch[].
 *
 * After users edit, getJSON() yields the PM doc; reverse-parse it into ReportSection shape,
 * compare against original sections to compute diffs, and produce a PATCH list.
 *
 * Callers (P3.2 edit mode) receiving SectionPatch[]:
 *   - Iterate items with .changed === true
 *   - PATCH /api/v1/canvas/{cid}/sections/{sectionId} { patch: <patch> }
 */

import type { JSONContent } from "@tiptap/react";
import type { ReportSection } from "@/lib/types";
import {
  NODE_NAME_TO_KIND,
  isAtomicKind,
  isTextBasedKind,
  isRecursiveKind,
  type SectionKind,
} from "./schema";
import { pmNodesToMarkdown } from "./markdown-pm";

export interface SectionPatch {
  sectionId: string;
  kind: SectionKind;
  /** When false, callers should skip PATCH (save network requests + revisions). */
  changed: boolean;
  /** Only fields differing from original (shallow diff, excluding id/kind). */
  patch: Partial<ReportSection>;
  /**
   * true = section Markdown is **degraded** output (serialization fell back to plain text, losing headings/bold/tables/
   * figure anchors). Callers **must not** PATCH it back, which would overwrite the original structure with plain text.
   * Read-only comparisons (dirty calculation / checking unsaved changes before export) do not persist and may ignore this marker.
   */
  degraded?: boolean;
}

/**
 * PM doc → SectionPatch[].
 *
 * @param doc        Latest editor doc (editor.getJSON())
 * @param original   Original sections (canvas-store baseline)
 */
export function pmDocToSections(
  doc: JSONContent,
  original: ReportSection[],
  /**
   * The same `original` sections after one trip through the editor (canonical → PM → canonical), keyed by id.
   * When given, a section counts as changed only if it differs from **this** projection. The Markdown and
   * PM representations are not a bijection (`  \n` is written back as `\\\n`, a heading followed by a
   * hard break becomes two blocks, a wrapper without `body` gains `body: []`), so comparing the editor
   * output against the stored text flags sections nobody touched and writes the normalization back over
   * them. See `projectSectionsThroughEditor`.
   */
  projected?: ReadonlyMap<string, ReportSection>,
): SectionPatch[] {
  const topNodes = (doc.content ?? []) as JSONContent[];
  const reconstructed: ReportSection[] = [];
  // Record degradation by **slot**, not sectionId lookup:
  //   ① Nested child degradation reports the child id, which never equals the outer wrapper id;
  //   ② Duplicate sectionId in the doc (copy/paste of an entire section node) makes id lookup falsely mark clean sections.
  // Slot alignment resolves both issues.
  const degradedFlags: boolean[] = [];
  for (const n of topNodes) {
    let degradedHere = false;
    const sec = pmNodeToSection(n, {
      onDegrade: () => {
        degradedHere = true;
      },
    });
    if (sec) {
      reconstructed.push(sec);
      degradedFlags.push(degradedHere);
    }
  }

  const originalMap = new Map(original.map((s) => [s.id, s]));
  const patches: SectionPatch[] = [];

  for (const [i, cur] of reconstructed.entries()) {
    const orig = originalMap.get(cur.id);
    const degraded = degradedFlags[i] === true;
    if (!orig) {
      // Editing creates a new section (not in the P3.1 main path; P3.4 AI rewrite may add sections)
      patches.push({
        sectionId: cur.id,
        kind: cur.kind as SectionKind,
        changed: true,
        patch: cur as Partial<ReportSection>,
        ...(degraded ? { degraded: true } : {}),
      });
      continue;
    }
    // Diff against the projection when one is available, so only fields the user changed are sent;
    // fall back to the stored section (sections added since the projection was taken).
    const reference = projected?.get(cur.id) ?? orig;
    const diff = shallowDiff(reference, cur);
    patches.push({
      sectionId: cur.id,
      kind: cur.kind as SectionKind,
      changed: Object.keys(diff).length > 0,
      patch: diff,
      ...(degraded ? { degraded: true } : {}),
    });
  }

  // Note: section deletion emits no patch here (P3.1 does not support delete).
  // When P3.2 adds delete, separately scan originalMap for sections absent from reconstructed.

  return patches;
}

/**
 * Reconstruct every top-level section of a PM doc, keyed by section id (no diffing).
 * Used to project stored sections through the same canonical → PM → canonical trip the editor applies.
 */
export function reconstructSectionsById(doc: JSONContent): Map<string, ReportSection> {
  const out = new Map<string, ReportSection>();
  for (const node of (doc.content ?? []) as JSONContent[]) {
    const section = pmNodeToSection(node);
    if (section && !out.has(section.id)) out.set(section.id, section);
  }
  return out;
}

/**
 * Single PM node JSON → ReportSection.
 * Return null for unknown types.
 */
export function pmNodeToSection(
  node: JSONContent,
  // onDegrade carries no sectionId: callers bind callbacks by **slot** (see pmDocToSections),
  // so nested child degradation correctly propagates to the outer wrapper, and duplicate ids do not contaminate each other.
  opts?: { onDegrade?: () => void },
): ReportSection | null {
  const nodeName = node.type;
  if (!nodeName || !nodeName.startsWith("section_")) return null;
  const kind = NODE_NAME_TO_KIND[nodeName];
  if (!kind) return null;

  const attrs = (node.attrs ?? {}) as Record<string, unknown>;
  const sectionId = attrs.sectionId as string;
  if (!sectionId) {
    console.warn(`[pm-to-canvas] node ${nodeName} 缺 sectionId, 跳过`);
    return null;
  }

  if (isAtomicKind(kind)) {
    const payload = (attrs.payload ?? {}) as Record<string, unknown>;
    // Reconstruct ReportSection: { id, kind, ...payload }
    return {
      id: sectionId,
      kind,
      ...payload,
    } as unknown as ReportSection;
  }

  if (isTextBasedKind(kind)) {
    const childNodes = (node.content ?? []) as JSONContent[];
    const md = pmNodesToMarkdown(childNodes, {
      onDegrade: (reason) => {
        console.warn(
          `[pm-to-canvas] section ${sectionId} markdown 降级, 拒绝落库: ${reason}`,
        );
        opts?.onDegrade?.();
      },
    });
    const meta = (attrs.meta ?? {}) as Record<string, unknown>;
    // A1-2 (report-edit-audit): canvas-to-pm stores all keys unrepresentable in Markdown in
    // meta.extras. Restore them unchanged here for lossless round-trips (otherwise shallowDiff unions keys
    // and considers turn_id/style etc. deleted → phantom changed; read-only exports always rejected).
    // Spread extras first, then reconstructed authoritative fields (markdown/label etc.) to override them.
    const extras = (meta.extras ?? {}) as Record<string, unknown>;

    if (kind === "narrative") {
      const out: Extract<ReportSection, { kind: "narrative" }> = {
        ...(extras as object),
        id: sectionId,
        kind: "narrative",
        markdown: md,
      };
      const uns = meta.unverified_numbers;
      if (Array.isArray(uns)) {
        out.unverified_numbers = uns as string[];
      }
      // W1-1 / W3-9: restore narrative source_refs from meta
      const srcRefs = meta.source_refs;
      if (Array.isArray(srcRefs)) {
        out.source_refs = srcRefs as string[];
      }
      return out;
    }
    if (kind === "deck") {
      return { ...(extras as object), id: sectionId, kind: "deck", markdown: md };
    }
    if (kind === "callout") {
      return {
        ...(extras as object),
        id: sectionId,
        kind: "callout",
        label: (meta.label as string) ?? "",
        markdown: md,
      };
    }
  }

  if (isRecursiveKind(kind)) {
    const childNodes = (node.content ?? []) as JSONContent[];
    const body: ReportSection[] = [];
    for (const c of childNodes) {
      // ⚠ Pass opts downward: nested narrative degradation must propagate to the **outer wrapper** slot.
      // Omitting it logs "拒绝落库" but persists normally, more dangerous than having no guard
      // because it creates the illusion of having fixed the issue.
      const sec = pmNodeToSection(c, opts);
      if (sec) body.push(sec);
    }
    // A1-2: restore extra wrapper keys from meta.extras (same as text-based).
    const meta = (attrs.meta ?? {}) as Record<string, unknown>;
    const extras = (meta.extras ?? {}) as Record<string, unknown>;
    return {
      ...(extras as object),
      id: sectionId,
      kind: "section",
      label: (attrs.label as string) ?? "",
      h2: (attrs.h2 as string) ?? "",
      body,
    };
  }

  return null;
}

/**
 * Shallow diff: return updated fields differing from orig (compare via JSON.stringify).
 * id/kind/rev never appear in the diff (primary key + type + server rev are immutable).
 * flattened_* are server-managed fields generated by design flattening; PM nodes may display them,
 * but debounced sync must not PATCH them back as local edits.
 */
function shallowDiff(
  orig: ReportSection,
  updated: ReportSection,
): Partial<ReportSection> {
  const diff: Record<string, unknown> = {};
  const o = orig as unknown as Record<string, unknown>;
  const u = updated as unknown as Record<string, unknown>;
  const keys = new Set([...Object.keys(o), ...Object.keys(u)]);
  for (const key of keys) {
    if (
      key === "id"
      || key === "kind"
      || key === "rev"
      || key === "flattened_artifact_id"
      || key === "flattened_source_rev"
      || key === "flattened_at"
    ) continue;
    const a = JSON.stringify(o[key]);
    const b = JSON.stringify(u[key]);
    if (a !== b) {
      diff[key] = u[key];
    }
  }
  return diff as Partial<ReportSection>;
}
