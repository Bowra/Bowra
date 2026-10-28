import {
  chartThemeBaseForMode,
  resolveChartThemeForStyle,
  type ChartTheme,
} from "@/lib/chart-theme";
import {
  BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
  chartStylePreset,
} from "@/lib/chart/chart-style-registry";
import {
  frozenPresentationChecksumPayload,
  type ChartStyleExportThemeV1,
  type ChartStyleExportThemeV2,
  type EffectiveChartStyleV2,
  type EffectiveSurfacePaletteProjectionV2,
  type FrozenChartPresentationV2,
} from "@/lib/chart/chart-style-export";
import { resolveChartPresentation } from "@/lib/chart/chart-style-presentation";
import { normalizeChartForRenderingV1 } from "@/lib/chart/normalize-chart-for-rendering";
import type { ChartRenderContext } from "@/lib/chart/render-context";
import type { ChartSpec } from "@/lib/chart/types";
import { sha256Canonical } from "@/lib/canvas-visual-identity";
import { PRINT_PAPER, PRINT_THEME_SNAPSHOT } from "@/lib/print-theme";

export function chartStyleExportThemeFixture(): ChartStyleExportThemeV1 {
  return {
    kind: "chart-style",
    schemaVersion: 1,
    mode: "light",
    preset: JSON.parse(JSON.stringify(chartStylePreset("bowhead-editorial"))),
    presetChecksum: "sha256:c3c14a924bfed7aea2cd5b468f76ad03d7bf09e2b7d9eaa5d0a3c9a286b6f5b5",
    selectionSource: "product",
    accessibility: { contrast: "standard", colorVision: "standard" },
    teamPolicyEtag: `W/"chart-style-policy:${"a".repeat(64)}"`,
    resolverPolicyVersion: "1",
    contextRecipeVersion: "1",
  };
}

const SNAPSHOT_CHECKSUM = `sha256:${"b".repeat(64)}`;

function paletteProjection(
  mode: "light" | "dark",
): EffectiveSurfacePaletteProjectionV2 {
  const colors = chartStylePreset("bowhead-editorial").tokens[mode];
  return {
    colors: {
      categorical: [...colors.palette],
      sequential: { ...colors.sequential },
      diverging: { ...colors.diverging },
    },
    categoricalSeriesPalette: [...colors.palette],
    maxDistinctSeriesColors: 16,
    paletteOverflowRecipeVersion: "palette-overflow/2",
    finalPaletteQA: { hardInvariantsPass: true },
    adjustments: [],
  };
}

export function effectiveChartStyleV2Fixture(): EffectiveChartStyleV2 {
  const base = resolveChartThemeForStyle(
    "bowhead-editorial",
    { contrast: "standard", colorVision: "standard" },
    chartThemeBaseForMode("light"),
    "light",
  );
  const preset = chartStylePreset("bowhead-editorial");
  return {
    schemaVersion: 2,
    registryVersion: 0,
    builtInRegistryChecksum: BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
    style: {
      kind: "builtIn",
      id: "bowhead-editorial",
      revision: "2026.10.1",
      snapshotChecksum: SNAPSHOT_CHECKSUM,
      fallbackBuiltInPresetId: "bowhead-editorial",
      resolvedPresetSnapshot: {
        schemaVersion: 2,
        tokenSchemaVersion: 2,
        resolverPolicyVersion: "chart-style/2.0",
        capabilities: {},
        colors: {
          light: paletteProjection("light").colors,
          dark: paletteProjection("dark").colors,
        },
        kindTokens: base.chartStyleV2!.kindTokens,
      },
    },
    selection: {
      source: "product",
      savedStyleId: null,
      unavailableReason: null,
    },
    accessibility: {
      contrast: "standard",
      colorVision: "standard",
      source: { contrast: "product", colorVision: "product" },
    },
    modeColors: {
      light: paletteProjection("light"),
      dark: paletteProjection("dark"),
    },
    teamPolicy: {
      version: 2,
      defaultStyleId: "bowhead-editorial",
      enabledStyleIds: ["bowhead-editorial", "clean-blue", "analytical-neutral"],
      fallbackBuiltInPresetId: "bowhead-editorial",
      etag: `"chart-style-policy-v2:${"a".repeat(64)}"`,
    },
    resolutionAdjustments: [],
  };
}

