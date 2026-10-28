import type { Locale } from "./locale";
import type { ReportSection } from "./types";
import type {
  ChartStyleExportTheme,
  EffectiveChartStyleV2,
  FrozenChartPresentationV2,
} from "./chart/chart-style-export";
import {
  PRINT_THEME_SNAPSHOT,
  type PrintThemeSnapshot,
} from "./print-theme";
import { sha256HexFallback } from "./sha256-fallback";

// "3" (2026-08-01): B1a static-safe source/copy contract changes the frozen
// visual input identity, so old cached receipts must not be reused.
// "4" (2026-08-12, chart-table-ux batch 1): export rendering changes — numeric labels always on (2.2),
// value-axis titles include units (2.3), protagonist muting/central main number (2.1b). Never reuse old cached receipts.
// "5" (2026-08-12, chart-table-ux batch 3): more changes — full-row export tables (4.1), pie legend
// shares (4.2), grid/axis borders use theme tokens (4.3).
// "6" (2026-08-14, chart-table-ux R8): value-axis ticks use column-level percentage mode — fraction-column
// ticks change 0/0.1/0.2 to 0/10/20 (same chart labels/tooltips/tables already showed 29.2%, only ticks retained
// raw values while axis title said "（%）", causing 100-fold misreading); scatter axes now use axisTitleOf.
// Same input no longer produces same pixels; never reuse old cached receipts.
// "7" (2026-08-15, chart-table-ux dogfood): exports omit chart action bars (including chart-kind switcher)
// — PDF charts on 116 had "Line ▾ / Open data / Download PNG / Download CSV",
// four buttons unclickable on paper. Same input no longer produces same pixels; never reuse old cached receipts.
// "8" (2026-08-15, chart-table-ux dogfood): exported charts no longer occupy only top half — export
// `.v2-chart-canvas-wrap` fills available height (old clamp 38vh=342px / available 772px,
// 52% blank). Same input no longer produces same pixels; never reuse old cached receipts.
// "9" (2026-08-15): export visual page sets **export-request locale** on `<html lang>` — rendering formerly
// followed gateway-selected bundle, unrelated to identity locale ⇒ same hash could yield numbers in two languages.
// Never reuse old cached receipts.
// "11" (2026-09-05): frozen chart-style preset packages and render-context
// recipes now drive export pixels; the default-theme identity is retired.
// "12" (2026-09-06): stable print chrome is frozen with the trusted chart
// snapshot and both identities participate in the visual input hash.
// "14" (2026-09-08): edge-label padding changes the exported line-chart geometry
// when a first/last data label would otherwise cross the canvas boundary.
// "15" (2026-09-27): top padding changes exported line-chart geometry — upward data labels gain top padding (2231de168),
// maximum label avoids top y-axis tick (4545ede3f), top padding computes anchor from scale and applies on first frame
// (da10a5f9d; export page formerly never received top padding). All three changed real report export pixels without version
// bump ⇒ old cached receipts still contain clipped labels; never reuse.
// "16" (2026-09-29): category axes no longer thin labels via `maxXTicks` — constant was set for **time axes** in
// 344px chat windows; applying to nominal category axes discards category identity (465 bar charts measured on 116: ten categories
// but five labels). Export widthClass=wide formerly used 12; Chart.js now autoSkips by actual label widths.
// Same input no longer produces same pixels; never reuse old cached receipts.
// "17" (2026-09-29): dual_axis multi-column axes no longer use first column name for whole axis — matching dimensions show only
// dimension (`元`/`%`), mismatches omit label (116 measurements: 17 of 106 axes across 53 charts were multi-column,
// none shared measure_header). Same input no longer produces same pixels; never reuse old cached receipts.
// "18" (2026-10-01): categorical series get a second channel once color alone stops being distinguishable.
// Built-in base colors were nudged to ΔE2000 ≥10 (normal) / ≥7 (protan/deutan) (e.g. analytical-neutral/dark
// slot 1 #A1B0B7→#808A94), overflow colors 7–16 are chosen by distinctness (`palette-overflow/2`), and series past
// the measured color-only capacity — or every series under color-safe — get fill patterns (bar/area/pie) and
// dashes (lines). Same input no longer yields identical pixels; never reuse old cached receipts.
// "19" (2026-10-01): chart readability batch 2 changes export pixels for existing charts: KPI values from one
// million show the magnitude as the main line and the exact value below; pie slices keep their full colors (the
// protagonist is offset instead of the others faded); waterfall drops are labelled below their bar with axis headroom;
// scatter name labels no longer cover other points; too-wide tables zoom to fit instead of losing columns; non-flow
// Sankey rows render as a table; long horizontal bars draw the largest rows that fit with a truncation note; KPI
// magnitude lines keep their unit; every waterfall follows the chart style's gridlines; exports honour the exporting
// user's legend-hidden series (`hidden_series`, part of the visual payload).
// Same input no longer yields identical pixels; old cached receipts must not be reused.
// "20" (2026-10-05): the print theme moves to recipe v3. Export chart ink, muted ink and grid come from the
// compiled scheme's cool neutrals (product default: ink #061223, ink3 #4F5F74, grid line #77879F / soft grid line2
// #A8B9D1, matching the on-screen --c-line/--c-line-2) instead of the warm #161310/#56503F/#D9D4CB, and a Business
// team's published brand reaches the snapshot. The export frame and chart surface stay white paper (#FFFFFF).
// Same input no longer yields identical pixels; old cached receipts must not be reused.
// "21" (2026-10-06): Chart Embed Spec. Every chart text is the interface sans (no Geist Mono ticks/labels/axis
// titles), data labels 12/500 without a backing plate and 6px outside bar ends; gridlines are one solid separator
// hairline (ink 12%, base line 20%) instead of the dashed / #77879F lines; bars are sized by band (clamp 24–96px) and horizontal bars
// reserve the widest label; axis titles state only the unit; the chart title row loses its tinted band and rule.
// Same input no longer yields identical pixels; old cached receipts must not be reused.
// Version 22: native name-only line endpoints, scoped month ticks and observation precision.
// Version 23: source-checked fixed numeric domains in native shared scale groups.
// Version 24: source-protected ordered native reading steps.
// Version 25: prepared static frames with stable entities and whole-dataset numeric domains.
// "30" (2026-10-11): hide endpoint numeric labels on the initial native line render.
// Version 32: source-bound analytical ranges, explicit series coordinates and static facet/row panels.
// Version 33: native table cells keep only short units inline (two characters or a currency code); longer unit text is
// named once in the column header. Same input no longer yields identical pixels; old cached receipts must not be reused.
// Version 34: ordinary numeric precision and base-unit Table readings; source verification retains available digits.
export const CANVAS_VISUAL_RENDERER_VERSION = "35";

