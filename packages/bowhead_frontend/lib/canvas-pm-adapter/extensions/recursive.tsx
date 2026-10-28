/**
 * One Recursive Section Extension (kind="section").
 *
 * User decision: do not flatten; implement a real recursive container.
 * Wrapper renders label + h2; children are nested section_* nodes.
 */

"use client";

import { NodeViewWrapper, NodeViewContent } from "@tiptap/react";
import type { NodeViewProps } from "@tiptap/react";
import { makeRecursiveSectionNode } from "../schema";

function SectionContainerWrapper({ node }: NodeViewProps) {
  const label = node.attrs.label as string | null;
  const h2 = node.attrs.h2 as string | null;
  return (
    <NodeViewWrapper
      data-section-kind="section"
      style={{
        margin: "20px 0",
        paddingTop: 12,
      }}
    >
      {label && (
        <div
          style={{
            fontSize: "0.6875rem",
            fontWeight: 700,
            color: "var(--c-accent, #4f46e5)",
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            marginBottom: 4,
          }}
        >
          {label}
        </div>
      )}
      {h2 && (
        <h2
          style={{
            fontSize: "1.375rem",
            fontWeight: 700,
            margin: "0 0 12px",
            color: "var(--c-ink, #111)",
          }}
        >
          {h2}
        </h2>
      )}
      <NodeViewContent />
    </NodeViewWrapper>
  );
}

export const SectionContainerExtension = makeRecursiveSectionNode({
  nodeViewComponent: SectionContainerWrapper,
});

export const ALL_RECURSIVE_EXTENSIONS = [SectionContainerExtension];
