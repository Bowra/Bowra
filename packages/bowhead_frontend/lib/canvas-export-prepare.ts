import type {
  CanvasDTO,
  CanvasSurfaceEntryDTO,
} from "./api";
import type { Locale } from "./locale";
import type { ReportSection } from "./types";
import type { EffectiveChartStyle } from "./theme-config";
import {
  buildChartStyleExportFreshnessV2,
  buildChartStyleExportTheme,
  type ChartStyleExportFreshnessV2,
  type ChartStyleExportThemeV1,
  type EffectiveChartStyleV2,
} from "./chart/chart-style-export";
import {
  computeCanvasScreenVisualInputSha256V2,
  computeCanvasVisualInputSha256,
  sha256Canonical,
} from "./canvas-visual-identity";

export type PrepareExportFailureReason =
  | "active_canvas_generation"
  | "rewrite_in_progress"
  | "save_failed"
  | "revision_conflict"
  | "reorder_failed"
  | "unsaved_presentation_override"
  | "surface_projection_conflict"
  | "surface_render_incomplete";

export type PrepareExportResult =
  | {
      ok: true;
      mode: "v2";
      projectionToken: string;
      surfaceToken: string;
      surfaceContractVersion: 1;
      renderedSurfaceReceiptSha256: string;
      visualReceipts: ClientVisualReceipt[];
      theme: ChartStyleExportThemeV1 | ChartStyleExportFreshnessV2;
      locale: Locale;
      v2Capabilities: NonNullable<CanvasDTO["exportV2Capabilities"]>;
    }
  | {
      ok: false;
      reason: PrepareExportFailureReason;
      sectionId?: string;
      message: string;
    };

export type SurfaceRenderState =
  | "rendered"
  | "registered"
  | "missing"
  | "runtime_fallback";

type RenderedSurfaceReceiptEntry = {
  section_id: string;
  section_rev: number;
  placement: CanvasSurfaceEntryDTO["placement"];
  effective_kind: string;
  render_disposition: "rendered" | "registered";
  visual_input_sha256: string;
};

export type ClientVisualReceipt = {
  section_id: string;
  section_rev: number;
  visual_input_sha256: string;
  render_disposition: "rendered";
} | {
  section_id: string;
  section_rev: number;
  surface_token: string;
  screen_visual_input_sha256: string;
  render_disposition: "rendered";
};