const VISUAL_FIELDS: Record<string, readonly string[]> = {
  image: ["artifact_id", "caption", "alt"],
  design: [
    "title",
    "size_preset",
    "canvas_width",
    "canvas_height",
    "background",
    "layers",
    "flattened_artifact_id",
    "flattened_source_rev",
    "flattened_at",
  ],
  chart: ["spec", "hidden_series"],
  spec_block: ["format", "title", "direction", "nodes", "edges"],
};

/** Nonvisual, viewer-projected chart-spec fields. Parity test compares against server
 *  `_NON_VISUAL_SPEC_FIELDS`. See stripProvenance. */
const NON_VISUAL_SPEC_FIELDS = ["source_meta", "presentation_plan"] as const;

/** Exclude **provenance** fields from visual identity.
 *
 *  R8 (2026-08-14, real device on 116): `spec.source_meta` is **viewer-specific** —
 *  server `project_sources_for_viewer` rewrites it as viewer-safe `source-ref-v1:`
 *  alias object. Canvas detail exposes `visualInputSha256` computed from **pre-projection** payload,
 *  while we receive **post-projection** section ⇒ equality never holds ⇒ all
 *  exact_visual receipts mismatch ⇒ V2 export falls back to legacy.
 *
 *  ⚠ Do not disprove using receipt-we-sent == response-hash: when equality below fails,
 *  **no receipt is produced**; every sent receipt necessarily matches exactly, a tautology rather than measurement.
 *
 *  Not visual: export page renders only `ChartBlock`; source row `ChartSourceLine` lies outside screenshot
 *  bounds. Correct solution makes identity **projection-insensitive**, rather than recalculating one side. */
