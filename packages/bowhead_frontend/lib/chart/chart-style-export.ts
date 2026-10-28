import {
  BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
  CHART_STYLE_PRESET_CHECKSUMS,
  chartStylePreset,
  type ChartStylePresetPackage,
} from "./chart-style-registry";
import type {
  ChartColorVision,
  ChartContrast,
  EffectiveChartStyle,
} from "../theme-config";
import type { NormalizedChartRenderShapeV1 } from "./normalize-chart-for-rendering";
import type {
  ChartPresentationTokensV2,
  ChartStyleAdjustmentV2,
  ResolvedChartPresentationV2,
} from "./chart-style-presentation";
import type { ChartRenderContext } from "./render-context";

export interface ChartStyleExportThemeV1 {
  kind: "chart-style";
  schemaVersion: 1;
  mode: "light";
  preset: ChartStylePresetPackage;
  presetChecksum: string;
  selectionSource: "product" | "team" | "user";
  accessibility: {
    contrast: ChartContrast;
    colorVision: ChartColorVision;
  };
  teamPolicyEtag: string;
  resolverPolicyVersion: "1";
  contextRecipeVersion: "1";
}

export interface EffectiveSurfacePaletteProjectionV2 {
  colors: {
    categorical: string[];
    sequential: { from: string; to: string };
    diverging: { neg: string; mid: string; pos: string };
  };
  categoricalSeriesPalette: string[];
  maxDistinctSeriesColors: 16;
  paletteOverflowRecipeVersion: "palette-overflow/2";
  finalPaletteQA: Record<string, unknown> & { hardInvariantsPass: true };
  adjustments: unknown[];
}

export interface ResolvedChartStyleSnapshotV2 {
  schemaVersion: 2;
  tokenSchemaVersion: 2;
  resolverPolicyVersion: "chart-style/2.0";
  capabilities: Record<string, unknown>;
  colors: Record<"light" | "dark", EffectiveSurfacePaletteProjectionV2["colors"]>;
  kindTokens: Record<string, unknown>;
}

export interface ChartStyleExportThemeV2 {
  kind: "chart-style";
  schemaVersion: 2;
  style: {
    kind: "builtIn" | "teamCustom";
    id: string;
    revision: string;
    snapshotChecksum: string;
    basePreset?: { id: string; revision: string; checksum: string };
    resolvedPresetSnapshot: ResolvedChartStyleSnapshotV2;
  };
  selectionSource: "product" | "team" | "user";
  accessibility: {
    contrast: ChartContrast;
    colorVision: ChartColorVision;
    source: {
      contrast: "product" | "user";
      colorVision: "product" | "user";
    };
  };
  teamPolicyEtag: string;
  registryVersion: number;
  builtInRegistryChecksum: string;
  structuralThemeChecksum: string;
  modeColors: { light: EffectiveSurfacePaletteProjectionV2 };
  resolverPolicyVersion: "chart-style/2.0";
  contextRecipeVersion: "2";
  renderShapeVersion: "chart-shape/1";
  rendererContractVersion: "2";
}

export type ChartStyleExportTheme =
  | ChartStyleExportThemeV1
  | ChartStyleExportThemeV2;

export interface ChartStyleExportFreshnessV2 {
  kind: "chart-style-freshness";
  schemaVersion: 2;
  styleId: string;
  revision: string;
  snapshotChecksum: string;
  teamPolicyEtag: string;
  registryVersion: number;
  builtInRegistryChecksum: string;
}

export interface EffectiveChartStyleV2 {
  schemaVersion: 2;
  registryVersion: number;
  builtInRegistryChecksum: string;
  style: ChartStyleExportThemeV2["style"] & { fallbackBuiltInPresetId: string };
  selection: {
    source: "product" | "team" | "user";
    savedStyleId: string | null;
    unavailableReason: string | null;
  };
  accessibility: ChartStyleExportThemeV2["accessibility"];
  modeColors: {
    light: EffectiveSurfacePaletteProjectionV2;
    dark: EffectiveSurfacePaletteProjectionV2;
  };
  teamPolicy: {
    version: 2;
    defaultStyleId: string;
    enabledStyleIds: string[];
    fallbackBuiltInPresetId: string;
    etag: string;
  };
  resolutionAdjustments: unknown[];
}

