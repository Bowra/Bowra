/**
 * Shared templates for Atomic / TextBased NodeViews.
 *
 * The 16 atomic extensions share 95% of their rendering logic, importing different React components.
 * Define each with a single makeAtomicExtension call.
 */

"use client";

import { NodeViewWrapper } from "@tiptap/react";
import type { NodeViewProps } from "@tiptap/react";
import { memo, type ComponentType } from "react";
import { staticTranslator } from "@/lib/locale-messages";
import type { ReportSection } from "@/lib/types";
import {
  makeAtomicSectionNode,
  makeTextBasedSectionNode,
  type AtomicKind,
  type TextBasedKind,
} from "../schema";
import { SectionChrome } from "./section-chrome";
import { CharIconText } from "@/components/char-icon";

/** Defensive fallback body for a section node that arrived without a sectionId. */
export function MissingSectionIdNote({ kind }: { kind: string }) {
  return (
    <div style={{ padding: 12, color: "#dc2626", fontSize: "0.75rem" }}>
      <CharIconText ch="⚠">{staticTranslator("canvas_node_fallback")("missing_section_id", { kind })}</CharIconText>
    </div>
  );
}

/**
 * Generic atomic extension factory.
 *
 * @param kind        section kind
 * @param Component   Existing React component in components/canvas/sections/*, accepting { data: ReportSection }
 * @param toData      Optional custom payload+id+kind → data prop conversion (chart components accept { spec } rather than { data })
 */
export function makeAtomicExtension<DataProp = { data: ReportSection }>(args: {
  kind: AtomicKind;
  Component: ComponentType<DataProp>;
  toData?: (payload: Record<string, unknown>, sectionId: string) => DataProp;
}) {
  // Default toData: reconstruct ReportSection { id, kind, ...payload } as the { data } prop
  const toData =
    args.toData ??
    ((payload: Record<string, unknown>, sectionId: string) => ({
      data: { id: sectionId, kind: args.kind, ...payload } as ReportSection,
    } as unknown as DataProp));

  // memo: identical spec/data references do not rerender (key to preventing chart flicker in the P3.0 spike)
  const MemoComponent = memo(args.Component as ComponentType<DataProp>) as ComponentType<DataProp>;

  function NodeView({ node }: NodeViewProps) {
    const payload = (node.attrs.payload ?? {}) as Record<string, unknown>;
    const sectionId = node.attrs.sectionId as string;
    if (!sectionId) {
      // Defensive fallback: the normal forward adapter requires sectionId, so this branch rarely runs.
      return (
        <NodeViewWrapper contentEditable={false}>
          <MissingSectionIdNote kind={args.kind} />
        </NodeViewWrapper>
      );
    }
    const dataProp = toData(payload, sectionId);
    return (
      <NodeViewWrapper
        contentEditable={false}
        data-drag-handle
        data-section-id={sectionId}
      >
        <SectionChrome sectionId={sectionId} kind={args.kind}>
          {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
          <MemoComponent {...(dataProp as any)} />
        </SectionChrome>
      </NodeViewWrapper>
    );
  }

  return makeAtomicSectionNode({
    kind: args.kind,
    nodeViewComponent: NodeView,
  });
}

/**
 * Text-based extension factory (narrative / deck / callout).
 * Uses no NodeView (PM renders a div by default); children are standard PM paragraphs/headings.
 *
 * Pass wrapperComponent to add an unverified_numbers warning to narrative sections.
 */
export function makeTextBasedExtension(args: {
  kind: TextBasedKind;
  /** Optional wrapper around children, rendering meta context (e.g. an unverified warning). */
  wrapperComponent?: ComponentType<NodeViewProps>;
}) {
  return makeTextBasedSectionNode({
    kind: args.kind,
    nodeViewComponent: args.wrapperComponent,
  });
}
