/**
 * Canvas PM Schema — PM node definitions for 24 section kinds.
 *
 * Four categories (aligned with P3.1 design document §3):
 *   - ATOMIC (19): complete atomic nodes, attrs.payload holds the original ReportSection literal
 *     - A. fully read-only (9): meta/data_table/code_block/spec_block/datarc_iframe/html_artifact/external_iframe/image (multimodal-v1)/unknown (degradation target)
 *     - B. selected editable fields (9): eyebrow/h1/kpi_row/chart/next_list/sources/recommendations/insight_card/design
 *   - TEXT_BASED (3): narrative/deck/callout, wrapper node + standard child PM blocks (paragraph/heading/list/...)
 *   - RECURSIVE (1): section, wrapper node + nested section_* children (any group: section)
 *
 * Group design:
 *   - All section_* nodes use group "section block"
 *   - Top-level CanvasDocument content: "section+" (only section_* children)
 *   - section_section content: "section+" (recursively accepts other section_*)
 *   - section_narrative/deck/callout content: "block+" (standard PM blocks, excluding section_* because they use 'section block', although narrative nesting sections is unexpected; constrain later in P3.2 if it occurs)
 */

import { Node, mergeAttributes } from "@tiptap/core";
import { Document } from "@tiptap/extension-document";
import { ReactNodeViewRenderer } from "@tiptap/react";
import type { NodeViewProps } from "@tiptap/react";
import type { ComponentType } from "react";

// ───────────────────────────────────────────────────────────
// Kind categories (aligned with lib/types.ts ReportSection, 24 kinds total)
// ───────────────────────────────────────────────────────────

export const ATOMIC_KINDS = [
  // A. Fully read-only
  "meta",
  "data_table",
  // Sprint ι: code_block replaces sql_block as the primary write-side kind;
  // sql_block stays as legacy alias for 90-day grace per PRD §4.A.2.
  "code_block",
  // S3: external DSL embed blocks (mermaid etc.)
  "spec_block",
  "datarc_iframe",
  "html_artifact",
  "external_iframe",
  // multimodal-v1: AI-generated images (artifact_id reference; caption/alt only patched by canvas-writer)
  "image",
  // B. Selected editable fields (P3.2 adds forms)
  // multimodal-v2A: background image + editable text layers
  "design",
  "eyebrow",
  "h1",
  "kpi_row",
  "chart",
  "next_list",
  "sources",
  "recommendations",
  "insight_card",
  // Degradation target: never produced by backend, only written by canvas-to-pm fallback.
  "unknown",
] as const;
export type AtomicKind = (typeof ATOMIC_KINDS)[number];

export const TEXT_BASED_KINDS = ["narrative", "deck", "callout"] as const;
export type TextBasedKind = (typeof TEXT_BASED_KINDS)[number];

export const RECURSIVE_KINDS = ["section"] as const;
export type RecursiveKind = (typeof RECURSIVE_KINDS)[number];

export const ALL_KINDS = [
  ...ATOMIC_KINDS,
  ...TEXT_BASED_KINDS,
  ...RECURSIVE_KINDS,
] as const;
export type SectionKind = (typeof ALL_KINDS)[number];

// Sanity: 22 = 18 + 3 + 1
// (TS compile-time check; no runtime throw, documentation only)
export const TOTAL_KIND_COUNT = ALL_KINDS.length; // 22 (S3: +spec_block; V2A: +design; +unknown degradation target; −trend_chart/−attribution/−sql_block retired 2026-09-05)

// ───────────────────────────────────────────────────────────
// Top-level doc: restrict content to the section group (all section_*)
// ───────────────────────────────────────────────────────────

export const CanvasDocument = Document.extend({
  // Permissive: top level accepts any block (including section_*, group "section block").
  // Strict "section+" has setContent validation quirks; accepting paragraphs in production is harmless (forward produces only section_*).
  content: "block+",
});

// ───────────────────────────────────────────────────────────
// Helper: Atomic Section Node
// Usage: makeAtomicSectionNode({ kind: "chart", nodeViewComponent: ChartNodeView })
// For atomic kinds; each extension file calls it in one line.
// ───────────────────────────────────────────────────────────

export interface AtomicNodeOpts {
  kind: AtomicKind;
  nodeViewComponent: ComponentType<NodeViewProps>;
}

export function makeAtomicSectionNode(opts: AtomicNodeOpts) {
  return Node.create({
    name: `section_${opts.kind}`,
    group: "section block",
    atom: true,
    selectable: true,
    draggable: true,

    addAttributes() {
      return {
        sectionId: { default: null },
        // payload: full ReportSection literal (without id/kind, inferred from sectionId + node name)
        payload: { default: null },
      };
    },

    parseHTML() {
      return [{ tag: `div[data-section-kind='${opts.kind}']` }];
    },

    renderHTML({ HTMLAttributes }) {
      return [
        "div",
        mergeAttributes(HTMLAttributes, { "data-section-kind": opts.kind }),
      ];
    },

    addNodeView() {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return ReactNodeViewRenderer(opts.nodeViewComponent as any);
    },
  });
}