/** Validate the authenticated/cacheable V2 read model before renderer use. */
export function parseEffectiveChartStyleV2(
  value: unknown,
): EffectiveChartStyleV2 | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    "schemaVersion", "registryVersion", "builtInRegistryChecksum", "style",
    "selection", "accessibility", "modeColors", "teamPolicy",
    "resolutionAdjustments",
  ])) return null;
  const style = value.style;
  const selection = value.selection;
  const accessibility = value.accessibility;
  const modeColors = value.modeColors;
  const teamPolicy = value.teamPolicy;
  if (
    value.schemaVersion !== 2
    || !Number.isInteger(value.registryVersion)
    || Number(value.registryVersion) < 0
    || value.builtInRegistryChecksum !== BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM
    || !isRecord(style)
    || ![
      ["kind", "id", "revision", "snapshotChecksum", "fallbackBuiltInPresetId", "resolvedPresetSnapshot"],
      ["kind", "id", "revision", "snapshotChecksum", "basePreset", "fallbackBuiltInPresetId", "resolvedPresetSnapshot"],
    ].some((keys) => hasExactKeys(style, keys))
    || !["builtIn", "teamCustom"].includes(String(style.kind))
    || typeof style.id !== "string"
    || !style.id
    || typeof style.revision !== "string"
    || !style.revision
    || !isSha256(style.snapshotChecksum)
    || typeof style.fallbackBuiltInPresetId !== "string"
    || !isResolvedSnapshot(style.resolvedPresetSnapshot)
    || !isRecord(selection)
    || !hasExactKeys(selection, ["source", "savedStyleId", "unavailableReason"])
    || !["product", "team", "user"].includes(String(selection.source))
    || (selection.savedStyleId !== null && typeof selection.savedStyleId !== "string")
    || (selection.unavailableReason !== null && typeof selection.unavailableReason !== "string")
    || !isRecord(accessibility)
    || !hasExactKeys(accessibility, ["contrast", "colorVision", "source"])
    || !["standard", "high"].includes(String(accessibility.contrast))
    || !["standard", "color-safe"].includes(String(accessibility.colorVision))
    || !isRecord(accessibility.source)
    || !hasExactKeys(accessibility.source, ["contrast", "colorVision"])
    || Object.values(accessibility.source).some((source) => !["product", "user"].includes(String(source)))
    || !isRecord(modeColors)
    || !hasExactKeys(modeColors, ["light", "dark"])
    || !isSurfaceProjection(modeColors.light)
    || !isSurfaceProjection(modeColors.dark)
    || !isRecord(teamPolicy)
    || !hasExactKeys(teamPolicy, [
      "version", "defaultStyleId", "enabledStyleIds",
      "fallbackBuiltInPresetId", "etag",
    ])
    || teamPolicy.version !== 2
    || typeof teamPolicy.defaultStyleId !== "string"
    || !Array.isArray(teamPolicy.enabledStyleIds)
    || teamPolicy.enabledStyleIds.length < 2
    || teamPolicy.enabledStyleIds.some((id) => typeof id !== "string")
    || !teamPolicy.enabledStyleIds.includes(teamPolicy.defaultStyleId)
    || typeof teamPolicy.fallbackBuiltInPresetId !== "string"
    || typeof teamPolicy.etag !== "string"
    || !value.resolutionAdjustments
    || !Array.isArray(value.resolutionAdjustments)
  ) return null;
  if (style.basePreset !== undefined && (
    !isRecord(style.basePreset)
    || !hasExactKeys(style.basePreset, ["id", "revision", "checksum"])
    || typeof style.basePreset.id !== "string"
    || typeof style.basePreset.revision !== "string"
    || !isSha256(style.basePreset.checksum)
  )) return null;
  if (
    (style.kind === "teamCustom" && style.basePreset === undefined)
    || (style.kind === "builtIn" && style.basePreset !== undefined)
  ) return null;
  return value as unknown as EffectiveChartStyleV2;
}

export interface FrozenChartPresentationV2 {
  normalizedRenderShape: NormalizedChartRenderShapeV1;
  normalizedRenderShapeChecksum: string;
  renderContext: ChartRenderContext;
  requested: ChartPresentationTokensV2;
  effective: ChartPresentationTokensV2;
  adjustments: ChartStyleAdjustmentV2[];
  finalPaletteQA: Record<string, unknown> & { hardInvariantsPass: true };
  presentationChecksum: string;
  contextRecipeVersion: "2";
  renderShapeVersion: "chart-shape/1";
  barRadiusRecipeVersion: "bar-radius/1";
  paletteOverflowRecipeVersion: "palette-overflow/2";
}

export function resolvedPresentationFromFrozen(
  frozen: FrozenChartPresentationV2,
): ResolvedChartPresentationV2 {
  return {
    requested: frozen.requested,
    effective: frozen.effective,
    adjustments: frozen.adjustments,
    renderShapeVersion: frozen.renderShapeVersion,
    presentationPolicyVersion: "chart-presentation/1",
  };
}

