/**
 * 20 Atomic Section Extensions (including code_block/spec_block/image/design).
 *
 * Each imports an existing components/canvas/sections/*.tsx using the makeAtomicExtension factory.
 * Chart is an exception (accepts spec instead of data), customized with toData.
 *
 * P3.1 does not support editing (all atomic nodes locked); P3.2 adds inline forms / modals.
 */

"use client";

import { useTranslations } from "next-intl";
import { useEffect, useId, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import type { DesignTextLayer, ReportSection } from "@/lib/types";
import type { NodeViewProps } from "@tiptap/react";
import { NodeViewWrapper } from "@tiptap/react";
import { ReactNodeViewRenderer } from "@tiptap/react";
import { makeAtomicExtension } from "./_helpers";
import { makeAtomicSectionNode } from "../schema";
import { useCanvasContext } from "../canvas-context";
import { InlineEditableText } from "./inline-text";
import { SectionChrome } from "./section-chrome";
import { ChartSourceLine } from "@/components/canvas/sections/chart-source-line";
import { DialogShell } from "@/components/ui/dialog";
import { flattenCanvasDesign } from "@/lib/api";
import { appPath } from "@/lib/base-path";
import { trackInteraction as track } from "@/lib/metrics";
import { formatInlineTokens } from "@/lib/format-inline-tokens";
import type { ChartSpec } from "@/lib/chart/types";
import { applyChartEdit, chartLayoutPreset, validChartComposition, type ChartEditTarget, type EditableChartPatch } from "@/lib/chart/composition";
import { Field } from "@/components/ui/field";
import { evidencePlanForViewer } from "@/lib/chart/presentation-plan";
import { checkKindShape, checkKindUnits } from "@/lib/chart/kind-shape";
import { normalizeChartForRenderingV1 } from "@/lib/chart/normalize-chart-for-rendering";

// ─── A. Fully read-only (10) ─────────────────────────────────────────

import { MetaSection } from "@/components/canvas/sections/meta";
import { DataTableSection } from "@/components/canvas/sections/data-table";
import { CodeBlockSection } from "@/components/canvas/sections/code-block";
import { SpecBlock as SpecBlockSection } from "@/components/canvas/sections/spec-block";
import { DatarcIframeSection } from "@/components/canvas/sections/datarc-iframe";
import { HtmlArtifactSection } from "@/components/canvas/sections/html-artifact";
import { ExternalIframeSection } from "@/components/canvas/sections/external-iframe";
import { UnknownSection } from "@/components/canvas/sections/unknown-section";
import { ImageSection } from "@/components/canvas/sections/image";
import { DesignSection } from "@/components/canvas/sections/design";

export const MetaExtension = makeAtomicExtension({
  kind: "meta",
  Component: MetaSection,
});
export const DataTableExtension = makeAtomicExtension({
  kind: "data_table",
  Component: DataTableSection,
});
// Sprint ι (PRD §4.A): code_block uses the same dual-kind component.
export const CodeBlockExtension = makeAtomicExtension({
  kind: "code_block",
  Component: CodeBlockSection,
});
// S3: spec_block embeds external DSLs (mermaid etc.), rendered through the SpecBlock registry.
export const SpecBlockExtension = makeAtomicExtension({
  kind: "spec_block",
  Component: SpecBlockSection,
});
export const DatarcIframeExtension = makeAtomicExtension({
  kind: "datarc_iframe",
  Component: DatarcIframeSection,
});
export const HtmlArtifactExtension = makeAtomicExtension({
  kind: "html_artifact",
  Component: HtmlArtifactSection,
});
export const ExternalIframeExtension = makeAtomicExtension({
  kind: "external_iframe",
  Component: ExternalIframeSection,
});
// Degradation target: canvas-to-pm.ts writes this when it reads an unknown kind.
// ⚠ This registration **must not retire with business kinds**; it is the safety net itself.
export const UnknownExtension = makeAtomicExtension({
  kind: "unknown",
  Component: UnknownSection,
});
// multimodal-v1: AI-generated images (fully read-only; caption/alt only patched by canvas-writer)
export const ImageExtension = makeAtomicExtension({
  kind: "image",
  Component: ImageSection,
});

type DesignData = Extract<ReportSection, { kind: "design" }>;
type DesignPayload = Omit<DesignData, "id" | "kind">;

function payloadFromDesignSection(section: DesignData): DesignPayload {
  return Object.fromEntries(
    Object.entries(section).filter(([key]) => key !== "id" && key !== "kind"),
  ) as DesignPayload;
}

export function DesignEditNodeView({ node, updateAttributes }: NodeViewProps) {
  const t = useTranslations("canvas");
  const {
    isEditMode,
    canvasId,
    flushPendingEdits,
    getSectionRev,
    acceptServerSection,
  } = useCanvasContext();
  const payload = (node.attrs.payload ?? {}) as DesignPayload;
  const sectionId = node.attrs.sectionId as string;
  const [selectedLayerId, setSelectedLayerId] = useState<string | null>(null);
  const [flattening, setFlattening] = useState(false);
  const [flattenError, setFlattenError] = useState<string | null>(null);
  const flattenCacheIsCurrent =
    typeof payload.flattened_source_rev !== "number"
    || typeof payload.rev !== "number"
    || payload.flattened_source_rev + 1 === payload.rev;
  const [flattenUrl, setFlattenUrl] = useState<string | null>(
    payload.flattened_artifact_id && flattenCacheIsCurrent
      ? appPath(`/api/v1/files/${payload.flattened_artifact_id}`)
      : null,
  );
  useEffect(() => {
    setFlattenUrl(
      payload.flattened_artifact_id && flattenCacheIsCurrent
        ? appPath(`/api/v1/files/${payload.flattened_artifact_id}`)
        : null,
    );
  }, [payload.flattened_artifact_id, flattenCacheIsCurrent]);
  const data = {
    id: sectionId,
    kind: "design" as const,
    title: payload.title ?? t("editor_design_title_fallback"),
    size_preset: payload.size_preset ?? "portrait_3_4",
    canvas_width: payload.canvas_width ?? 1080,
    canvas_height: payload.canvas_height ?? 1440,
    background: payload.background,
    layers: payload.layers ?? [],
    flattened_artifact_id: payload.flattened_artifact_id ?? null,
    flattened_source_rev: payload.flattened_source_rev ?? null,
    flattened_at: payload.flattened_at ?? null,
  } as DesignData;
  const effectiveSelectedLayerId =
    selectedLayerId ?? (isEditMode ? data.layers[0]?.id ?? null : null);

  const updateLayer = (layerId: string, patch: Partial<DesignTextLayer>) => {
    if (data.layers.find((layer) => layer.id === layerId)?.locked) return;
    const nextLayers = data.layers.map((layer) => (
      layer.id === layerId ? { ...layer, ...patch } : layer
    ));
    setFlattenUrl(null);
    updateAttributes({
      payload: {
        ...payload,
        layers: nextLayers,
        flattened_artifact_id: null,
        flattened_source_rev: null,
        flattened_at: null,
      },
    });
  };
  const handleFlatten = async () => {
    if (!canvasId || flattening) return;
    setFlattening(true);
    setFlattenError(null);
    track("bowhead_design_flatten_ui_total", { action: "start" });
    try {
      const flushResult = await flushPendingEdits?.();
      if (flushResult && !flushResult.ok) {
        throw new Error(flushResult.message);
      }
      const expectedRev = getSectionRev?.(sectionId);
      const result = await flattenCanvasDesign(canvasId, sectionId, expectedRev);
      if (result.section.kind === "design" && result.section.id === sectionId) {
        acceptServerSection?.(result.section);
        updateAttributes({ payload: payloadFromDesignSection(result.section) });
      }
      const publicFlattenUrl = appPath(result.url);
      setFlattenUrl(publicFlattenUrl);
      if (typeof window !== "undefined") {
        window.location.href = publicFlattenUrl;
      }
      track("bowhead_design_flatten_ui_total", { action: "done" });
    } catch (err) {
      setFlattenError(err instanceof Error ? err.message : t("editor_export_failed"));
      track("bowhead_design_flatten_ui_total", { action: "fail" });
    } finally {
      setFlattening(false);
    }
  };

  return (
    <NodeViewWrapper
      contentEditable={false}
      data-drag-handle
      data-section-id={sectionId}
    >
      <SectionChrome sectionId={sectionId} kind="design">
        <DesignSection
          data={data}
          isEditMode={isEditMode}
          selectedLayerId={effectiveSelectedLayerId}
          downloadUrl={flattenUrl}
          isFlattening={flattening}
          flattenError={flattenError}
          onSelectLayer={setSelectedLayerId}
          onLayerTextChange={(layerId, text) => updateLayer(layerId, { text })}
          onLayerStyleChange={updateLayer}
          onFlatten={canvasId ? handleFlatten : undefined}
        />
      </SectionChrome>
    </NodeViewWrapper>
  );
}

export const DesignExtension = makeAtomicSectionNode({
  kind: "design",
  nodeViewComponent: DesignEditNodeView,
});

// ─── B. Selected editable fields (9) ────────────────────────────────────

import { KpiRowSection, KpiValueText, kpiValueSizeTier } from "@/components/canvas/sections/kpi-row";
import { NextListSection } from "@/components/canvas/sections/next-list";
import { SourcesSection } from "@/components/canvas/sections/sources";
import { RecommendationsSection } from "@/components/canvas/sections/recommendations";
import { InsightCardSection } from "@/components/canvas/sections/insight-card";
import { CharIcon } from "@/components/char-icon";
import { kpiMaxColumns } from "@/lib/kpi-grid";
import { useKpiColumns } from "@/lib/use-kpi-columns";
// ChartBlock loaded separately with dynamic import (chart.js bundle ~250KB lazy)
const ChartBlock = dynamic(
  () => import("@/components/canvas/sections/chart-block").then((m) => m.ChartBlock),
  {
    ssr: false,
    loading: () => (
      <div
        style={{
          height: 280,
          background: "#f5f5f5",
          borderRadius: 6,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#999",
          fontSize: "0.8125rem",
        }}
      >
        loading chart...
      </div>
    ),
  },
);

// Custom eyebrow / h1 NodeViews add inline editing (versus generic makeAtomicExtension)
//
// Streaming placeholder: backend emits canvas.section_streaming (empty payload)
// → canvas.section_done (fills text) in order; NodeView lacks text between them. Previously return null left h1
// completely blank on first viewing (streaming), then a 40px italic heading appeared on second viewing (done),
// a strong visual contrast misread as wrong font size. Now streaming uses a skeleton (fixed height + shimmer),
// switching to actual rendering on done for visual continuity.
function EyebrowEditNodeView({ node, updateAttributes }: NodeViewProps) {
  const t = useTranslations("canvas");
  const { isEditMode, isStreaming } = useCanvasContext();
  const payload = (node.attrs.payload ?? {}) as { text?: string };
  const sectionId = node.attrs.sectionId as string;
  if (!payload.text && !isEditMode) {
    if (!isStreaming) return null;
    return (
      <NodeViewWrapper contentEditable={false} data-section-id={sectionId}>
        <div className="v2-section-skeleton v2-skeleton-eyebrow" aria-hidden />
      </NodeViewWrapper>
    );
  }
  return (
    <NodeViewWrapper contentEditable={false} data-section-id={sectionId} data-drag-handle>
      <InlineEditableText
        as="div"
        className="v2-report-eyebrow"
        value={payload.text ?? ""}
        isEditMode={isEditMode}
        onChange={(v) => updateAttributes({ payload: { ...payload, text: v } })}
        placeholder={t("editor_placeholder_label")}
      />
    </NodeViewWrapper>
  );
}
export const EyebrowExtension = makeAtomicSectionNode({
  kind: "eyebrow",
  nodeViewComponent: EyebrowEditNodeView,
});

function H1EditNodeView({ node, updateAttributes }: NodeViewProps) {
  const t = useTranslations("canvas");
  const { isEditMode, isStreaming } = useCanvasContext();
  const payload = (node.attrs.payload ?? {}) as { text?: string };
  const sectionId = node.attrs.sectionId as string;
  if (!payload.text && !isEditMode) {
    if (!isStreaming) return null;
    return (
      <NodeViewWrapper contentEditable={false} data-section-id={sectionId}>
        <div className="v2-section-skeleton v2-skeleton-h1" aria-hidden />
      </NodeViewWrapper>
    );
  }
  return (
    <NodeViewWrapper contentEditable={false} data-section-id={sectionId} data-drag-handle>
      <InlineEditableText
        as="h1"
        className="v2-report-h1"
        value={payload.text ?? ""}
        isEditMode={isEditMode}
        onChange={(v) => updateAttributes({ payload: { ...payload, text: v } })}
        placeholder={t("editor_placeholder_title")}
      />
    </NodeViewWrapper>
  );
}
export const H1Extension = makeAtomicSectionNode({
  kind: "h1",
  nodeViewComponent: H1EditNodeView,
});
// kpi_row: cells[].label inline editable; val/sub immutable (LLM calculations)
function KpiRowEditNodeView({ node, updateAttributes }: NodeViewProps) {
  const t = useTranslations("canvas");
  const { isEditMode } = useCanvasContext();
  const payload = (node.attrs.payload ?? {}) as { cells?: Array<{ lbl: string; val: string; sub?: string; subTone?: string }> };
  const sectionId = node.attrs.sectionId as string;
  const cells = payload.cells ?? [];
  const kpiGrid = useKpiColumns<HTMLDivElement>(cells.length);
  if (cells.length === 0) return null;
  return (
    <NodeViewWrapper contentEditable={false} data-section-id={sectionId} data-drag-handle>
      <div ref={kpiGrid.ref} className="v2-kpi-row" data-max-cols={kpiMaxColumns(cells.length)} data-cols={kpiGrid.columns ?? undefined}>
        {cells.map((c, i) => (
          <div key={i} className="v2-kpi-cell">
            <InlineEditableText
              as="div"
              className="lbl"
              value={c.lbl}
              isEditMode={isEditMode}
              onChange={(v) => {
                const newCells = cells.map((cc, j) => j === i ? { ...cc, lbl: v } : cc);
                updateAttributes({ payload: { ...payload, cells: newCells } });
              }}
              placeholder={t("editor_placeholder_label")}
            />
            {/* feedback-triage-2026-08-26 (116 device: chat "建材用品 139.15%" fit one line at 17px,
                same canvas value wrapped to two lines at 32px): long-value tiers were added **only** to kpi-row.tsx
                KpiRowSection (reused by chat), while canvas actually uses this hand-written JSX, missing data-len.
                All three CSS tiers existed in globals and report-canvas; only this attribute was missing,
                so checking stylesheets alone falsely implied a fix. See kpiValueSizeTier for tier criteria. */}
            <div
              className="val"
              data-len={kpiValueSizeTier(c.val)}
              title={isEditMode ? t("editor_kpi_value_locked") : undefined}
              style={isEditMode ? { cursor: "not-allowed", opacity: 0.95 } : undefined}
            >
              {/* P2 batch 6: number and unit set apart (value stays read-only). */}
              <KpiValueText text={formatInlineTokens(c.val)} />
            </div>
            {c.sub ? (
              <div className={`sub ${c.subTone === "pos" ? "pos" : c.subTone === "neg" ? "neg" : ""}`}>
                {formatInlineTokens(c.sub)}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </NodeViewWrapper>
  );
}
export const KpiRowExtension = makeAtomicSectionNode({
  kind: "kpi_row",
  nodeViewComponent: KpiRowEditNodeView,
});
// next_list: items[].title / detail inline editable
function NextListEditNodeView({ node, updateAttributes }: NodeViewProps) {
  const t = useTranslations("canvas");
  const { isEditMode } = useCanvasContext();
  const payload = (node.attrs.payload ?? {}) as { items?: Array<{ title: string; detail: string; action?: string }> };
  const sectionId = node.attrs.sectionId as string;
  const items = payload.items ?? [];
  if (items.length === 0) return null;
  return (
    <NodeViewWrapper contentEditable={false} data-section-id={sectionId} data-drag-handle>
      <div className="v2-next-list">
        {items.map((it, i) => (
          <div key={i} className="v2-next-item" style={{ pointerEvents: isEditMode ? "auto" : undefined }}>
            <span className="arrow" aria-hidden><CharIcon ch="→" aria-hidden /></span>
            <span className="body">
              <InlineEditableText
                as="span"
                className="title"
                value={it.title}
                isEditMode={isEditMode}
                onChange={(v) => {
                  const newItems = items.map((x, j) => j === i ? { ...x, title: v } : x);
                  updateAttributes({ payload: { ...payload, items: newItems } });
                }}
              />
              {it.detail || isEditMode ? (
                <InlineEditableText
                  as="span"
                  className="detail"
                  value={it.detail}
                  isEditMode={isEditMode}
                  onChange={(v) => {
                    const newItems = items.map((x, j) => j === i ? { ...x, detail: v } : x);
                    updateAttributes({ payload: { ...payload, items: newItems } });
                  }}
                  placeholder={t("editor_placeholder_detail")}
                />
              ) : null}
            </span>
          </div>
        ))}
      </div>
    </NodeViewWrapper>
  );
}
export const NextListExtension = makeAtomicSectionNode({
  kind: "next_list",
  nodeViewComponent: NextListEditNodeView,
});
// sources: entries[].label inline editable; tool/sql/artifact_id immutable
function SourcesEditNodeView({ node }: NodeViewProps) {
  const payload = (node.attrs.payload ?? {}) as Extract<ReportSection, { kind: "sources" }>;
  const sectionId = node.attrs.sectionId as string;
  const entries = payload.entries ?? [];
  if (entries.length === 0) return null;
  return (
    <NodeViewWrapper contentEditable={false} data-section-id={sectionId} data-drag-handle>
      <SourcesSection data={{ ...payload, id: sectionId, kind: "sources", entries }} />
    </NodeViewWrapper>
  );
}
export const SourcesExtension = makeAtomicSectionNode({
  kind: "sources",
  nodeViewComponent: SourcesEditNodeView,
});

// Unify on one renderer: delegate to RecommendationsSection (design .rc-rec, soft-background pills, no emoji),
// removing inline duplication (old inline implementation duplicated sections/recommendations.tsx, allowing one-sided changes).
// Editing uses RecommendationsSection built-in dispatchPatch (same pattern as InsightCardSection).
function RecommendationsEditNodeView({ node }: NodeViewProps) {
  const sectionId = node.attrs.sectionId as string;
  const payload = (node.attrs.payload ?? {}) as { items?: Array<{ priority: "P0"|"P1"|"P2"; action: string; owner?: string; timeline?: string; expected_impact?: string; rationale?: string }> };
  const items = payload.items ?? [];
  if (items.length === 0) return null;
  const data = { id: sectionId, kind: "recommendations" as const, items };
  return (
    <NodeViewWrapper contentEditable={false} data-section-id={sectionId} data-drag-handle>
      <RecommendationsSection data={data} />
    </NodeViewWrapper>
  );
}
export const RecommendationsExtension = makeAtomicSectionNode({
  kind: "recommendations",
  nodeViewComponent: RecommendationsEditNodeView,
});

/**
 * insight_card NodeView: thesis inline editable, evidence/impact immutable (LLM calculations).
 * Simplification: pass modified data so rendering replaces thesis with inline editing.
 *   - Edit mode: inline thesis above; pass thesis="" to InsightCardSection to hide its built-in thesis.
 *   - Read mode: pass original data directly.
 *
 * P3.2.5c: evidence[].text inline editing (complex, multiple evidence types)
 */
function InsightCardWrapper({ node, updateAttributes }: NodeViewProps) {
  const t = useTranslations("canvas");
  const { isEditMode, chartLookup } = useCanvasContext();
  const payload = (node.attrs.payload ?? {}) as Record<string, unknown>;
  const sectionId = node.attrs.sectionId as string;
  const data = { id: sectionId, kind: "insight_card" as const, ...payload } as Extract<ReportSection, { kind: "insight_card" }>;
  const dataForChild = isEditMode ? { ...data, thesis: "" } : data;
  return (
    <NodeViewWrapper contentEditable={false} data-section-id={sectionId} data-drag-handle>
      {isEditMode && (
        <InlineEditableText
          as="p"
          value={(payload.thesis as string) ?? ""}
          isEditMode={isEditMode}
          onChange={(v) => updateAttributes({ payload: { ...payload, thesis: v } })}
          placeholder={t("editor_placeholder_thesis")}
          multiline
          style={{
            fontSize: "0.95rem",
            color: "var(--v2-fg-muted, #4b5563)",
            margin: "4px 16px 8px",
            fontStyle: "italic",
          }}
        />
      )}
      <InsightCardSection data={dataForChild} chartLookup={chartLookup} />
    </NodeViewWrapper>
  );
}
export const InsightCardExtension = makeAtomicSectionNode({
  kind: "insight_card",
  nodeViewComponent: InsightCardWrapper,
});

// chart NodeView: P3.6 top source row + inline title/subtitle in edit mode + W12-4 ⚙ settings modal + ChartBlock
function ChartEditNodeView({ node, updateAttributes }: NodeViewProps) {
  const t = useTranslations("chart_settings");
  const { isEditMode, readOnly, isStreaming } = useCanvasContext();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const payload = (node.attrs.payload ?? {}) as { spec?: any; data_source?: any; rev?: number };
  const sectionId = node.attrs.sectionId as string;
  const spec = payload.spec;
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [editError, setEditError] = useState(false);
  const canEdit = isEditMode && !readOnly && !isStreaming;
  const evidencePlan = evidencePlanForViewer(spec?.presentation_plan);
  const canEditCopy = canEdit && (!spec?.presentation_plan || (evidencePlan?.dataset.availability === "available" && evidencePlan.evidence_notes.every((note) => note.availability === "available")));
  // In edit mode, pass modified spec to ChartBlock to blank its built-in title (avoid duplicate inline editing above)
  //
  // review R6-1: memoization is required. Previously a fresh object was built inline on every render; this component
  // rerenders on setSettingsOpen and any useCanvasContext change, causing ChartBlock
  // effectiveSpec/tableSpec recomputation and ChartTable reset-on-spec-change effect,
  // so clicking ⚙ settings in edit mode collapsed a 98-row expanded table to 10 rows and lost sorting.
  // ★ useMemo goes **before** if (!spec) return null; hooks after early returns caused
  // two failures this sprint (batch 3 summary row, batch 2 reference lines); chart-block.test.tsx guards deterministically.
  // eslint-disable-next-line react-hooks/rules-of-hooks, @typescript-eslint/no-explicit-any
  const specForChild: any = useMemo(
    () => (spec && canEdit ? { ...spec, title: "", subtitle: undefined } : spec),
    [spec, canEdit],
  );
  if (!spec) return null;
  const onSpecPatch = (patch: EditableChartPatch, target: ChartEditTarget = { scope: "root" }): boolean => {
    if (!canEdit) return false;
    try {
      const changed = applyChartEdit(spec, target, patch);
      updateAttributes({ payload: { ...payload, spec: changed } });
      setEditError(false);
      return true;
    } catch {
      setEditError(true);
      return false;
    }
  };
  return (
    <NodeViewWrapper contentEditable={false} data-section-id={sectionId} data-drag-handle>
      {canEdit && (
        <div style={{ padding: "0 4px", marginBottom: 4, display: "flex", alignItems: "flex-start", gap: 8 }}>
          <div style={{ flex: 1 }}>
            <InlineEditableText
              as="div"
              className="v2-chart-title"
              value={spec.title}
              isEditMode={canEditCopy}
              onChange={(v) => onSpecPatch({ title: v })}
              placeholder={t("chart_title_placeholder")}
            />
            <InlineEditableText
              as="div"
              className="v2-chart-subtitle"
              value={spec.subtitle ?? ""}
              isEditMode={canEditCopy}
              onChange={(v) => onSpecPatch({ subtitle: v || undefined })}
              placeholder={t("chart_subtitle_placeholder")}
              style={{ fontSize: "0.85em", color: "var(--c-ink-3, #888)" }}
            />
          </div>
          <button
            type="button"
            className="v2-icon-btn"
            onClick={() => setSettingsOpen(true)}
            title={t("settings_tooltip")}
            style={{ flexShrink: 0, fontSize: "0.875rem" }}
          >
            <CharIcon ch="⚙" />
          </button>
        </div>
      )}
      {editError ? <p role="alert" className="text-sm text-muted-foreground">{t("invalid_edit")}</p> : null}
      {/* Root identity and source changes reset display state without depending on suppressed titles. */}
      <ChartBlock
        key={`${sectionId}:${spec.data_artifact_id || ""}`}
        spec={specForChild}
        sectionId={sectionId}
        rootRevision={typeof payload.rev === "number" ? payload.rev : undefined}
        onKindChange={canEdit ? (kind) => { onSpecPatch({ kind }); } : undefined}
        onViewKindChange={canEdit ? (viewId, kind) => { onSpecPatch({ kind }, { scope: "view", viewId }); } : undefined}
      />
      {/* P3.6 PRD R5 → report-refactor P1 (design D8): source line moved to block bottom;
          without source, render the none state ("source not recorded" is an actual state and must not be omitted). */}
      {/* Batch 2/c10: section payload Datarc workspace provenance (historical placeholder when absent). */}
      <ChartSourceLine source={spec.source_meta ?? null} dataSource={payload.data_source ?? null}
        notes={evidencePlan?.evidence_notes}
        measureLabels={Object.fromEntries([spec, ...(spec.views?.map((view: { spec: ChartSpec }) => view.spec) ?? [])]
          .flatMap((chart: ChartSpec) => (chart.columns ?? []).map((column) => [column.key, column.header])))} />
      {settingsOpen && canEdit && (
        <ChartSettingsModal
          spec={spec}
          onSave={(patch, target) => { if (onSpecPatch(patch, target)) setSettingsOpen(false); }}
          onCancel={() => setSettingsOpen(false)}
        />
      )}
    </NodeViewWrapper>
  );
}

// W12-4: ChartSettingsModal edits advanced chart rendering options (kind / legend / data labels / percent stack).
// Root copy uses inline editing; child copy and arrangement share the existing dialog.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function ChartSettingsModal({
  spec, onSave, onCancel,
}: {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  spec: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  onSave: (patch: EditableChartPatch, target?: ChartEditTarget) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("chart_settings");
  const views: NonNullable<ChartSpec["views"]> = validChartComposition(spec) ? spec.views : [];
  const fieldId = useId();
  const [viewId, setViewId] = useState<string | null>(views[0]?.id ?? null);
  const selectedSpec = views.find((view) => view.id === viewId)?.spec ?? spec;
  const [childTitle, setChildTitle] = useState<string>(selectedSpec.title ?? "");
  const [layoutPreset, setLayoutPreset] = useState<"current" | "stack" | "grid2" | "split">("current");
  const [kind, setKind] = useState<ChartSpec["kind"]>(selectedSpec.kind);
  const [showLegend, setShowLegend] = useState<boolean>(selectedSpec.show_legend !== false);
  const [showDataLabels, setShowDataLabels] = useState<boolean>(!!selectedSpec.show_data_labels);
  const [percentStack, setPercentStack] = useState<boolean>(!!selectedSpec.percent_stack);
  const [invalidEdit, setInvalidEdit] = useState(false);

  const selectView = (id: string) => {
    if (!id) { setViewId(null); setInvalidEdit(false); return; }
    const view = views.find((candidate) => candidate.id === id);
    if (!view) return;
    setViewId(id);
    setChildTitle(view.spec.title);
    setKind(view.spec.kind);
    setShowLegend(view.spec.show_legend !== false);
    setShowDataLabels(!!view.spec.show_data_labels);
    setPercentStack(!!view.spec.percent_stack);
    setInvalidEdit(false);
  };

  const KIND_OPTIONS = [
    "line", "bar", "hbar", "pie", "stacked_bar", "dual_axis", "combo",
    "area_stacked", "scatter", "waterfall", "sankey", "heatmap", "kpi", "table",
  ];
  const supportsPercentStack = kind === "stacked_bar" || kind === "area_stacked";

  const handleSave = () => {
    if (views.length && viewId === null) {
      if (layoutPreset === "current") { onCancel(); return; }
      onSave({ layout: chartLayoutPreset(spec, layoutPreset) }, { scope: "root" });
      return;
    }
    const patch: EditableChartPatch = {
      kind,
      show_legend: showLegend,
      show_data_labels: showDataLabels,
      ...(viewId && childTitle !== selectedSpec.title ? { title: childTitle } : {}),
    };
    if (supportsPercentStack) {
      patch.percent_stack = percentStack;
    } else {
      patch.percent_stack = undefined;
    }
    if (Array.isArray(selectedSpec.columns) && kind !== selectedSpec.kind) {
      const candidate = { ...selectedSpec, kind } as ChartSpec;
      if (checkKindShape(normalizeChartForRenderingV1(candidate).spec, kind) || checkKindUnits(candidate, kind).length) {
        setInvalidEdit(true);
        return;
      }
    }
    if (viewId) onSave(patch, { scope: "view", viewId });
    else onSave(patch);
  };

  // Mount on body (DialogShell default): previous in-place chart NodeView rendering was trapped by report stacking contexts
  // (.canvas-report-shell / .v2-ws-right-body isolation, desktop .v2-app-root),
  // so z-index 9999 was effectively at layer 0; sidebar and split line painted over scrim/dialog (design section "图层").
  return (
    <DialogShell
      onClose={onCancel}
      label={t("title")}
      backdropStyle={{
        background: "rgba(0,0,0,0.4)",
        display: "flex", alignItems: "center", justifyContent: "center",
        zIndex: "var(--z-dialog)",
      }}
      panelProps={{ "data-testid": "chart-settings-modal" } as Record<string, string>}
      panelStyle={{
        background: "var(--c-surface, #fff)",
        border: "1px solid var(--c-line, #e5e7eb)",
        borderRadius: 8, padding: 20, minWidth: 0, width: "min(360px, calc(100vw - 32px))",
        maxHeight: "calc(100dvh - 32px)", overflowY: "auto",
        boxShadow: "0 8px 24px rgba(0,0,0,0.16)",
        color: "var(--c-ink, #1a1916)",
      }}
    >
      <div style={{ fontSize: "0.875rem", fontWeight: 600, marginBottom: 12 }}>{t("title")}</div>
      {views.length ? (
        <label style={{ display: "block", marginBottom: 10, fontSize: "0.75rem" }}>
          {t("view_label")}
          <select value={viewId ?? ""} onChange={(event) => selectView(event.target.value)}
            style={{ display: "block", width: "100%", marginTop: 4, padding: "4px 8px",
              border: "1px solid var(--c-line)", borderRadius: 4,
              background: "var(--c-bg, #fff)", color: "inherit", fontSize: "0.8125rem" }}>
            <option value="">{t("figure_layout")}</option>
            {views.map((view) => <option key={view.id} value={view.id}>{view.spec.title || view.id}</option>)}
          </select>
        </label>
      ) : null}
      {views.length && viewId === null ? (
        <Field id={`${fieldId}-layout`} label={t("layout_label")}>
          <select id={`${fieldId}-layout`} className="ui-input" value={layoutPreset} onChange={(event) => setLayoutPreset(event.target.value as typeof layoutPreset)}>
            <option value="current">{t("layout_current")}</option>
            <option value="stack">{t("layout_stack")}</option>
            <option value="grid2">{t("layout_grid2")}</option>
            {views.length === 2 ? <option value="split">{t("layout_split")}</option> : null}
          </select>
        </Field>
      ) : <>
      {viewId ? <Field id={`${fieldId}-child-title`} label={t("child_title_label")}>
        <input id={`${fieldId}-child-title`} className="ui-input" value={childTitle} onChange={(event) => setChildTitle(event.target.value)} />
      </Field> : null}
      <label style={{ display: "block", marginBottom: 10, fontSize: "0.75rem" }}>
        {t("kind_label")}
        <select
          value={kind}
          onChange={(e) => { setKind(e.target.value as ChartSpec["kind"]); setInvalidEdit(false); }}
          style={{
            display: "block", width: "100%", marginTop: 4, padding: "4px 8px",
            border: "1px solid var(--c-line)", borderRadius: 4,
            background: "var(--c-bg, #fff)", color: "inherit", fontSize: "0.8125rem",
          }}
        >
          {KIND_OPTIONS.map((k) => (
            <option key={k} value={k}>{k}</option>
          ))}
        </select>
      </label>
      <label style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8, fontSize: "0.75rem" }}>
        <input
          type="checkbox"
          checked={showLegend}
          onChange={(e) => setShowLegend(e.target.checked)}
        />
        {t("show_legend")}
      </label>
      <label style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8, fontSize: "0.75rem" }}>
        <input
          type="checkbox"
          checked={showDataLabels}
          onChange={(e) => setShowDataLabels(e.target.checked)}
        />
        {t("show_data_labels")}
      </label>
      {supportsPercentStack && (
        <label style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8, fontSize: "0.75rem" }}>
          <input
            type="checkbox"
            checked={percentStack}
            onChange={(e) => setPercentStack(e.target.checked)}
          />
          {t("percent_stack")}
        </label>
      )}
      </>}
      {invalidEdit ? <p role="alert" className="text-sm text-muted-foreground">{t("invalid_edit")}</p> : null}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
        <button
          type="button"
          onClick={onCancel}
          style={{
            all: "unset", cursor: "pointer", padding: "4px 10px",
            fontSize: "0.75rem", color: "var(--c-ink-3)",
          }}
        >{t("cancel")}</button>
        <button
          type="button"
          onClick={handleSave}
          style={{
            all: "unset", cursor: "pointer", padding: "4px 10px",
            fontSize: "0.75rem", color: "var(--c-accent)", fontWeight: 500,
          }}
        >{t("save")}</button>
      </div>
    </DialogShell>
  );
}
export const ChartExtension = makeAtomicSectionNode({
  kind: "chart",
  nodeViewComponent: ChartEditNodeView,
});

// ─── Collection: 20 atomic extensions (code_block + spec_block + image + design included) ───

export const ALL_ATOMIC_EXTENSIONS = [
  // A. Fully read-only
  MetaExtension,
  DataTableExtension,
  // Sprint ι (PRD §4.A): code_block uses same dual-kind renderer.
  CodeBlockExtension,
  // S3: spec_block external DSL embedding
  SpecBlockExtension,
  DatarcIframeExtension,
  HtmlArtifactExtension,
  ExternalIframeExtension,
  UnknownExtension,
  ImageExtension,
  // B. Selected editable fields
  DesignExtension,
  EyebrowExtension,
  H1Extension,
  KpiRowExtension,
  ChartExtension,
  NextListExtension,
  SourcesExtension,
  RecommendationsExtension,
  InsightCardExtension,
];
