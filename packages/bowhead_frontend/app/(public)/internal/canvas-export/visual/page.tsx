"use client";

import { Chart } from "chart.js";
import { useEffect, useRef, useState } from "react";

import { ChartBlock } from "@/components/canvas/sections/chart-block";
import { CharIconTextOnly } from "@/components/char-icon";
import { SpecBlock } from "@/components/canvas/sections/spec-block";
import {
  chartThemeBaseForMode,
  ChartThemePreviewProvider,
  colorOnlyCapacityOf,
  resolveChartThemeForPresetPackage,
  separatorGrid,
  type ChartTheme,
} from "@/lib/chart-theme";
import {
  frozenPresentationChecksumPayload,
  resolvedPresentationFromFrozen,
  type ChartStyleExportThemeV2,
} from "@/lib/chart/chart-style-export";
import { normalizeChartForRenderingV1 } from "@/lib/chart/normalize-chart-for-rendering";
import type { ChartStyleThemeV2 } from "@/lib/chart/chart-style-presentation";
import type { ChartSpec } from "@/lib/chart/types";
import {
  computeCanvasVisualInputSha256,
  sha256Canonical,
} from "@/lib/canvas-visual-identity";
import { PRINT_PAPER, printThemeChecksumMatches } from "@/lib/print-theme";
import type { ReportSection } from "@/lib/types";

import {
  parseVisualExportPayload,
  type VisualExportPayload,
} from "./contract";

declare global {
  interface Window {
    __BOWHEAD_EXPORT_VISUAL__?: unknown;
  }
}

// W2 golden smoke evidence (export-runtime-hardening): Chart.js defaults to ~1s entry animation,
// page ready signal does not await animation → headless capture lands midframe; two renders of same spec yield
// RMSE 4.9%. Export is static-render context: disable global animation at module level here, first frame is final,
// establishing pixel determinism (renderer_version bumped alongside to record behavioral change).
Chart.defaults.animation = false;

// Capture-surface geometry is **contract**, not layout preference: `export_browser_visual._validate_receipt`
// checks each boundingBox is 1600×900, otherwise directly `visual_route_dimensions_invalid`.
// Content box (minus padding) is sole true component space allocation; visual sections use it to draw at
// print size, rather than small chart leaving most frame empty.
const EXPORT_FRAME_W = 1600;
const EXPORT_FRAME_H = 900;
const EXPORT_PAD_X = 72;
const EXPORT_PAD_Y = 64;
const EXPORT_CONTENT_W = EXPORT_FRAME_W - EXPORT_PAD_X * 2; // 1456
const EXPORT_CONTENT_H = EXPORT_FRAME_H - EXPORT_PAD_Y * 2; // 772