export function buildChartStyleExportTheme(
  effective: EffectiveChartStyle,
): ChartStyleExportThemeV1 {
  return {
    kind: "chart-style",
    schemaVersion: 1,
    mode: "light",
    preset: structuredClonePreset(chartStylePreset(effective.preset.id)),
    presetChecksum: effective.preset.checksum,
    selectionSource: effective.selection.source,
    accessibility: {
      contrast: effective.accessibility.contrast,
      colorVision: effective.accessibility.colorVision,
    },
    teamPolicyEtag: effective.teamPolicy.etag,
    resolverPolicyVersion: "1",
    contextRecipeVersion: "1",
  };
}

export function parseChartStyleExportTheme(
  value: unknown,
): ChartStyleExportTheme | null {
  if (isRecord(value) && value.schemaVersion === 2) {
    return parseChartStyleExportThemeV2(value);
  }
  if (!isRecord(value) || !hasExactKeys(value, [
    "kind",
    "schemaVersion",
    "mode",
    "preset",
    "presetChecksum",
    "selectionSource",
    "accessibility",
    "teamPolicyEtag",
    "resolverPolicyVersion",
    "contextRecipeVersion",
  ])) return null;
  const preset = value.preset;
  const accessibility = value.accessibility;
  if (
    value.kind !== "chart-style"
    || value.schemaVersion !== 1
    || value.mode !== "light"
    || !isRecord(preset)
    || typeof preset.id !== "string"
    || !isRecord(accessibility)
    || !hasExactKeys(accessibility, ["contrast", "colorVision"])
    || !["standard", "high"].includes(String(accessibility.contrast))
    || !["standard", "color-safe"].includes(String(accessibility.colorVision))
    || !["product", "team", "user"].includes(String(value.selectionSource))
    || typeof value.presetChecksum !== "string"
    || !/^sha256:[0-9a-f]{64}$/.test(value.presetChecksum)
    || typeof value.teamPolicyEtag !== "string"
    || value.resolverPolicyVersion !== "1"
    || value.contextRecipeVersion !== "1"
  ) return null;
  const reviewed = chartStylePreset(preset.id as ChartStylePresetPackage["id"]);
  if (
    reviewed.id !== preset.id
    || stableJson(reviewed) !== stableJson(preset)
    || value.presetChecksum !== CHART_STYLE_PRESET_CHECKSUMS[reviewed.id]
  ) return null;
  return value as unknown as ChartStyleExportThemeV1;
}

export function buildChartStyleExportFreshnessV2(
  effective: EffectiveChartStyleV2,
): ChartStyleExportFreshnessV2 {
  return {
    kind: "chart-style-freshness",
    schemaVersion: 2,
    styleId: effective.style.id,
    revision: effective.style.revision,
    snapshotChecksum: effective.style.snapshotChecksum,
    teamPolicyEtag: effective.teamPolicy.etag,
    registryVersion: effective.registryVersion,
    builtInRegistryChecksum: effective.builtInRegistryChecksum,
  };
}

function parseChartStyleExportThemeV2(
  value: Record<string, unknown>,
): ChartStyleExportThemeV2 | null {
  if (!hasExactKeys(value, [
    "kind", "schemaVersion", "style", "selectionSource", "accessibility",
    "teamPolicyEtag", "registryVersion", "builtInRegistryChecksum",
    "structuralThemeChecksum", "modeColors", "resolverPolicyVersion",
    "contextRecipeVersion", "renderShapeVersion", "rendererContractVersion",
  ])) return null;
  const style = value.style;
  const accessibility = value.accessibility;
  const modeColors = value.modeColors;
  if (
    value.kind !== "chart-style"
    || !isRecord(style)
    || ![["kind", "id", "revision", "snapshotChecksum", "resolvedPresetSnapshot"],
      ["kind", "id", "revision", "snapshotChecksum", "basePreset", "resolvedPresetSnapshot"]]
      .some((keys) => hasExactKeys(style, keys))
    || !["builtIn", "teamCustom"].includes(String(style.kind))
    || typeof style.id !== "string" || !style.id
    || typeof style.revision !== "string" || !style.revision
    || !isSha256(style.snapshotChecksum)
    || !isRecord(style.resolvedPresetSnapshot)
    || style.resolvedPresetSnapshot.schemaVersion !== 2
    || style.resolvedPresetSnapshot.tokenSchemaVersion !== 2
    || style.resolvedPresetSnapshot.resolverPolicyVersion !== "chart-style/2.0"
    || !isRecord(accessibility)
    || !hasExactKeys(accessibility, ["contrast", "colorVision", "source"])
    || !["standard", "high"].includes(String(accessibility.contrast))
    || !["standard", "color-safe"].includes(String(accessibility.colorVision))
    || !isRecord(accessibility.source)
    || !hasExactKeys(accessibility.source, ["contrast", "colorVision"])
    || Object.values(accessibility.source).some((source) => !["product", "user"].includes(String(source)))
    || !["product", "team", "user"].includes(String(value.selectionSource))
    || typeof value.teamPolicyEtag !== "string" || !value.teamPolicyEtag
    || !Number.isInteger(value.registryVersion) || Number(value.registryVersion) < 0
    || value.builtInRegistryChecksum !== BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM
    || !isSha256(value.structuralThemeChecksum)
    || !isRecord(modeColors) || !hasExactKeys(modeColors, ["light"])
    || !isSurfaceProjection(modeColors.light)
    || value.resolverPolicyVersion !== "chart-style/2.0"
    || value.contextRecipeVersion !== "2"
    || value.renderShapeVersion !== "chart-shape/1"
    || value.rendererContractVersion !== "2"
  ) return null;
  if (style.basePreset !== undefined && (
    !isRecord(style.basePreset)
    || !hasExactKeys(style.basePreset, ["id", "revision", "checksum"])
    || typeof style.basePreset.id !== "string"
    || typeof style.basePreset.revision !== "string"
    || !isSha256(style.basePreset.checksum)
  )) return null;
  if (
    (style.kind === "teamCustom" && style.basePreset === undefined)
    || (style.kind === "builtIn" && style.basePreset !== undefined)
  ) return null;
  return value as unknown as ChartStyleExportThemeV2;
}

