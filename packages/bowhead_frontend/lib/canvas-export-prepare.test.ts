import { beforeAll, describe, expect, it } from "vitest";

import type { CanvasDTO, CanvasSurfaceEntryDTO } from "./api";
import { validateAndBuildRenderedSurfaceReceipt } from "./canvas-export-prepare";
import { computeCanvasVisualInputSha256 } from "./canvas-visual-identity";
import type { ReportSection } from "./types";
import type { EffectiveChartStyle } from "./theme-config";
import { effectiveChartStyleV2Fixture } from "@/__tests__/chart-style-export-fixture";
import { buildChartStyleExportFreshnessV2 } from "./chart/chart-style-export";
import { computeCanvasScreenVisualInputSha256V2 } from "./canvas-visual-identity";

const chartStyle: EffectiveChartStyle = {
  schemaVersion: 1,
  preset: { id: "bowhead-editorial", revision: "2026.09.1", checksum: `sha256:${"d".repeat(64)}` },
  selection: { source: "product", savedPresetId: null, unavailableByTeamPolicy: false },
  accessibility: {
    contrast: "standard",
    colorVision: "standard",
    source: { contrast: "product", colorVision: "product" },
  },
  teamPolicy: {
    version: 1,
    defaultPresetId: "bowhead-editorial",
    enabledPresetIds: ["bowhead-editorial", "clean-blue", "analytical-neutral"],
    etag: `W/"chart-style-policy:${"a".repeat(64)}"`,
  },
};

const sections: ReportSection[] = [
  { id: "sec_title", kind: "h1", text: "Q2 review", rev: 3 },
  {
    id: "sec_chart",
    kind: "chart",
    rev: 5,
    spec: {
      kind: "bar",
      title: "GMV",
      row_count: 1,
      columns: [{ key: "gmv", header: "GMV", type: "numeric" }],
      rows: [{ gmv: 42 }],
    },
  },
  {
    id: "sec_code",
    kind: "code_block",
    call_id: "tl_sql_42",
    lang: "sql",
    code: "select 42",
    rev: 2,
  },
];

const entries: CanvasSurfaceEntryDTO[] = [
  {
    sectionId: "sec_title",
    sectionRev: 3,
    order: 0,
    placement: "report_body",
    parentSectionId: null,
    effectiveKind: "h1",
    parityRequirement: "semantic_content",
  },
  {
    sectionId: "sec_chart",
    sectionRev: 5,
    order: 1,
    placement: "report_body",
    parentSectionId: null,
    effectiveKind: "chart",
    parityRequirement: "exact_visual",
    // renderer_version bumps no longer fail: calculate with the canonical function (filled in beforeAll)
    visualInputSha256: "PENDING",
  },
  {
    sectionId: "sec_code",
    sectionRev: 2,
    order: 2,
    placement: "code_tab",
    parentSectionId: null,
    effectiveKind: "code_block",
    parityRequirement: "semantic_content",
  },
];

function detail(overrides: Partial<CanvasDTO> = {}): CanvasDTO {
  return {
    canvas: {
      id: "cv_1",
      sessionId: "s_1",
      workspaceId: "ws_1",
      title: "Q2 review",
      status: "active",
      createdAt: "2026-07-16T00:00:00Z",
      updatedAt: "2026-07-16T00:00:00Z",
    },
    sections,
    projectionToken: "sha256:content",
    surfaceToken: "sha256:surface",
    surfaceProjection: {
      contractVersion: 1,
      contentProjectionToken: "sha256:content",
      surfaceToken: "sha256:surface",
      entries,
      exclusions: [],
    },
    ...overrides,
  };
}

beforeAll(async () => {
  entries[1].visualInputSha256 = (await computeCanvasVisualInputSha256({
    section: sections[1],
    locale: "zh-CN",
    theme: (await import("./chart/chart-style-export")).buildChartStyleExportTheme(chartStyle),
  }))!;
});