function stripProvenance(
  sectionKind: string,
  payload: Record<string, unknown>,
): Record<string, unknown> {
  if (sectionKind !== "chart") return payload;
  const spec = payload.spec;
  if (!spec || typeof spec !== "object" || Array.isArray(spec)) return payload;
  const record = spec as Record<string, unknown>;
  if (!NON_VISUAL_SPEC_FIELDS.some((field) => Object.hasOwn(record, field))) return payload;
  const plan = record.presentation_plan as { scale_groups?: unknown; guide_steps?: unknown; analytical_views?: unknown } | undefined;
  return {
    ...payload,
    spec: { ...Object.fromEntries(
      Object.entries(record).filter(
        ([key]) => !(NON_VISUAL_SPEC_FIELDS as readonly string[]).includes(key),
      ),
    ), ...(plan?.scale_groups ? { _shared_scale_groups: plan.scale_groups } : {}), ...(plan?.guide_steps ? { _guide_steps: plan.guide_steps } : {}),
    ...(Array.isArray(plan?.analytical_views) ? { _analytical_views: plan.analytical_views.filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item)).map((item) => Object.fromEntries(Object.entries(item).filter(([key, value]) => key !== "availability" || value !== "available"))) } : {}) },
  };
}

export async function computeCanvasVisualInputSha256(args: {
  section: ReportSection;
  locale: Locale;
  theme: ChartStyleExportTheme;
  printTheme?: PrintThemeSnapshot;
  rendererVersion?: string;
  frozenChartPresentation?: FrozenChartPresentationV2;
  exportSurfaceContractVersion?: "canvas-export-visual/2";
}): Promise<string | null> {
  const fields = VISUAL_FIELDS[args.section.kind];
  if (!fields) return null;
  const payload = stripProvenance(args.section.kind, pickFields(args.section, fields));
  const typedData = visualTypedData(args.section.kind, payload);
  const printTheme = args.printTheme ?? PRINT_THEME_SNAPSHOT;
  const themeHash = await sha256Canonical({
    chart_style_snapshot_checksum: await sha256Canonical(args.theme),
    print_theme_checksum: printTheme.checksum,
    chart_context_recipe_version: args.theme.contextRecipeVersion,
  });
  return sha256Canonical({
    effective_kind: effectiveVisualKind(args.section.kind, payload),
    ...(args.frozenChartPresentation
      ? { frozen_chart_presentation: args.frozenChartPresentation }
      : {}),
    locale: args.locale,
    payload,
    renderer_version:
      args.rendererVersion ?? CANVAS_VISUAL_RENDERER_VERSION,
    section_kind: args.section.kind,
    theme_hash: themeHash,
    typed_data_sha256:
      typedData === null ? null : await sha256Canonical(typedData),
    ...(args.exportSurfaceContractVersion
      ? { export_surface_contract_version: args.exportSurfaceContractVersion }
      : {}),
  });
}

export async function computeCanvasScreenVisualInputSha256V2(args: {
  section: ReportSection;
  locale: Locale;
  surfaceToken: string;
  effectiveChartStyle: EffectiveChartStyleV2;
  rendererVersion?: string;
}): Promise<string | null> {
  const fields = VISUAL_FIELDS[args.section.kind];
  if (!fields) return null;
  const payload = stripProvenance(
    args.section.kind,
    pickFields(args.section, fields),
  );
  const typedData = visualTypedData(args.section.kind, payload);
  const themeHash = await sha256Canonical({
    screen_chart_style: {
      schema_version: 2,
      style: args.effectiveChartStyle.style,
      accessibility: args.effectiveChartStyle.accessibility,
      mode_colors: args.effectiveChartStyle.modeColors,
      team_policy_etag: args.effectiveChartStyle.teamPolicy.etag,
      registry_version: args.effectiveChartStyle.registryVersion,
      built_in_registry_checksum:
        args.effectiveChartStyle.builtInRegistryChecksum,
      context_recipe_version: "2",
    },
    surface_token: args.surfaceToken,
  });
  return sha256Canonical({
    effective_kind: effectiveVisualKind(args.section.kind, payload),
    locale: args.locale,
    payload,
    renderer_version:
      args.rendererVersion ?? CANVAS_VISUAL_RENDERER_VERSION,
    section_kind: args.section.kind,
    theme_hash: themeHash,
    typed_data_sha256:
      typedData === null ? null : await sha256Canonical(typedData),
  });
}