export function parseFrozenChartPresentationV2(
  value: unknown,
): FrozenChartPresentationV2 | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    "normalizedRenderShape", "normalizedRenderShapeChecksum", "renderContext",
    "requested", "effective", "adjustments", "finalPaletteQA",
    "presentationChecksum", "contextRecipeVersion", "renderShapeVersion",
    "barRadiusRecipeVersion", "paletteOverflowRecipeVersion",
  ])) return null;
  if (
    !isRecord(value.normalizedRenderShape)
    || value.normalizedRenderShape.version !== "chart-shape/1"
    || !isSha256(value.normalizedRenderShapeChecksum)
    || !isRecord(value.renderContext)
    || !isRecord(value.requested)
    || !isRecord(value.effective)
    || !Array.isArray(value.adjustments)
    || !isRecord(value.finalPaletteQA)
    || value.finalPaletteQA.hardInvariantsPass !== true
    || !isSha256(value.presentationChecksum)
    || value.contextRecipeVersion !== "2"
    || value.renderShapeVersion !== "chart-shape/1"
    || value.barRadiusRecipeVersion !== "bar-radius/1"
    || value.paletteOverflowRecipeVersion !== "palette-overflow/2"
  ) return null;
  return value as unknown as FrozenChartPresentationV2;
}

export function frozenPresentationChecksumPayload(
  frozen: FrozenChartPresentationV2,
): Omit<FrozenChartPresentationV2, "presentationChecksum"> {
  const { presentationChecksum: _checksum, ...payload } = frozen;
  return payload;
}

function isSurfaceProjection(value: unknown): value is EffectiveSurfacePaletteProjectionV2 {
  if (!isRecord(value)) return false;
  const palette = value.categoricalSeriesPalette;
  const qa = value.finalPaletteQA;
  return hasExactKeys(value, [
    "colors", "categoricalSeriesPalette", "maxDistinctSeriesColors",
    "paletteOverflowRecipeVersion", "finalPaletteQA", "adjustments",
  ])
    && isRecord(value.colors)
    && Array.isArray(palette) && palette.length >= 6 && palette.length <= 16
    && palette.every((color) => typeof color === "string" && /^#[0-9A-F]{6}$/.test(color))
    && value.maxDistinctSeriesColors === 16
    && value.paletteOverflowRecipeVersion === "palette-overflow/2"
    && isRecord(qa) && qa.hardInvariantsPass === true
    && Array.isArray(value.adjustments);
}

function isResolvedSnapshot(value: unknown): value is ResolvedChartStyleSnapshotV2 {
  return isRecord(value)
    && hasExactKeys(value, [
      "schemaVersion", "tokenSchemaVersion", "resolverPolicyVersion",
      "capabilities", "colors", "kindTokens",
    ])
    && value.schemaVersion === 2
    && value.tokenSchemaVersion === 2
    && value.resolverPolicyVersion === "chart-style/2.0"
    && isRecord(value.capabilities)
    && isRecord(value.colors)
    && hasExactKeys(value.colors, ["light", "dark"])
    && isRecord(value.colors.light)
    && isRecord(value.colors.dark)
    && isRecord(value.kindTokens);
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);
}

function structuredClonePreset(
  preset: Readonly<ChartStylePresetPackage>,
): ChartStylePresetPackage {
  return JSON.parse(JSON.stringify(preset)) as ChartStylePresetPackage;
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  return Object.keys(value).sort().join("\0") === [...keys].sort().join("\0");
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