// ───────────────────────────────────────────────────────────
// Helper: Text-based Section Node (narrative / deck / callout)
// content="block+" accepts standard PM blocks.
// Forward adapter parses the markdown field into child PM nodes.
// Optional nodeViewComponent (wrapper UI such as narrative unverified_numbers warnings).
// ───────────────────────────────────────────────────────────

export interface TextBasedNodeOpts {
  kind: TextBasedKind;
  nodeViewComponent?: ComponentType<NodeViewProps>;
}

export function makeTextBasedSectionNode(opts: TextBasedNodeOpts) {
  const base = {
    name: `section_${opts.kind}`,
    group: "section block",
    content: "block+",
    defining: true, // Explicit boundaries; merging across sections requires crossing the wrapper
    // r3 §5.3: make the drag handle functional; without this flag PM does not initiate node drag, making batch 1 rc-handle
    // decorative for text-based nodes. Drag starts only from data-drag-handle (rc-handle), leaving text
    // selection unaffected.
    draggable: true,

    addAttributes() {
      return {
        sectionId: { default: null },
        // narrative.unverified_numbers / callout.label etc. can live in meta
        meta: { default: {} as Record<string, unknown> },
      };
    },

    parseHTML() {
      return [{ tag: `div[data-section-kind='${opts.kind}']` }];
    },

    renderHTML({ HTMLAttributes }: { HTMLAttributes: Record<string, unknown> }) {
      return [
        "div",
        mergeAttributes(HTMLAttributes, { "data-section-kind": opts.kind }),
        0, // Children render here
      ];
    },
  };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const config: any = opts.nodeViewComponent
    ? {
        ...base,
        addNodeView() {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          return ReactNodeViewRenderer(opts.nodeViewComponent as any);
        },
      }
    : base;
  return Node.create(config);
}

// ───────────────────────────────────────────────────────────
// Helper: Recursive Container Node (kind="section")
// content="section+" accepts other section_* children (including itself, recursive).
// attrs: label (eyebrow-style small text) + h2 (large heading)
// ───────────────────────────────────────────────────────────

export interface RecursiveNodeOpts {
  nodeViewComponent?: ComponentType<NodeViewProps>;
}

export function makeRecursiveSectionNode(opts: RecursiveNodeOpts = {}) {
  const base = {
    name: "section_section",
    group: "section block",
    content: "section+",
    defining: true,

    addAttributes() {
      return {
        sectionId: { default: null },
        label: { default: null as string | null },
        h2: { default: null as string | null },
        // A1-2 (report-edit-audit): extras carrier for lossless round-trip (same as text-based meta).
        meta: { default: {} as Record<string, unknown> },
      };
    },

    parseHTML() {
      return [{ tag: "div[data-section-kind='section']" }];
    },

    renderHTML({ HTMLAttributes }: { HTMLAttributes: Record<string, unknown> }) {
      return [
        "div",
        mergeAttributes(HTMLAttributes, { "data-section-kind": "section" }),
        0,
      ];
    },
  };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const config: any = opts.nodeViewComponent
    ? {
        ...base,
        addNodeView() {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          return ReactNodeViewRenderer(opts.nodeViewComponent as any);
        },
      }
    : base;
  return Node.create(config);
}

// ───────────────────────────────────────────────────────────
// PM node name ↔ Section kind mapping (used by forward/backward adapters)
// ───────────────────────────────────────────────────────────

export const KIND_TO_NODE_NAME: Record<SectionKind, string> = Object.fromEntries(
  ALL_KINDS.map((k) => [k, `section_${k}`]),
) as Record<SectionKind, string>;

export const NODE_NAME_TO_KIND: Record<string, SectionKind> = Object.fromEntries(
  ALL_KINDS.map((k) => [`section_${k}`, k]),
) as Record<string, SectionKind>;

// ───────────────────────────────────────────────────────────
// Category detection helpers (used by adapters)
// ───────────────────────────────────────────────────────────

export function isAtomicKind(k: SectionKind): k is AtomicKind {
  return (ATOMIC_KINDS as readonly string[]).includes(k);
}
export function isTextBasedKind(k: SectionKind): k is TextBasedKind {
  return (TEXT_BASED_KINDS as readonly string[]).includes(k);
}
export function isRecursiveKind(k: SectionKind): k is RecursiveKind {
  return (RECURSIVE_KINDS as readonly string[]).includes(k);
}