export async function sha256Canonical(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(value));
  // crypto.subtle exists only in secure contexts (HTTPS/localhost). HTTP-only deployments
  // (e.g. intranet hosts accessed directly by IP) lack it; every V2 export receipt entry passes
  // here — must not throw, otherwise prepare catches surface_render_incomplete globally,
  // forcing all HTTP-deployment V2 exports to legacy. Fall back to pure JS (small JSON input,
  // no performance concern); both paths must produce identical output (comparison test guard).
  if (typeof crypto !== "undefined" && crypto.subtle) {
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const hex = Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    return `sha256:${hex}`;
  }
  return `sha256:${sha256HexFallback(bytes)}`;
}

function pickFields(
  section: ReportSection,
  fields: readonly string[],
): Record<string, unknown> {
  const source = section as unknown as Record<string, unknown>;
  return Object.fromEntries(
    fields
      .filter((field) => Object.hasOwn(source, field))
      .map((field) => [field, source[field]]),
  );
}

function visualTypedData(
  sectionKind: string,
  payload: Record<string, unknown>,
): unknown {
  if (sectionKind === "chart") {
    const spec = payload.spec;
    if (!spec || typeof spec !== "object" || Array.isArray(spec)) return null;
    const record = spec as Record<string, unknown>;
    return Array.isArray(record.rows)
      ? record.rows
      : (record.data ?? null);
  }
  if (sectionKind === "spec_block") {
    return {
      edges: payload.edges,
      nodes: payload.nodes,
    };
  }
  return null;
}

function effectiveVisualKind(
  sectionKind: string,
  payload: Record<string, unknown>,
): string {
  if (sectionKind === "chart") {
    const spec = payload.spec;
    if (spec && typeof spec === "object" && !Array.isArray(spec)) {
      const kind = (spec as Record<string, unknown>).kind;
      if (typeof kind === "string" && kind) return kind;
    }
  }
  if (sectionKind === "spec_block") {
    const format = payload.format;
    if (typeof format === "string" && format) return format;
  }
  return sectionKind;
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalValue(value));
}

function canonicalValue(value: unknown): unknown {
  if (value === null || value === undefined) return ["null"];
  if (typeof value === "boolean") return ["bool", value];
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("Visual identity numbers must be finite");
    }
    const normalized = Object.is(value, -0) ? 0 : value;
    const buffer = new ArrayBuffer(8);
    new DataView(buffer).setFloat64(0, normalized, false);
    const hex = Array.from(new Uint8Array(buffer))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    return ["f64", hex];
  }
  if (typeof value === "string") return ["str", value];
  if (Array.isArray(value)) {
    return ["array", value.map(canonicalValue)];
  }
  if (value && typeof value === "object") {
    return [
      "object",
      Object.entries(value)
        .filter(([, child]) => child !== undefined)
        .sort(([left], [right]) => compareUnicodeCodePoints(left, right))
        .map(([key, child]) => [key, canonicalValue(child)]),
    ];
  }
  throw new Error(`Unsupported visual identity value: ${typeof value}`);
}

function compareUnicodeCodePoints(left: string, right: string): number {
  const leftPoints = Array.from(left, (char) => char.codePointAt(0)!);
  const rightPoints = Array.from(right, (char) => char.codePointAt(0)!);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftPoints[index] - rightPoints[index];
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
}
