/**
 * report-engine Phase-1 (WP1) — `figure_ref` inline figure node.
 *
 * A thin reference node embedded in prose (`[figure:<section_id>]` block anchor).
 * It looks the target figure section up from CanvasContext.sections by id and
 * renders the EXISTING section component inline — the figure section itself
 * stays canonical in the store (INV-1, never moved). Mirrors the live
 * `insight_card.evidence[].chart_section_id` + chartLookup precedent.
 *
 * Render delegation covers the `{ data }`-prop figure kinds + chart (`{ spec }`).
 * Other kinds (kpi_row/insight_card/design custom NodeViews) fall back to a
 * labeled placeholder for now — they carry edit-controller subtleties the
 * reviews flagged (design contentEditable focus, dispatchPatch path) and are a
 * follow-up slice.
 */

"use client";

import { useTranslations } from "next-intl";
import { Node } from "@tiptap/core";
import {
  NodeViewWrapper,
  ReactNodeViewRenderer,
  type NodeViewProps,
} from "@tiptap/react";
import dynamic from "next/dynamic";
import { memo, type ComponentType, type ReactNode } from "react";
import type { ReportSection } from "@/lib/types";
import { useCanvasContext } from "../canvas-context";
import { captionPrefixLength, stripCaptionPrefix } from "./figure-caption";
import { ChartSourceLine, type DataSourceRef } from "@/components/canvas/sections/chart-source-line";
import { evidencePlanForViewer } from "@/lib/chart/presentation-plan";

import { ImageSection } from "@/components/canvas/sections/image";
import { DataTableSection } from "@/components/canvas/sections/data-table";
import { DatarcIframeSection } from "@/components/canvas/sections/datarc-iframe";
import { HtmlArtifactSection } from "@/components/canvas/sections/html-artifact";
import { ExternalIframeSection } from "@/components/canvas/sections/external-iframe";

// chart.js is heavy + SSR-unsafe — dynamic import, matching atomic.tsx.
const ChartBlock = dynamic(
  () => import("@/components/canvas/sections/chart-block").then((m) => m.ChartBlock),
  { ssr: false },
);

// kinds whose existing component takes the default `{ data: ReportSection }` prop
// (registered via makeAtomicExtension's default toData).
const DATA_PROP_COMPONENTS = {
  image: ImageSection,
  data_table: DataTableSection,
  datarc_iframe: DatarcIframeSection,
  html_artifact: HtmlArtifactSection,
  external_iframe: ExternalIframeSection,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
} as Record<string, ComponentType<{ data: any }>>;

function FigurePlaceholder({ label, muted }: { label: string; muted?: boolean }) {
  return (
    <div
      className="rc-figure-ref-placeholder"
      style={{
        margin: "20px 0",
        padding: "16px 0",
        fontFamily: "var(--font-mono, monospace)",
        fontSize: "0.75rem",
        color: muted ? "var(--c-ink-3, #9ca3af)" : "var(--c-ink-2, #6b7280)",
        fontStyle: muted ? "italic" : undefined,
      }}
    >
      {label}
    </div>
  );
}

/** Dispatch a resolved figure section to its existing render component. */
function renderFigure(
  section: ReportSection,
  labelForKind: (kind: string) => string,
  captionText: string | null,
): ReactNode {
  const kind = section.kind;
  if (kind === "chart") {
    const spec = (section as Extract<ReportSection, { kind: "chart" }>).spec;
    return (
      <figure className="v2-chart-figure" style={{ margin: "20px 0" }}>
        {/* Isolate local display state by canonical root and source identity. */}
        <ChartBlock
          key={`${section.id}:${spec?.data_artifact_id || ""}`}
          spec={spec}
          sectionId={section.id}
          rootRevision={section.rev}
          captionText={captionText}
        />
        {/* Batch 1 2.4 (1-E): always render, matching atomic.tsx:556; without source, render the none state
            ("source not recorded" is an actual state and must not be omitted). Inline figures previously lacked a source line. */}
        <ChartSourceLine
          source={spec?.source_meta ?? null}
          dataSource={(section as { data_source?: DataSourceRef | null }).data_source ?? null}
          notes={evidencePlanForViewer(spec?.presentation_plan)?.evidence_notes}
          measureLabels={Object.fromEntries([spec, ...(spec?.views?.map((view) => view.spec) ?? [])]
            .flatMap((chart) => chart?.columns?.map((column) => [column.key, column.header]) ?? []))}
        />
      </figure>
    );
  }
  const Component = DATA_PROP_COMPONENTS[kind];
  if (Component) {
    return (
      <div className="rc-figure-ref-body" style={{ margin: "20px 0" }}>
        <Component data={section} />
      </div>
    );
  }
  // kpi_row / insight_card / design / recommendations / … → follow-up slice.
  return <FigurePlaceholder label={labelForKind(kind)} />;
}

