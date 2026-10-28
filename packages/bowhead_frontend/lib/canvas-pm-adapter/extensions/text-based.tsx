/**
 * 3 Text-based Section Extensions (narrative / deck / callout).
 *
 * Their markdown fields are expanded during forward adaptation into
 * standard PM blocks (paragraph/heading/list/...), so wrappers only need to:
 *   - narrative: render unverified_numbers warnings (above children)
 *   - callout: render label headings
 *   - deck: no wrapper needed (default div)
 *
 * Children use standard PM ContentEditable, giving users the standard Notion editing experience.
 */

"use client";

import { useTranslations } from "next-intl";
import { NodeViewWrapper, NodeViewContent } from "@tiptap/react";
import type { NodeViewProps } from "@tiptap/react";
import { makeTextBasedExtension } from "./_helpers";
import { useCanvasContext } from "../canvas-context";
import { InlineEditableText } from "./inline-text";
import { SectionChrome } from "./section-chrome";
import { UnverifiedNumbersNote } from "@/components/canvas/unverified-numbers-note";

/**
 * Narrative wrapper: render unverified_numbers warnings + children.
 * W11-4: wrap SectionChrome for consistent behavior (drag handle / focus ring / menu when handlers are supplied).
 */
function NarrativeWrapper({ node }: NodeViewProps) {
  const meta = (node.attrs.meta ?? {}) as { unverified_numbers?: string[] };
  const sectionId = (node.attrs.sectionId as string | null) ?? "";
  return (
    <NodeViewWrapper data-section-kind="narrative" data-section-id={sectionId || undefined}>
      <SectionChrome sectionId={sectionId || "narrative"} kind="narrative">
        <NodeViewContent />
        {/* B1a: SourceLinkageStrip's "数字待核对" navigation selector depends on this block's class */}
        <UnverifiedNumbersNote numbers={meta.unverified_numbers} />
      </SectionChrome>
    </NodeViewWrapper>
  );
}

/**
 * Deck wrapper: deck previously lacked a wrapper; W11-4 adds SectionChrome.
 */
function DeckWrapper({ node }: NodeViewProps) {
  const sectionId = (node.attrs.sectionId as string | null) ?? "";
  return (
    <NodeViewWrapper data-section-kind="deck" data-section-id={sectionId || undefined}>
      <SectionChrome sectionId={sectionId || "deck"} kind="deck">
        <NodeViewContent />
      </SectionChrome>
    </NodeViewWrapper>
  );
}

/**
 * Callout wrapper: label heading above children; label supports inline edits in edit mode.
 * W11-4 wraps SectionChrome.
 */
function CalloutWrapper({ node, updateAttributes }: NodeViewProps) {
  const t = useTranslations("canvas");
  const { isEditMode } = useCanvasContext();
  const meta = (node.attrs.meta ?? {}) as { label?: string };
  const label = meta.label ?? "";
  const sectionId = (node.attrs.sectionId as string | null) ?? "";
  return (
    <NodeViewWrapper data-section-kind="callout" data-section-id={sectionId || undefined}>
      <SectionChrome sectionId={sectionId || "callout"} kind="callout">
        {(label || isEditMode) && (
          <InlineEditableText
            as="div"
            value={label}
            isEditMode={isEditMode}
            onChange={(v) =>
              updateAttributes({ meta: { ...meta, label: v } })
            }
            placeholder={t("editor_placeholder_label")}
            style={{
              fontSize: "0.75rem",
              fontWeight: 600,
              color: "var(--c-accent, #4f46e5)",
              marginBottom: 4,
              textTransform: "uppercase",
              letterSpacing: "0.04em",
              display: "inline-block",
            }}
          />
        )}
        <NodeViewContent />
      </SectionChrome>
    </NodeViewWrapper>
  );
}

export const NarrativeExtension = makeTextBasedExtension({
  kind: "narrative",
  wrapperComponent: NarrativeWrapper,
});

export const DeckExtension = makeTextBasedExtension({
  kind: "deck",
  // W11-4: add a wrapper for consistent SectionChrome wrapping
  wrapperComponent: DeckWrapper,
});

export const CalloutExtension = makeTextBasedExtension({
  kind: "callout",
  wrapperComponent: CalloutWrapper,
});

export const ALL_TEXT_BASED_EXTENSIONS = [
  NarrativeExtension,
  DeckExtension,
  CalloutExtension,
];