describe("validateAndBuildRenderedSurfaceReceipt", () => {
  it("submits only V2 freshness and a screen-scoped visual receipt", async () => {
    const chartStyleV2 = effectiveChartStyleV2Fixture();
    const screenHash = await computeCanvasScreenVisualInputSha256V2({
      section: sections[1],
      locale: "zh-CN",
      surfaceToken: "sha256:surface",
      effectiveChartStyle: chartStyleV2,
    });
    const result = await validateAndBuildRenderedSurfaceReceipt({
      detail: detail(),
      localSections: sections,
      locale: "zh-CN",
      chartStyle: undefined,
      chartStyleV2,
      revisionOf: (sectionId) => (
        sections.find((section) => section.id === sectionId)?.rev
      ),
      renderStateOf: (entry) => (
        entry.placement === "code_tab" ? "registered" : "rendered"
      ),
    });

    expect(result).toMatchObject({
      ok: true,
      theme: buildChartStyleExportFreshnessV2(chartStyleV2),
      visualReceipts: [{
        section_id: "sec_chart",
        section_rev: 5,
        surface_token: "sha256:surface",
        screen_visual_input_sha256: screenHash,
        render_disposition: "rendered",
      }],
    });
    if (!result.ok) throw new Error("expected V2 export preparation");
    expect(result.theme).not.toHaveProperty("style");
    expect(result.visualReceipts[0]).not.toHaveProperty("visual_input_sha256");
  });

  it("returns a stable receipt for matching persisted and rendered state", async () => {
    const result = await validateAndBuildRenderedSurfaceReceipt({
      detail: detail(),
      localSections: sections,
      locale: "zh-CN",
      chartStyle,
      revisionOf: (sectionId) => sections.find((section) => section.id === sectionId)?.rev,
      renderStateOf: (entry) => (
        entry.placement === "code_tab" ? "registered" : "rendered"
      ),
    });

    expect(result).toEqual({
      ok: true,
      mode: "v2",
      projectionToken: "sha256:content",
      surfaceToken: "sha256:surface",
      surfaceContractVersion: 1,
      renderedSurfaceReceiptSha256: expect.stringMatching(/^sha256:[a-f0-9]{64}$/),
      visualReceipts: [
        {
          section_id: "sec_chart",
          section_rev: 5,
          visual_input_sha256: entries[1].visualInputSha256,
          render_disposition: "rendered",
        },
      ],
      theme: expect.objectContaining({
        kind: "chart-style",
        preset: expect.objectContaining({ id: "bowhead-editorial" }),
      }),
      locale: "zh-CN",
      v2Capabilities: [],
    });
    await expect(validateAndBuildRenderedSurfaceReceipt({
      detail: detail(),
      localSections: [...sections].reverse(),
      locale: "zh-CN",
      chartStyle,
      revisionOf: (sectionId) => sections.find((section) => section.id === sectionId)?.rev,
      renderStateOf: (entry) => (
        entry.placement === "code_tab" ? "registered" : "rendered"
      ),
    })).resolves.toEqual(result);
  });

  it("rejects a local revision that differs from the refreshed plan", async () => {
    const result = await validateAndBuildRenderedSurfaceReceipt({
      detail: detail(),
      localSections: sections,
      locale: "zh-CN",
      chartStyle,
      revisionOf: (sectionId) => (sectionId === "sec_chart" ? 4 : 3),
      renderStateOf: () => "rendered",
    });
    expect(result).toMatchObject({
      ok: false,
      reason: "surface_projection_conflict",
      sectionId: "sec_chart",
    });
  });

  it("rejects missing render markers and runtime fallbacks", async () => {
    const missing = await validateAndBuildRenderedSurfaceReceipt({
      detail: detail(),
      localSections: sections,
      locale: "zh-CN",
      chartStyle,
      revisionOf: (sectionId) => sections.find((section) => section.id === sectionId)?.rev,
      renderStateOf: (entry) => (entry.sectionId === "sec_chart" ? "missing" : "rendered"),
    });
    expect(missing).toMatchObject({
      ok: false,
      reason: "surface_render_incomplete",
      sectionId: "sec_chart",
    });

    const fallback = await validateAndBuildRenderedSurfaceReceipt({
      detail: detail(),
      localSections: sections,
      locale: "zh-CN",
      chartStyle,
      revisionOf: (sectionId) => sections.find((section) => section.id === sectionId)?.rev,
      renderStateOf: (entry) => (
        entry.sectionId === "sec_chart" ? "runtime_fallback" : "rendered"
      ),
    });
    expect(fallback).toMatchObject({
      ok: false,
      reason: "surface_render_incomplete",
      sectionId: "sec_chart",
    });
  });

  it("rejects inconsistent projection tokens before hashing", async () => {
    const result = await validateAndBuildRenderedSurfaceReceipt({
      detail: detail({ surfaceToken: "sha256:other" }),
      localSections: sections,
      locale: "zh-CN",
      chartStyle,
      revisionOf: () => 1,
      renderStateOf: () => "rendered",
    });
    expect(result).toMatchObject({ ok: false, reason: "surface_projection_conflict" });
  });

  it("computes the per-user style hash without trusting the style-neutral server plan", async () => {
    const mismatchedEntries = entries.map((entry) => (
      entry.sectionId === "sec_chart"
        ? { ...entry, visualInputSha256: `sha256:${"f".repeat(64)}` }
        : entry
    ));
    const result = await validateAndBuildRenderedSurfaceReceipt({
      detail: detail({
        surfaceProjection: {
          contractVersion: 1,
          contentProjectionToken: "sha256:content",
          surfaceToken: "sha256:surface",
          entries: mismatchedEntries,
          exclusions: [],
        },
      }),
      localSections: sections,
      locale: "zh-CN",
      chartStyle,
      revisionOf: (sectionId) => (
        sections.find((section) => section.id === sectionId)?.rev
      ),
      renderStateOf: () => "rendered",
    });
    expect(result).toMatchObject({ ok: true, mode: "v2" });
  });
});
