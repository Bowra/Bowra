import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CANVAS_VISUAL_RENDERER_VERSION, computeCanvasVisualInputSha256 } from "@/lib/canvas-visual-identity";
import type { ReportSection } from "@/lib/types";
import {
  chartStyleExportThemeFixture,
  chartStyleExportThemeV2Fixture,
  frozenChartPresentationV2Fixture,
} from "@/__tests__/chart-style-export-fixture";
import { PRINT_THEME_SNAPSHOT } from "@/lib/print-theme";

// chart/table carriers need intl context (legacy trend_chart did not use useTranslations,
// so plain render previously sufficed).
import { renderIntl } from "@/__tests__/intl-render";
import { parseVisualExportPayload } from "./contract";
import CanvasExportVisualPage from "./page";

afterEach(() => {
  cleanup();
  delete window.__BOWHEAD_EXPORT_VISUAL__;
  vi.restoreAllMocks();
});

describe("Canvas export visual payload", () => {
  it("accepts the closed renderer contract", () => {
    expect(
      parseVisualExportPayload({
        blockKey: "sec_chart",
        sectionKind: "chart",
        payload: {
          spec: {
            kind: "bar",
            title: "Revenue",
            row_count: 1,
            rows: [{ region: "APAC", revenue: 120 }],
            columns: [],
          },
        },
        locale: "zh-CN",
        theme: chartStyleExportThemeFixture(),
        printTheme: PRINT_THEME_SNAPSHOT,
        visualInputSha256: `sha256:${"a".repeat(64)}`,
        rendererVersion: CANVAS_VISUAL_RENDERER_VERSION,
      }),
    ).toMatchObject({
      blockKey: "sec_chart",
      sectionKind: "chart",
      rendererVersion: CANVAS_VISUAL_RENDERER_VERSION,
    });
  });

  it.each(["zh-CN", "en-US", "de-DE", "fr-FR"])("accepts the %s interface locale", (locale) => {
    const parsed = parseVisualExportPayload({
      blockKey: "sec_chart",
      sectionKind: "chart",
      payload: {},
      locale,
      theme: chartStyleExportThemeFixture(),
      printTheme: PRINT_THEME_SNAPSHOT,
      visualInputSha256: `sha256:${"a".repeat(64)}`,
      rendererVersion: CANVAS_VISUAL_RENDERER_VERSION,
    });
    expect(parsed?.locale).toBe(locale);
  });

  it.each([
    { rendererVersion: "0" },
    { sectionKind: "image" },
    { visualInputSha256: "sha256:forged" },
    { payload: [] },
    { locale: "ja-JP" },
  ])("rejects open or forged fields: %o", (override) => {
    expect(
      parseVisualExportPayload({
        blockKey: "sec_chart",
        sectionKind: "chart",
        payload: {},
        locale: "zh-CN",
        theme: chartStyleExportThemeFixture(),
        printTheme: PRINT_THEME_SNAPSHOT,
        visualInputSha256: `sha256:${"a".repeat(64)}`,
        rendererVersion: CANVAS_VISUAL_RENDERER_VERSION,
        ...override,
      }),
    ).toBeNull();
  });

  it("rejects a modified preset package or release checksum", () => {
    const modifiedPackage = chartStyleExportThemeFixture();
    modifiedPackage.preset.tokens.light.palette[0] = "#000000";
    const modifiedChecksum = chartStyleExportThemeFixture();
    modifiedChecksum.presetChecksum = `sha256:${"f".repeat(64)}`;
    const payload = {
      blockKey: "sec_chart",
      sectionKind: "chart",
      payload: {},
      locale: "zh-CN",
      visualInputSha256: `sha256:${"a".repeat(64)}`,
      rendererVersion: CANVAS_VISUAL_RENDERER_VERSION,
    };

    expect(parseVisualExportPayload({ ...payload, printTheme: PRINT_THEME_SNAPSHOT, theme: modifiedPackage })).toBeNull();
    expect(parseVisualExportPayload({ ...payload, printTheme: PRINT_THEME_SNAPSHOT, theme: modifiedChecksum })).toBeNull();
  });

  it("rejects V2 themes not bound to print chrome and mutable export contexts", async () => {
    const spec = {
      kind: "table" as const,
      title: "Frozen",
      row_count: 0,
      columns: [],
      rows: [],
    };
    const frozen = await frozenChartPresentationV2Fixture(spec);
    const base = {
      blockKey: "sec_chart_v2",
      sectionKind: "chart",
      payload: { spec },
      locale: "zh-CN",
      theme: chartStyleExportThemeV2Fixture(),
      printTheme: PRINT_THEME_SNAPSHOT,
      visualInputSha256: `sha256:${"a".repeat(64)}`,
      rendererVersion: CANVAS_VISUAL_RENDERER_VERSION,
      frozenPresentation: frozen,
      exportSurfaceContractVersion: "canvas-export-visual/2",
    };
    const unbound = structuredClone(base);
    unbound.theme.structuralThemeChecksum = `sha256:${"f".repeat(64)}`;
    const mutableContext = structuredClone(base);
    mutableContext.frozenPresentation.renderContext.bounds.widthPx = 720;

    expect(parseVisualExportPayload(unbound)).toBeNull();
    expect(parseVisualExportPayload(mutableContext)).toBeNull();
  });

  it("computes its receipt from the rendered payload instead of echoing input", async () => {
    // Carrier is now chart/table (trend_chart section kind retired on 2026-09-05). This checks
    // receipt SHA computed from the rendered product rather than echoing input, independent of kind; use table because
    // it avoids Chart.js (jsdom lacks canvas getContext), and unlike spec_block/graph does not
    // await a dynamic dagre import—neither of those paths reaches ready in jsdom.
    const spec = {
      kind: "table",
      title: "各省退款率",
      columns: [
        { key: "province", header: "省份", type: "categorical", role: "x" },
        { key: "refund", header: "退款率", type: "numeric", role: "y" },
      ],
      rows: [
        { province: "天津", refund: 19.74 },
        { province: "河南", refund: 12.57 },
      ],
    };
    const section = {
      id: "sec_chart",
      kind: "chart",
      spec,
    } as unknown as ReportSection;
    const expected = await computeCanvasVisualInputSha256({
      section,
      locale: "zh-CN",
      theme: chartStyleExportThemeFixture(),
      printTheme: PRINT_THEME_SNAPSHOT,
    });
    window.__BOWHEAD_EXPORT_VISUAL__ = {
      blockKey: section.id,
      sectionKind: section.kind,
      payload: {
        spec,
      },
      locale: "zh-CN",
      theme: chartStyleExportThemeFixture(),
      printTheme: PRINT_THEME_SNAPSHOT,
      visualInputSha256: `sha256:${"f".repeat(64)}`,
      rendererVersion: CANVAS_VISUAL_RENDERER_VERSION,
    };

    renderIntl(<CanvasExportVisualPage />);

    await waitFor(() => {
      expect(
        screen
          .getByTestId("canvas-export-visual-root")
          .getAttribute("data-export-visual-input-sha256"),
      ).toBe(expected);
    });
    expect(expected).not.toBe(`sha256:${"f".repeat(64)}`);
  });

  it("renders V2 from the frozen presentation without requesting current theme state", async () => {
    const spec = {
      kind: "table" as const,
      title: "各省退款率",
      row_count: 2,
      columns: [
        { key: "province", header: "省份", type: "categorical" as const, role: "x" as const },
        { key: "refund", header: "退款率", type: "numeric" as const, role: "y" as const },
      ],
      rows: [
        { province: "天津", refund: 19.74 },
        { province: "河南", refund: 12.57 },
      ],
    };
    const section = {
      id: "sec_chart_v2",
      kind: "chart",
      spec,
    } as ReportSection;
    const theme = chartStyleExportThemeV2Fixture();
    const frozen = await frozenChartPresentationV2Fixture(spec);
    const expected = await computeCanvasVisualInputSha256({
      section,
      locale: "zh-CN",
      theme,
      printTheme: PRINT_THEME_SNAPSHOT,
      frozenChartPresentation: frozen,
      exportSurfaceContractVersion: "canvas-export-visual/2",
    });
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    window.__BOWHEAD_EXPORT_VISUAL__ = {
      blockKey: section.id,
      sectionKind: section.kind,
      payload: { spec },
      locale: "zh-CN",
      theme,
      printTheme: PRINT_THEME_SNAPSHOT,
      visualInputSha256: expected,
      rendererVersion: CANVAS_VISUAL_RENDERER_VERSION,
      frozenPresentation: frozen,
      exportSurfaceContractVersion: "canvas-export-visual/2",
    };

    renderIntl(<CanvasExportVisualPage />);

    await waitFor(() => {
      const root = screen.getByTestId("canvas-export-visual-root");
      expect(root.getAttribute("data-export-render-state")).toBe("ready");
      expect(root.getAttribute("data-export-visual-input-sha256")).toBe(expected);
      expect(root.getAttribute("data-export-surface-contract-version"))
        .toBe("canvas-export-visual/2");
      expect(root.getAttribute("data-export-presentation-checksum"))
        .toBe(frozen.presentationChecksum);
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("fails closed when the raw ChartSpec no longer matches the frozen shape", async () => {
    const spec = {
      kind: "table" as const,
      title: "Original",
      row_count: 1,
      columns: [
        { key: "name", header: "Name", type: "categorical" as const },
        { key: "value", header: "Value", type: "numeric" as const },
      ],
      rows: [{ name: "A", value: 1 }],
    };
    const frozen = await frozenChartPresentationV2Fixture(spec);
    window.__BOWHEAD_EXPORT_VISUAL__ = {
      blockKey: "sec_chart_v2",
      sectionKind: "chart",
      payload: { spec: { ...spec, rows: [...spec.rows, { name: "B", value: 2 }] } },
      locale: "zh-CN",
      theme: chartStyleExportThemeV2Fixture(),
      printTheme: PRINT_THEME_SNAPSHOT,
      visualInputSha256: `sha256:${"f".repeat(64)}`,
      rendererVersion: CANVAS_VISUAL_RENDERER_VERSION,
      frozenPresentation: frozen,
      exportSurfaceContractVersion: "canvas-export-visual/2",
    };

    renderIntl(<CanvasExportVisualPage />);

    await waitFor(() => {
      const root = screen.getByTestId("canvas-export-visual-root");
      expect(root.getAttribute("data-export-render-state")).toBe("error");
      expect(root.getAttribute("data-export-error-code"))
        .toBe("visual_render_shape_mismatch");
    });
  });
});