export async function validateAndBuildRenderedSurfaceReceipt(args: {
  detail: CanvasDTO;
  localSections: ReportSection[];
  locale: Locale;
  chartStyle: EffectiveChartStyle | undefined;
  chartStyleV2?: EffectiveChartStyleV2;
  revisionOf: (sectionId: string) => number | undefined;
  renderStateOf: (entry: CanvasSurfaceEntryDTO) => SurfaceRenderState;
  exposeVisualIdentity?: (
    entry: CanvasSurfaceEntryDTO,
    visualInputSha256: string,
    renderDisposition: "rendered",
  ) => void;
}): Promise<PrepareExportResult> {
  const {
    detail,
    localSections,
    locale,
    chartStyle,
    chartStyleV2,
    revisionOf,
    renderStateOf,
    exposeVisualIdentity,
  } = args;
  const plan = detail.surfaceProjection;
  if (!chartStyle && !chartStyleV2) {
    return projectionFailure("Chart style has not finished loading");
  }
  const legacyTheme = chartStyle
    ? buildChartStyleExportTheme(chartStyle)
    : null;
  const theme = chartStyleV2
    ? buildChartStyleExportFreshnessV2(chartStyleV2)
    : legacyTheme!;
  if (
    !detail.projectionToken
    || !detail.surfaceToken
    || !plan
    || plan.contractVersion !== 1
    || plan.contentProjectionToken !== detail.projectionToken
    || plan.surfaceToken !== detail.surfaceToken
  ) {
    return projectionFailure("Canvas surface projection is missing or internally inconsistent");
  }
  const surfaceToken = detail.surfaceToken;

  const localById = new Map(localSections.map((section) => [section.id, section]));
  const serverById = new Map(detail.sections.map((section) => [section.id, section]));
  const accountedIds = new Set([
    ...plan.entries.map((entry) => entry.sectionId),
    ...plan.exclusions.map((entry) => entry.sectionId),
  ]);
  for (const section of localSections) {
    if (!accountedIds.has(section.id)) {
      return projectionFailure(
        `Local section ${section.id} is not accounted for by the server surface plan`,
        section.id,
      );
    }
  }

  const orderedEntries = [...plan.entries].sort((left, right) => left.order - right.order);
  if (
    new Set(orderedEntries.map((entry) => entry.sectionId)).size !== orderedEntries.length
    || orderedEntries.some((entry, index) => entry.order !== index)
  ) {
    return projectionFailure("Canvas surface plan contains duplicate or non-contiguous entries");
  }

  const receiptResults = await Promise.all(orderedEntries.map(async (entry) => {
    const local = localById.get(entry.sectionId);
    const server = serverById.get(entry.sectionId);
    if (!local || !server) {
      return projectionFailure(
        `Section ${entry.sectionId} is missing from the local or refreshed Canvas state`,
        entry.sectionId,
      );
    }
    const localRev = revisionOf(entry.sectionId) ?? local.rev;
    if (
      localRev !== entry.sectionRev
      || server.rev !== entry.sectionRev
      || local.kind !== entry.effectiveKind
      || server.kind !== entry.effectiveKind
    ) {
      return projectionFailure(
        `Section ${entry.sectionId} does not match the refreshed surface plan`,
        entry.sectionId,
      );
    }

    const renderState = renderStateOf(entry);
    if (renderState === "missing") {
      return renderFailure(
        `Section ${entry.sectionId} is missing from the rendered surface`,
        entry.sectionId,
      );
    }
    if (renderState === "runtime_fallback") {
      return renderFailure(
        `Section ${entry.sectionId} is using a runtime fallback`,
        entry.sectionId,
      );
    }
    let visualInputSha256: string;
    let visualReceipt: ClientVisualReceipt | undefined;
    if (entry.parityRequirement === "exact_visual") {
      const localVisualInputSha256 = chartStyleV2
        ? await computeCanvasScreenVisualInputSha256V2({
            section: local,
            locale,
            surfaceToken,
            effectiveChartStyle: chartStyleV2,
          })
        : await computeCanvasVisualInputSha256({
            section: local,
            locale,
            theme: legacyTheme!,
          });
      if (
        !localVisualInputSha256
      ) {
        return projectionFailure(
          `Visual input for section ${entry.sectionId} does not match the server surface plan`,
          entry.sectionId,
        );
      }
      if (renderState !== "rendered") {
        return renderFailure(
          `Visual section ${entry.sectionId} is not rendered`,
          entry.sectionId,
        );
      }
      visualInputSha256 = localVisualInputSha256;
      visualReceipt = chartStyleV2
        ? {
            section_id: entry.sectionId,
            section_rev: entry.sectionRev,
            surface_token: surfaceToken,
            screen_visual_input_sha256: localVisualInputSha256,
            render_disposition: "rendered",
          }
        : {
            section_id: entry.sectionId,
            section_rev: entry.sectionRev,
            visual_input_sha256: localVisualInputSha256,
            render_disposition: "rendered",
          };
      exposeVisualIdentity?.(
        entry,
        localVisualInputSha256,
        "rendered",
      );
    } else {
      visualInputSha256 = await sha256Canonical(server);
    }
    return {
      receipt: {
        section_id: entry.sectionId,
        section_rev: entry.sectionRev,
        placement: entry.placement,
        effective_kind: entry.effectiveKind,
        render_disposition: renderState,
        visual_input_sha256: visualInputSha256,
      } satisfies RenderedSurfaceReceiptEntry,
      visualReceipt,
    };
  }));

  const failure = receiptResults.find((entry): entry is Extract<PrepareExportResult, { ok: false }> => (
    "ok" in entry && !entry.ok
  ));
  if (failure) return failure;
  const successes = receiptResults as Array<{
    receipt: RenderedSurfaceReceiptEntry;
    visualReceipt?: ClientVisualReceipt;
  }>;
  const entries = successes.map((entry) => entry.receipt);
  return {
    ok: true,
    mode: "v2",
    projectionToken: detail.projectionToken,
    surfaceToken: detail.surfaceToken,
    surfaceContractVersion: 1,
    renderedSurfaceReceiptSha256: await sha256Canonical(entries),
    visualReceipts: successes.flatMap((entry) => (
      entry.visualReceipt ? [entry.visualReceipt] : []
    )),
    theme,
    locale,
    v2Capabilities: detail.exportV2Capabilities ?? [],
  };
}

function projectionFailure(message: string, sectionId?: string): PrepareExportResult {
  return {
    ok: false,
    reason: "surface_projection_conflict",
    ...(sectionId ? { sectionId } : {}),
    message,
  };
}

function renderFailure(message: string, sectionId: string): PrepareExportResult {
  return {
    ok: false,
    reason: "surface_render_incomplete",
    sectionId,
    message,
  };
}