export default function CanvasExportVisualPage() {
  const rootRef = useRef<HTMLDivElement>(null);
  const [resolved, setResolved] = useState<{
    payload: VisualExportPayload;
    visualInputSha256: string;
  } | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [errorCode, setErrorCode] = useState("");
  const payload = resolved?.payload ?? null;

  useEffect(() => {
    const candidate = parseVisualExportPayload(window.__BOWHEAD_EXPORT_VISUAL__);
    if (!candidate) {
      setState("error");
      return;
    }
    let canceled = false;
    // Apply **export-request locale** to document.
    //
    // Required because `getCurrentLocale()` reads `<html lang>`, while gateway serves this page
    // by selecting locale bundle for its own static route (headless browser’s
    // Accept-Language may determine bundle), unrelated to `candidate.locale`.
    // Thus all locale-dependent chart formatting, especially `abbrTick`/`abbrChinese`,
    // (zh: `20亿` / en: `2B`) follows **incidental render locale**, not export-request locale.
    //
    // Beyond appearance: `visualInputSha256` **includes locale in identity** (see
    // `computeCanvasVisualInputSha256({ locale: candidate.locale })`, a few lines below);
    // contract: same identity ⇒ same pixels. Ignoring locale lets same hash identify two
    // different charts; reusing cached receipt yields numbers in another language.
    if (typeof document !== "undefined") {
      document.documentElement.lang = candidate.locale;
    }
    const section = {
      id: candidate.blockKey,
      kind: candidate.sectionKind,
      ...candidate.payload,
    } as ReportSection;
    void validateVisualIdentity(candidate, section).then((result) => {
      if (canceled) return;
      if (!result.visualInputSha256) {
        setErrorCode(result.errorCode);
        setState("error");
        return;
      }
      setResolved({
        payload: candidate,
        visualInputSha256: result.visualInputSha256,
      });
    }).catch(() => {
      if (!canceled) {
        setErrorCode("visual_identity_invalid");
        setState("error");
      }
    });
    return () => {
      canceled = true;
    };
  }, []);

  useEffect(() => {
    if (!payload || !rootRef.current) return;
    let canceled = false;
    let attempts = 0;
    const check = () => {
      if (canceled || !rootRef.current) return;
      attempts += 1;
      const root = rootRef.current;
      const failed = root.querySelector(
        ".v2-chart-fallback-wrap, .rc-spec-fallback, [data-export-render-state='error']",
      );
      if (failed) {
        setState("error");
        return;
      }
      const loading = root.querySelector(
        ".rc-spec-loading, [aria-busy='true'], [data-export-render-state='loading']",
      );
      const expectedSurfaceReady =
        payload.sectionKind !== "chart"
        || root.querySelector("canvas, .v2-chart-table, .v2-chart-kpi");
      if (!loading && expectedSurfaceReady && attempts >= 2) {
        setState("ready");
        return;
      }
      if (attempts >= 300) {
        setState("error");
        return;
      }
      requestAnimationFrame(check);
    };
    requestAnimationFrame(check);
    return () => {
      canceled = true;
    };
  }, [payload]);

  return (
    <main
      style={{
        width: EXPORT_FRAME_W,
        height: EXPORT_FRAME_H,
        margin: 0,
        overflow: "hidden",
        background: payload ? PRINT_PAPER : "transparent",
      }}
    >
      <div
        ref={rootRef}
        data-testid="canvas-export-visual-root"
        data-export-render-state={state}
        data-export-visual-input-sha256={resolved?.visualInputSha256 ?? ""}
        data-export-renderer-version={payload?.rendererVersion ?? ""}
        data-export-block-key={payload?.blockKey ?? ""}
        data-export-surface-contract-version={payload?.exportSurfaceContractVersion ?? ""}
        data-export-presentation-checksum={payload?.frozenPresentation?.presentationChecksum ?? ""}
        data-export-error-code={errorCode}
        style={{
          boxSizing: "border-box",
          width: EXPORT_FRAME_W,
          height: EXPORT_FRAME_H,
          padding: `${EXPORT_PAD_Y}px ${EXPORT_PAD_X}px`,
          overflow: "hidden",
          color: payload?.printTheme.tokens.ink ?? "inherit",
          background: payload ? PRINT_PAPER : "transparent",
          borderColor: payload?.printTheme.tokens.line ?? "transparent",
          "--c-bg": payload ? PRINT_PAPER : undefined,
          "--c-surface": payload ? PRINT_PAPER : undefined,
          "--c-ink": payload?.printTheme.tokens.ink,
          "--c-surface-2": payload?.printTheme.tokens.surface2,
          "--c-surface-3": payload?.printTheme.tokens.surface3,
          "--c-ink-2": payload?.printTheme.tokens.ink2,
          "--c-ink-3": payload?.printTheme.tokens.ink3,
          "--c-ink-4": payload?.printTheme.tokens.ink4,
          "--c-line": payload?.printTheme.tokens.line,
          "--c-line-2": payload?.printTheme.tokens.line2,
          "--c-accent": payload?.printTheme.tokens.accent,
          "--c-accent-hover": payload?.printTheme.tokens.accentHover,
          "--c-accent-ink": payload?.printTheme.tokens.accentInk,
          "--c-accent-soft": payload?.printTheme.tokens.accentSoft,
          "--c-accent-tint": payload?.printTheme.tokens.accentTint,
          "--c-warn": payload?.printTheme.tokens.warning,
          "--c-warn-soft": payload?.printTheme.tokens.warningSoft,
          // Let chart fill capture surface (with two globals.css rules for `[data-testid="canvas-export-visual-root"]`
          // below). Nonchart sections (spec_block, etc.) receive no flex value,
          // remain `0 1 auto` = content height, appearance unchanged.
          display: "flex",
          flexDirection: "column",
        } as React.CSSProperties}
      >
        {payload ? (
          <CharIconTextOnly>
            <VisualSurface payload={payload} />
          </CharIconTextOnly>
        ) : null}
      </div>
    </main>
  );
}