export function chartStyleExportThemeV2Fixture(): ChartStyleExportThemeV2 {
  const effective = effectiveChartStyleV2Fixture();
  const {
    fallbackBuiltInPresetId: _fallback,
    ...style
  } = effective.style;
  return {
    kind: "chart-style",
    schemaVersion: 2,
    style,
    selectionSource: effective.selection.source,
    accessibility: effective.accessibility,
    teamPolicyEtag: effective.teamPolicy.etag,
    registryVersion: effective.registryVersion,
    builtInRegistryChecksum: effective.builtInRegistryChecksum,
    structuralThemeChecksum: PRINT_THEME_SNAPSHOT.checksum,
    modeColors: { light: effective.modeColors.light },
    resolverPolicyVersion: "chart-style/2.0",
    contextRecipeVersion: "2",
    renderShapeVersion: "chart-shape/1",
    rendererContractVersion: "2",
  };
}

export function frozenExportContextFixture(): ChartRenderContext {
  return {
    placement: "embed",
    bounds: { widthPx: 1456, heightPx: 772, devicePixelRatio: 1 },
    input: { hover: false, pointer: "none", keyboard: false },
    output: { medium: "raster", static: true },
    mode: "light",
    locale: "zh-CN",
    reducedMotion: true,
    forcedColors: false,
    exactDataAlternative: { kind: "none" },
  };
}

export async function frozenChartPresentationV2Fixture(
  spec: ChartSpec,
): Promise<FrozenChartPresentationV2> {
  const theme = chartStyleExportThemeV2Fixture();
  const projection = theme.modeColors.light;
  const base = chartThemeBaseForMode("light");
  const chartTheme: ChartTheme = {
    ...base,
    accessibility: {
      contrast: theme.accessibility.contrast,
      colorVision: theme.accessibility.colorVision,
    },
    palette: [...projection.categoricalSeriesPalette],
    primary: projection.categoricalSeriesPalette[0]!,
    sequential: projection.colors.sequential,
    diverging: projection.colors.diverging,
    ink: PRINT_THEME_SNAPSHOT.tokens.ink,
    inkSoft: PRINT_THEME_SNAPSHOT.tokens.ink3,
    surface: PRINT_PAPER,
    grid: PRINT_THEME_SNAPSHOT.tokens.line,
    gridSoft: PRINT_THEME_SNAPSHOT.tokens.line2,
    chartStyleV2: {
      presentationPolicyVersion: "chart-presentation/1",
      kindTokens: theme.style.resolvedPresetSnapshot.kindTokens,
      categoricalSeriesPalette: [...projection.categoricalSeriesPalette],
      maxDistinctSeriesColors: projection.maxDistinctSeriesColors,
      paletteOverflowRecipeVersion: projection.paletteOverflowRecipeVersion,
    },
  };
  const normalized = normalizeChartForRenderingV1(spec);
  const resolved = resolveChartPresentation(
    normalized.shape,
    chartTheme,
    frozenExportContextFixture(),
  );
  const frozen: FrozenChartPresentationV2 = {
    normalizedRenderShape: normalized.shape,
    normalizedRenderShapeChecksum: await sha256Canonical(normalized.shape),
    renderContext: frozenExportContextFixture(),
    requested: resolved.requested,
    effective: resolved.effective,
    adjustments: resolved.adjustments,
    finalPaletteQA: projection.finalPaletteQA,
    presentationChecksum: `sha256:${"0".repeat(64)}`,
    contextRecipeVersion: "2",
    renderShapeVersion: "chart-shape/1",
    barRadiusRecipeVersion: "bar-radius/1",
    paletteOverflowRecipeVersion: "palette-overflow/2",
  };
  frozen.presentationChecksum = await sha256Canonical(
    frozenPresentationChecksumPayload(frozen),
  );
  return frozen;
}
