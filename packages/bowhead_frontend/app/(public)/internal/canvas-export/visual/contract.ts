import { CANVAS_VISUAL_RENDERER_VERSION } from "@/lib/canvas-visual-identity";
import { SUPPORTED_LOCALES, type Locale } from "@/lib/locale";
import {
  parseFrozenChartPresentationV2,
  parseChartStyleExportTheme,
  type ChartStyleExportTheme,
  type FrozenChartPresentationV2,
} from "@/lib/chart/chart-style-export";
import {
  parsePrintThemeSnapshot,
  type PrintThemeSnapshot,
} from "@/lib/print-theme";

// Single-source reference (2026-07-31): this used to be a third copy of "1"; parity tests only compared
// BE↔canvas-visual-identity, so bumping to "2" nearly rejected all V2 exports here.
const RENDERER_VERSION = CANVAS_VISUAL_RENDERER_VERSION;
const VISUAL_KINDS = new Set([
  "chart",
  "spec_block",
]);

export interface VisualExportPayload {
  blockKey: string;
  sectionKind: "chart" | "spec_block";
  payload: Record<string, unknown>;
  locale: Locale;
  theme: ChartStyleExportTheme;
  printTheme: PrintThemeSnapshot;
  visualInputSha256: string;
  rendererVersion: string;
  frozenPresentation?: FrozenChartPresentationV2;
  exportSurfaceContractVersion?: "canvas-export-visual/2";
}

export function parseVisualExportPayload(
  value: unknown,
): VisualExportPayload | null {
  if (!isRecord(value)) return null;
  const {
    blockKey,
    sectionKind,
    payload,
    locale,
    theme,
    printTheme,
    visualInputSha256,
    rendererVersion,
    frozenPresentation,
    exportSurfaceContractVersion,
  } = value;
  const parsedTheme = parseChartStyleExportTheme(theme);
  const parsedPrintTheme = parsePrintThemeSnapshot(printTheme);
  if (
    typeof blockKey !== "string"
    || !blockKey
    || typeof sectionKind !== "string"
    || !VISUAL_KINDS.has(sectionKind)
    || !isRecord(payload)
    || !(SUPPORTED_LOCALES as readonly string[]).includes(String(locale))
    || !parsedTheme
    || !parsedPrintTheme
    || typeof visualInputSha256 !== "string"
    || !/^sha256:[0-9a-f]{64}$/.test(visualInputSha256)
    || rendererVersion !== RENDERER_VERSION
  ) {
    return null;
  }
  const isV2 = parsedTheme.schemaVersion === 2;
  const parsedPresentation = parseFrozenChartPresentationV2(
    frozenPresentation,
  );
  if (
    (isV2 && (
      parsedTheme.structuralThemeChecksum !== parsedPrintTheme.checksum
      ||
      exportSurfaceContractVersion !== "canvas-export-visual/2"
      || (sectionKind === "chart" && !parsedPresentation)
      || (sectionKind === "chart" && parsedPresentation
        && !isFrozenExportContext(parsedPresentation.renderContext, locale))
      || (sectionKind !== "chart" && frozenPresentation !== undefined)
    ))
    || (!isV2 && (
      frozenPresentation !== undefined
      || exportSurfaceContractVersion !== undefined
    ))
  ) return null;
  return {
    blockKey,
    sectionKind: sectionKind as VisualExportPayload["sectionKind"],
    payload,
    locale: locale as VisualExportPayload["locale"],
    theme: parsedTheme as ChartStyleExportTheme,
    printTheme: printTheme as PrintThemeSnapshot,
    visualInputSha256,
    rendererVersion,
    ...(parsedPresentation
      ? { frozenPresentation: parsedPresentation }
      : {}),
    ...(isV2
      ? { exportSurfaceContractVersion: "canvas-export-visual/2" as const }
      : {}),
  };
}

function isFrozenExportContext(
  value: unknown,
  locale: unknown,
): boolean {
  if (!isRecord(value) || !hasExactKeys(value, [
    "placement",
    "bounds",
    "input",
    "output",
    "mode",
    "locale",
    "reducedMotion",
    "forcedColors",
    "exactDataAlternative",
  ])) return false;
  return value.placement === "embed"
    && isRecord(value.bounds)
    && hasExactKeys(value.bounds, ["widthPx", "heightPx", "devicePixelRatio"])
    && value.bounds.widthPx === 1456
    && value.bounds.heightPx === 772
    && value.bounds.devicePixelRatio === 1
    && isRecord(value.input)
    && hasExactKeys(value.input, ["hover", "pointer", "keyboard"])
    && value.input.hover === false
    && value.input.pointer === "none"
    && value.input.keyboard === false
    && isRecord(value.output)
    && hasExactKeys(value.output, ["medium", "static"])
    && value.output.medium === "raster"
    && value.output.static === true
    && value.mode === "light"
    && value.locale === locale
    && value.reducedMotion === true
    && value.forcedColors === false
    && isRecord(value.exactDataAlternative)
    && hasExactKeys(value.exactDataAlternative, ["kind"])
    && value.exactDataAlternative.kind === "none";
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  return Object.keys(value).sort().join("\0")
    === [...keys].sort().join("\0");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