function VisualSurface({ payload }: { payload: VisualExportPayload }) {
  if (payload.sectionKind === "chart") {
    const spec = payload.payload.spec;
    if (!isRecord(spec)) return null;
    const frozen = payload.frozenPresentation;
    if (payload.theme.schemaVersion === 2 && !frozen) return null;
    return (
      <ChartThemePreviewProvider
        theme={payload.theme.schemaVersion === 2
          ? exportChartThemeV2(payload.theme, payload.printTheme)
          : resolveChartThemeForPresetPackage(
              payload.theme.preset,
              payload.theme.accessibility,
              chartThemeBaseForMode("light"),
              "light",
            )}
      >
        <ChartBlock
          sectionId={payload.blockKey}
          spec={spec as unknown as ChartSpec}
          surface="export"
          hiddenSeries={hiddenSeriesOf(payload.payload)}
          renderMode="light"
          frozenPresentation={frozen
            ? resolvedPresentationFromFrozen(frozen)
            : undefined}
          frozenRenderContext={frozen?.renderContext}
        />
      </ChartThemePreviewProvider>
    );
  }
  const section = {
    id: payload.blockKey,
    kind: payload.sectionKind,
    ...payload.payload,
  } as ReportSection;
  if (section.kind === "spec_block") {
    return <SpecBlock data={section} />;
  }
  return null;
}

async function validateVisualIdentity(
  candidate: VisualExportPayload,
  section: ReportSection,
): Promise<{ visualInputSha256: string | null; errorCode: string }> {
  // The visual identity hash is keyed on the print-theme checksum only, so the tokens about to
  // be painted must still match that digest (team brands are not a fixed constant). This is a
  // consistency check, not tamper protection: the digest is unkeyed.
  if (!await printThemeChecksumMatches(candidate.printTheme)) {
    return { visualInputSha256: null, errorCode: "visual_print_theme_invalid" };
  }
  if (candidate.theme.schemaVersion === 2 && candidate.sectionKind === "chart") {
    const frozen = candidate.frozenPresentation;
    const spec = candidate.payload.spec;
    if (!frozen || !isRecord(spec)) {
      return { visualInputSha256: null, errorCode: "visual_frozen_presentation_invalid" };
    }
    const normalized = normalizeChartForRenderingV1(spec as unknown as ChartSpec);
    const shapeChecksum = await sha256Canonical(normalized.shape);
    if (shapeChecksum !== frozen.normalizedRenderShapeChecksum) {
      return { visualInputSha256: null, errorCode: "visual_render_shape_mismatch" };
    }
    const presentationChecksum = await sha256Canonical(
      frozenPresentationChecksumPayload(frozen),
    );
    if (presentationChecksum !== frozen.presentationChecksum) {
      return { visualInputSha256: null, errorCode: "visual_frozen_presentation_invalid" };
    }
  }
  const visualInputSha256 = await computeCanvasVisualInputSha256({
    section,
    locale: candidate.locale,
    theme: candidate.theme,
    printTheme: candidate.printTheme,
    rendererVersion: candidate.rendererVersion,
    frozenChartPresentation: candidate.frozenPresentation,
    exportSurfaceContractVersion: candidate.exportSurfaceContractVersion,
  });
  if (
    candidate.theme.schemaVersion === 2
    && visualInputSha256 !== candidate.visualInputSha256
  ) {
    return { visualInputSha256: null, errorCode: "visual_export_identity_mismatch" };
  }
  return { visualInputSha256, errorCode: "" };
}

function exportChartThemeV2(
  theme: ChartStyleExportThemeV2,
  printTheme: VisualExportPayload["printTheme"],
): ChartTheme {
  const base = chartThemeBaseForMode("light");
  const projection = theme.modeColors.light;
  const colors = projection.colors;
  return {
    ...base,
    accessibility: {
      contrast: theme.accessibility.contrast,
      colorVision: theme.accessibility.colorVision,
    },
    palette: [...projection.categoricalSeriesPalette],
    primary: projection.categoricalSeriesPalette[0]!,
    sequential: colors.sequential,
    diverging: colors.diverging,
    ink: printTheme.tokens.ink,
    ink2: printTheme.tokens.ink2,
    inkSoft: printTheme.tokens.ink3,
    surface: PRINT_PAPER,
    // Gridlines are separator hairlines derived from the print ink (Chart Embed Spec D2), not the line tokens.
    ...separatorGrid(printTheme.tokens.ink, "light"),
    chartStyleV2: {
      presentationPolicyVersion: "chart-presentation/1",
      kindTokens: (
        theme.style.resolvedPresetSnapshot.kindTokens
      ) as ChartStyleThemeV2["kindTokens"],
      categoricalSeriesPalette: [...projection.categoricalSeriesPalette],
      maxDistinctSeriesColors: projection.maxDistinctSeriesColors,
      paletteOverflowRecipeVersion: projection.paletteOverflowRecipeVersion,
      ...colorOnlyCapacityOf(projection),
    },
  };
}

/** The exporting user's legend-hidden series, carried in the chart's visual payload (part of its visual identity). */
function hiddenSeriesOf(payload: Record<string, unknown>): string[] | undefined {
  const hidden = payload.hidden_series;
  return Array.isArray(hidden) && hidden.every((key) => typeof key === "string") ? hidden : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