function FigureRefNodeViewImpl({ node, editor, getPos }: NodeViewProps) {
  const t = useTranslations("canvas");
  const { sections, isStreaming } = useCanvasContext();
  const targetSectionId = (node.attrs.targetSectionId as string | null) ?? null;
  const section = targetSectionId
    ? sections.find((s) => s.id === targetSectionId)
    : undefined;

  // Batch 1 2.1b: the adjacent "图N：" paragraph is the caption, serving as the protagonist parser input (the same
  // rules as figure-caption.ts). Read failure fails open to null (ChartBlock falls back to spec.subtitle).
  let captionText: string | null = null;
  try {
    const pos = typeof getPos === "function" ? getPos() : null;
    if (typeof pos === "number") {
      const $pos = editor.state.doc.resolve(pos);
      const parent = $pos.parent;
      const index = $pos.index();
      if (index + 1 < parent.childCount) {
        const next = parent.child(index + 1);
        if (next.type.name === "paragraph" && captionPrefixLength(next.textContent) > 0) {
          captionText = stripCaptionPrefix(next.textContent);
        }
      }
    }
  } catch {
    captionText = null;
  }

  return (
    <NodeViewWrapper
      contentEditable={false}
      data-drag-handle
      data-figure-ref=""
      data-target-section-id={targetSectionId ?? ""}
      // A1-1 (report-edit-audit P0-1 root fix): export receipt surfaceMarkers use
      // data-export-section-id to locate inline figures (following insight-card). Without it,
      // charts woven into narrative are "absent" from the DOM, so V2 export always reports
      // surface_render_incomplete for writer-loop reports. Mark only successfully resolved sections; placeholders
      // must not falsely claim to be rendered.
      data-export-section-id={section ? (targetSectionId ?? undefined) : undefined}
    >
      {section ? (
        renderFigure(section, (k) => t("figure_ref_placeholder", { kind: k }), captionText)
      ) : isStreaming ? (
        <FigurePlaceholder label={t("figure_generating")} />
      ) : (
        <FigurePlaceholder label={t("figure_not_found")} muted />
      )}
    </NodeViewWrapper>
  );
}

const FigureRefNodeView = memo(FigureRefNodeViewImpl);

/**
 * The `figure_ref` Tiptap node. group "block" + atom so it sits between
 * paragraphs in a narrative's `block+` content (schema bet confirmed by review).
 */
export const FigureRefExtension = Node.create({
  name: "figure_ref",
  group: "block",
  atom: true,
  selectable: true,
  draggable: true,

  addAttributes() {
    return {
      targetSectionId: {
        default: null,
        parseHTML: (el: HTMLElement) => el.getAttribute("data-target-section-id"),
        renderHTML: (attrs: { targetSectionId?: string | null }) =>
          attrs.targetSectionId
            ? { "data-target-section-id": attrs.targetSectionId }
            : {},
      },
    };
  },

  parseHTML() {
    return [{ tag: "div[data-figure-ref]" }];
  },

  renderHTML({ HTMLAttributes }) {
    return ["div", { ...HTMLAttributes, "data-figure-ref": "" }];
  },

  addNodeView() {
    return ReactNodeViewRenderer(FigureRefNodeView);
  },
});
