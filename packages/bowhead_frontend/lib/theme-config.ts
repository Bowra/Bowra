"use client";

import { PRODUCT_NAME } from "@/lib/product-identity";

import {
  parseEffectiveChartStyleV2,
  type EffectiveChartStyleV2,
} from "./chart/chart-style-export";

export type Theme = "light" | "dark";
export type ThemeMode = Theme | "system";
export type ColorProfileId =
  "bowhead" | "ocean" | "forest" | "plum" | "graphite";
export type ThemeAccent = "default" | "ocean" | "forest" | "plum" | "graphite";
export type TypographyProfileId =
  "bowhead" | "system" | "readable" | "serif-display";
export type TypeScale = "compact" | "standard" | "large" | "extra-large";
export type ThemeDensity = "comfortable" | "compact" | "spacious";
export type ThemeRadius = "balanced" | "sharp" | "soft";
export type ThemeMotion = "standard" | "reduced";
export type ChartPalette = "default" | "high-contrast" | "colorblind-safe";

export interface ThemeConfig {
  version: 1;
  mode: ThemeMode;
  colorProfile: { type: "preset"; id: ColorProfileId };
  typographyProfile: { type: "preset"; id: TypographyProfileId };
  typeScale: TypeScale;
  density: ThemeDensity;
  radius: ThemeRadius;
  motion: ThemeMotion;
  chartPalette: ChartPalette;
}

export interface UserThemeConfig {
  version: 1;
  mode?: ThemeMode;
  colorProfile?: { type: "preset"; id: ColorProfileId };
  typographyProfile?: { type: "preset"; id: TypographyProfileId };
  typeScale?: TypeScale;
  density?: ThemeDensity;
  radius?: ThemeRadius;
  motion?: ThemeMotion;
  chartPalette?: ChartPalette;
}

type ThemeField = Exclude<keyof ThemeConfig, "version">;

export const COLOR_SCHEME_TOKEN_KEYS = [
  "background",
  "surface",
  "surface2",
  "surface3",
  "line",
  "line2",
  "ink",
  "ink2",
  "ink3",
  "ink4",
  "accent",
  "accentHover",
  "accentInk",
  "accentSoft",
  "accentTint",
  "focus",
  "positive",
  "positiveSoft",
  "warning",
  "warningSoft",
  "error",
  "errorSoft",
  "info",
  "infoSoft",
] as const;

export type ColorSchemeToken = (typeof COLOR_SCHEME_TOKEN_KEYS)[number];
export type ColorSchemeMode = Record<ColorSchemeToken, string>;

export interface EffectiveColorScheme {
  tokenSchemaVersion: 2;
  light: ColorSchemeMode;
  dark: ColorSchemeMode;
  checksum: string;
}

export type BrandRenderable =
  | { kind: "asset"; artifactId: string; path: string }
  | { kind: "text"; text?: string }
  | { kind: "generatedInitial"; text: string };

export interface EffectiveBrand {
  applicationName: string;
  logoAlt: string;
  logoOnLight: BrandRenderable;
  logoOnDark: BrandRenderable;
  mark: BrandRenderable;
  favicon: BrandRenderable;
}

export type ChartStylePresetId =
  | "bowhead-editorial"
  | "clean-blue"
  | "analytical-neutral";
export type ChartContrast = "standard" | "high";
export type ChartColorVision = "standard" | "color-safe";

export interface EffectiveChartStyle {
  schemaVersion: 1;
  preset: {
    id: ChartStylePresetId;
    revision: string;
    checksum: string;
  };
  selection: {
    source: "product" | "team" | "user";
    savedPresetId: ChartStylePresetId | null;
    unavailableByTeamPolicy: boolean;
  };
  accessibility: {
    contrast: ChartContrast;
    colorVision: ChartColorVision;
    source: {
      contrast: "product" | "user";
      colorVision: "product" | "user";
    };
  };
  teamPolicy: {
    version: 1;
    defaultPresetId: ChartStylePresetId;
    enabledPresetIds: ChartStylePresetId[];
    etag: string;
  };
}

export interface EffectiveThemeResponse {
  schemaVersion?: 2;
  brand?: EffectiveBrand;
  effective: ThemeConfig & { colorScheme?: EffectiveColorScheme };
  locks: Partial<Record<"colorProfile" | "colorScheme", boolean>>;
  source: Partial<
    Record<
      ThemeField | "colorScheme",
      "product" | "team_default" | "user" | "team_lock"
    >
  >;
  userConfig: UserThemeConfig;
  chartStyle?: EffectiveChartStyle | EffectiveChartStyleV2;
  team?: {
    id?: string;
    brandName?: string | null;
    themeRevision?: number | null;
    revisionId?: string | null;
    configurationType?: "legacy" | "bowhead" | "preset" | "custom";
  };
}

export const THEME_STORAGE_KEY = "bowhead.theme";
export const THEME_CONFIG_STORAGE_KEY = "bowhead.theme_config";
export const THEME_CONFIG_CHANGED_EVT = "bowhead:theme-config-changed";
export const BRAND_BUNDLE_CHANGED_EVT = "bowhead:brand-bundle-changed";
export const BRAND_CACHE_POINTER_KEY = "bowhead.brand.active-team:v2";
export const BRAND_CACHE_PREFIX = "bowhead.brand.bundle:v2:";
export const CANONICAL_BOWHEAD_ICON_PATH = "/bowhead-icon.svg";
export const APPEARANCE_THEME_CONFIG_KEY = "appearance_theme_config";
const LEGACY_CHART_PRESET_KEY = "bowhead.chart_theme_preset";
const LEGACY_CHART_OVERRIDE_KEY = "bowhead.chart_theme_override";

export const DEFAULT_THEME_CONFIG: ThemeConfig = {
  version: 1,
  mode: "system",
  colorProfile: { type: "preset", id: "bowhead" },
  typographyProfile: { type: "preset", id: "bowhead" },
  typeScale: "standard",
  density: "comfortable",
  radius: "balanced",
  motion: "standard",
  chartPalette: "default",
};

export const DEFAULT_USER_THEME_CONFIG: UserThemeConfig = { version: 1 };

const THEME_MODES = new Set<ThemeMode>(["system", "light", "dark"]);
const COLOR_PROFILES = new Set<ColorProfileId>([
  "bowhead",
  "ocean",
  "forest",
  "plum",
  "graphite",
]);
const LEGACY_ACCENTS = new Set<ThemeAccent>([
  "default",
  "ocean",
  "forest",
  "plum",
  "graphite",
]);
const TYPOGRAPHY_PROFILES = new Set<TypographyProfileId>([
  "bowhead",
  "system",
  "readable",
  "serif-display",
]);
const TYPE_SCALES = new Set<TypeScale>([
  "compact",
  "standard",
  "large",
  "extra-large",
]);
const DENSITIES = new Set<ThemeDensity>(["comfortable", "compact", "spacious"]);
const RADII = new Set<ThemeRadius>(["balanced", "sharp", "soft"]);
const MOTIONS = new Set<ThemeMotion>(["standard", "reduced"]);
const CHART_PALETTES = new Set<ChartPalette>([
  "default",
  "high-contrast",
  "colorblind-safe",
]);
const HEX_COLOR = /^#[0-9A-F]{6}$/;
const CHECKSUM = /^sha256:[0-9a-f]{64}$/;
const BRAND_ID = /^tbr_[0-9a-f]{24}$/;
const ASSET_ID = /^art_[0-9a-f]{24}$/;

function readLegacyTheme(): ThemeMode | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage?.getItem?.(THEME_STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : null;
  } catch {
    return null;
  }
}

function readLegacyChartPalette(): ChartPalette | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage?.getItem?.(LEGACY_CHART_PRESET_KEY);
    return stored && CHART_PALETTES.has(stored as ChartPalette)
      ? (stored as ChartPalette)
      : null;
  } catch {
    return null;
  }
}

function objectValue(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function enumValue<T extends string>(
  value: unknown,
  allowed: Set<T>,
  fallback: T,
): T {
  return typeof value === "string" && allowed.has(value as T)
    ? (value as T)
    : fallback;
}

function colorProfileFromLegacyAccent(accent: unknown): ColorProfileId | null {
  if (typeof accent !== "string" || !LEGACY_ACCENTS.has(accent as ThemeAccent))
    return null;
  return accent === "default" ? "bowhead" : (accent as ColorProfileId);
}

function legacyAccentFromColorProfile(id: ColorProfileId): ThemeAccent {
  return id === "bowhead" ? "default" : id;
}

function normalizeProfile<T extends string>(
  value: unknown,
  allowed: Set<T>,
  fallback: { type: "preset"; id: T },
): { type: "preset"; id: T } {
  const raw = objectValue(value);
  if (raw.type !== "preset") return fallback;
  return {
    type: "preset",
    id: enumValue(raw.id, allowed, fallback.id),
  };
}

function normalizeThemeConfig(
  value: unknown,
  fallbackMode: ThemeMode,
  fallbackChartPalette = DEFAULT_THEME_CONFIG.chartPalette,
): ThemeConfig {
  const raw = objectValue(value);
  const legacyColorProfile = colorProfileFromLegacyAccent(raw.accent);

  return {
    version: 1,
    mode: enumValue(raw.mode, THEME_MODES, fallbackMode),
    colorProfile: legacyColorProfile
      ? { type: "preset", id: legacyColorProfile }
      : normalizeProfile(
          raw.colorProfile,
          COLOR_PROFILES,
          DEFAULT_THEME_CONFIG.colorProfile,
        ),
    typographyProfile: normalizeProfile(
      raw.typographyProfile,
      TYPOGRAPHY_PROFILES,
      DEFAULT_THEME_CONFIG.typographyProfile,
    ),
    typeScale: enumValue(
      raw.typeScale,
      TYPE_SCALES,
      DEFAULT_THEME_CONFIG.typeScale,
    ),
    density: enumValue(raw.density, DENSITIES, DEFAULT_THEME_CONFIG.density),
    radius: enumValue(raw.radius, RADII, DEFAULT_THEME_CONFIG.radius),
    motion: enumValue(raw.motion, MOTIONS, DEFAULT_THEME_CONFIG.motion),
    chartPalette: enumValue(
      raw.chartPalette,
      CHART_PALETTES,
      fallbackChartPalette,
    ),
  };
}

export function normalizeUserThemeConfig(value: unknown): UserThemeConfig {
  const raw = objectValue(value);
  const normalized: UserThemeConfig = { version: 1 };
  if (raw.version !== 1) return normalized;

  const legacyColorProfile = colorProfileFromLegacyAccent(raw.accent);
  if (typeof raw.mode === "string" && THEME_MODES.has(raw.mode as ThemeMode)) {
    normalized.mode = raw.mode as ThemeMode;
  }
  if (legacyColorProfile) {
    normalized.colorProfile = { type: "preset", id: legacyColorProfile };
  } else if (raw.colorProfile !== undefined) {
    const profile = normalizeProfile(
      raw.colorProfile,
      COLOR_PROFILES,
      DEFAULT_THEME_CONFIG.colorProfile,
    );
    if (profile.id === objectValue(raw.colorProfile).id)
      normalized.colorProfile = profile;
  }
  if (raw.typographyProfile !== undefined) {
    const profile = normalizeProfile(
      raw.typographyProfile,
      TYPOGRAPHY_PROFILES,
      DEFAULT_THEME_CONFIG.typographyProfile,
    );
    if (profile.id === objectValue(raw.typographyProfile).id)
      normalized.typographyProfile = profile;
  }
  if (
    typeof raw.typeScale === "string" &&
    TYPE_SCALES.has(raw.typeScale as TypeScale)
  ) {
    normalized.typeScale = raw.typeScale as TypeScale;
  }
  if (
    typeof raw.density === "string" &&
    DENSITIES.has(raw.density as ThemeDensity)
  ) {
    normalized.density = raw.density as ThemeDensity;
  }
  if (typeof raw.radius === "string" && RADII.has(raw.radius as ThemeRadius)) {
    normalized.radius = raw.radius as ThemeRadius;
  }
  if (
    typeof raw.motion === "string" &&
    MOTIONS.has(raw.motion as ThemeMotion)
  ) {
    normalized.motion = raw.motion as ThemeMotion;
  }
  if (
    typeof raw.chartPalette === "string" &&
    CHART_PALETTES.has(raw.chartPalette as ChartPalette)
  ) {
    normalized.chartPalette = raw.chartPalette as ChartPalette;
  }
  return normalized;
}

export function serializeUserThemeConfig(config: UserThemeConfig): string {
  return JSON.stringify(normalizeUserThemeConfig(config));
}

export function parseEffectiveThemeResponse(
  value: unknown,
): EffectiveThemeResponse {
  const raw = objectValue(value);
  const effectiveRaw = objectValue(raw.effective);
  const fallbackMode = enumValue(
    effectiveRaw.mode,
    THEME_MODES,
    DEFAULT_THEME_CONFIG.mode,
  );
  const effective = normalizeThemeConfig(effectiveRaw, fallbackMode);
  const userConfig = normalizeUserThemeConfig(raw.userConfig);
  const locks = normalizeLocks(raw.locks);
  const source = normalizeSources(raw.source);

  if (raw.schemaVersion !== 2) {
    return {
      effective,
      locks,
      source,
      userConfig,
      ...(raw.chartStyle === undefined
        ? {}
        : { chartStyle: normalizeEffectiveChartStyle(raw.chartStyle) }),
      team: normalizeLegacyTeam(raw.team),
    };
  }

  requireExactKeys(
    raw,
    [
      "schemaVersion",
      "brand",
      "effective",
      "locks",
      "source",
      "userConfig",
      "team",
      ...(raw.chartStyle === undefined ? [] : ["chartStyle"]),
    ],
    "effective theme response",
  );
  requireExactKeys(
    effectiveRaw,
    [
      "version",
      "mode",
      "colorProfile",
      "colorScheme",
      "typographyProfile",
      "typeScale",
      "density",
      "radius",
      "motion",
      "chartPalette",
    ],
    "effective",
  );
  validateSparseUserConfig(raw.userConfig);
  const colorScheme = parseEffectiveColorScheme(effectiveRaw.colorScheme);
  const brand = normalizeBrand(raw.brand);
  const team = normalizeV2Team(raw.team);
  return {
    schemaVersion: 2,
    brand,
    effective: { ...effective, colorScheme },
    locks,
    source,
    userConfig,
    ...(raw.chartStyle === undefined
      ? {}
      : { chartStyle: normalizeEffectiveChartStyleAny(raw.chartStyle) }),
    team,
  };
}

const CHART_STYLE_PRESET_IDS = new Set<ChartStylePresetId>([
  "bowhead-editorial",
  "clean-blue",
  "analytical-neutral",
]);

function normalizeEffectiveChartStyle(value: unknown): EffectiveChartStyle {
  const raw = objectValue(value);
  requireExactKeys(
    raw,
    ["schemaVersion", "preset", "selection", "accessibility", "teamPolicy"],
    "chartStyle",
  );
  if (raw.schemaVersion !== 1) throw new Error("Invalid chart style schema version");

  const preset = objectValue(raw.preset);
  requireExactKeys(preset, ["id", "revision", "checksum"], "chartStyle.preset");
  if (!CHART_STYLE_PRESET_IDS.has(preset.id as ChartStylePresetId))
    throw new Error("Invalid chart style preset id");
  if (typeof preset.revision !== "string" || !/^\d{4}\.\d{2}\.\d+$/.test(preset.revision))
    throw new Error("Invalid chart style preset revision");
  if (typeof preset.checksum !== "string" || !/^sha256:[a-f0-9]{64}$/.test(preset.checksum))
    throw new Error("Invalid chart style preset checksum");

  const selection = objectValue(raw.selection);
  requireExactKeys(
    selection,
    ["source", "savedPresetId", "unavailableByTeamPolicy"],
    "chartStyle.selection",
  );
  if (!["product", "team", "user"].includes(String(selection.source)))
    throw new Error("Invalid chart style selection source");
  if (
    selection.savedPresetId !== null &&
    !CHART_STYLE_PRESET_IDS.has(selection.savedPresetId as ChartStylePresetId)
  ) throw new Error("Invalid saved chart style preset id");
  if (typeof selection.unavailableByTeamPolicy !== "boolean")
    throw new Error("Invalid chart style policy availability");

  const accessibility = objectValue(raw.accessibility);
  requireExactKeys(
    accessibility,
    ["contrast", "colorVision", "source"],
    "chartStyle.accessibility",
  );
  if (!["standard", "high"].includes(String(accessibility.contrast)))
    throw new Error("Invalid chart style contrast");
  if (!["standard", "color-safe"].includes(String(accessibility.colorVision)))
    throw new Error("Invalid chart style color vision");
  const accessibilitySource = objectValue(accessibility.source);
  requireExactKeys(accessibilitySource, ["contrast", "colorVision"], "chartStyle.accessibility.source");
  for (const source of Object.values(accessibilitySource)) {
    if (!["product", "legacy", "user"].includes(String(source)))
      throw new Error("Invalid chart accessibility source");
  }

  const teamPolicy = objectValue(raw.teamPolicy);
  requireExactKeys(
    teamPolicy,
    ["version", "defaultPresetId", "enabledPresetIds", "etag"],
    "chartStyle.teamPolicy",
  );
  if (teamPolicy.version !== 1) throw new Error("Invalid chart style policy version");
  if (!CHART_STYLE_PRESET_IDS.has(teamPolicy.defaultPresetId as ChartStylePresetId))
    throw new Error("Invalid chart style policy default");
  if (
    !Array.isArray(teamPolicy.enabledPresetIds) ||
    teamPolicy.enabledPresetIds.length < 2 ||
    teamPolicy.enabledPresetIds.some((id) => !CHART_STYLE_PRESET_IDS.has(id as ChartStylePresetId)) ||
    new Set(teamPolicy.enabledPresetIds).size !== teamPolicy.enabledPresetIds.length ||
    !teamPolicy.enabledPresetIds.includes(teamPolicy.defaultPresetId)
  ) throw new Error("Invalid chart style enabled presets");
  if (typeof teamPolicy.etag !== "string" || !/^W\/"chart-style-policy:[a-f0-9]{64}"$/.test(teamPolicy.etag))
    throw new Error("Invalid chart style policy etag");

  return {
    schemaVersion: 1,
    preset: {
      id: preset.id as ChartStylePresetId,
      revision: preset.revision,
      checksum: preset.checksum,
    },
    selection: {
      source: selection.source as EffectiveChartStyle["selection"]["source"],
      savedPresetId: selection.savedPresetId as ChartStylePresetId | null,
      unavailableByTeamPolicy: selection.unavailableByTeamPolicy,
    },
    accessibility: {
      contrast: accessibility.contrast as ChartContrast,
      colorVision: accessibility.colorVision as ChartColorVision,
      source: accessibilitySource as EffectiveChartStyle["accessibility"]["source"],
    },
    teamPolicy: {
      version: 1,
      defaultPresetId: teamPolicy.defaultPresetId as ChartStylePresetId,
      enabledPresetIds: [...teamPolicy.enabledPresetIds] as ChartStylePresetId[],
      etag: teamPolicy.etag,
    },
  };
}

function normalizeEffectiveChartStyleAny(
  value: unknown,
): EffectiveChartStyle | EffectiveChartStyleV2 {
  const raw = objectValue(value);
  if (raw.schemaVersion === 2) {
    const parsed = parseEffectiveChartStyleV2(value);
    if (!parsed) throw new Error("Invalid chart style V2 response");
    return parsed;
  }
  return normalizeEffectiveChartStyle(value);
}

export function canonicalEffectiveThemeFallback(): EffectiveThemeResponse {
  return {
    brand: {
      applicationName: PRODUCT_NAME,
      logoAlt: PRODUCT_NAME,
      logoOnLight: { kind: "text", text: PRODUCT_NAME },
      logoOnDark: { kind: "text", text: PRODUCT_NAME },
      mark: { kind: "generatedInitial", text: "B" },
      favicon: { kind: "generatedInitial", text: "B" },
    },
    effective: { ...DEFAULT_THEME_CONFIG },
    locks: {},
    source: {},
    userConfig: DEFAULT_USER_THEME_CONFIG,
    team: {
      brandName: PRODUCT_NAME,
    },
  };
}

export function isCanonicalBrandBundle(
  bundle: EffectiveThemeResponse,
): boolean {
  const configurationType = bundle.team?.configurationType;
  if (
    configurationType === "bowhead" ||
    configurationType === "legacy"
  ) {
    return true;
  }
  if (
    configurationType === "preset" ||
    configurationType === "custom"
  ) {
    return false;
  }
  if (bundle.schemaVersion === 2) return false;
  // A pre-V2 bundle without `brand` (the gateway's legacy no-resolver path returns only
  // `team.brandName`) is rendered with the canonical fallback brand, so it is canonical too;
  // otherwise the title would say Bowhead while the favicon became a generated initial.
  return (
    bundle.brand === undefined ||
    bundle.brand.applicationName === PRODUCT_NAME
  );
}

export function parseEffectiveColorScheme(value: unknown): EffectiveColorScheme {
  const raw = objectValue(value);
  requireExactKeys(
    raw,
    ["tokenSchemaVersion", "light", "dark", "checksum"],
    "colorScheme",
  );
  if (
    raw.tokenSchemaVersion !== 2 ||
    typeof raw.checksum !== "string" ||
    !CHECKSUM.test(raw.checksum)
  ) {
    throw new Error("Invalid effective color scheme metadata");
  }
  return {
    tokenSchemaVersion: 2,
    light: normalizeColorSchemeMode(raw.light, "light"),
    dark: normalizeColorSchemeMode(raw.dark, "dark"),
    checksum: raw.checksum,
  };
}

function normalizeColorSchemeMode(
  value: unknown,
  mode: "light" | "dark",
): ColorSchemeMode {
  const raw = objectValue(value);
  requireExactKeys(raw, [...COLOR_SCHEME_TOKEN_KEYS], `colorScheme.${mode}`);
  const normalized = {} as ColorSchemeMode;
  for (const token of COLOR_SCHEME_TOKEN_KEYS) {
    const color = raw[token];
    if (typeof color !== "string" || !HEX_COLOR.test(color)) {
      throw new Error(`Invalid effective color token: ${mode}.${token}`);
    }
    normalized[token] = color;
  }
  return normalized;
}

function normalizeBrand(value: unknown): EffectiveBrand {
  const raw = objectValue(value);
  requireExactKeys(
    raw,
    [
      "applicationName",
      "logoAlt",
      "logoOnLight",
      "logoOnDark",
      "mark",
      "favicon",
    ],
    "brand",
  );
  const applicationName = identityText(raw.applicationName, "applicationName");
  const logoAlt = identityText(raw.logoAlt, "logoAlt");
  return {
    applicationName,
    logoAlt,
    logoOnLight: normalizeBrandRenderable(raw.logoOnLight, applicationName),
    logoOnDark: normalizeBrandRenderable(raw.logoOnDark, applicationName),
    mark: normalizeBrandRenderable(raw.mark, applicationName),
    favicon: normalizeBrandRenderable(raw.favicon, applicationName),
  };
}

function normalizeBrandRenderable(
  value: unknown,
  applicationName: string,
): BrandRenderable {
  const raw = objectValue(value);
  if (raw.kind === "asset") {
    requireExactKeys(raw, ["kind", "artifactId", "path"], "brand asset");
    if (
      typeof raw.artifactId !== "string" ||
      !ASSET_ID.test(raw.artifactId) ||
      raw.path !== `/api/v1/branding/assets/${raw.artifactId}`
    ) {
      throw new Error("Invalid brand asset reference");
    }
    return {
      kind: "asset",
      artifactId: raw.artifactId,
      path: raw.path,
    };
  }
  if (raw.kind === "text") {
    requireAllowedKeys(raw, ["kind", "text"], "brand text");
    return {
      kind: "text",
      text:
        raw.text === undefined
          ? applicationName
          : identityText(raw.text, "brand text"),
    };
  }
  if (raw.kind === "generatedInitial") {
    requireExactKeys(raw, ["kind", "text"], "brand initial");
    const text = identityText(raw.text, "brand initial");
    if ([...text].length !== 1)
      throw new Error("Invalid generated brand initial");
    return { kind: "generatedInitial", text };
  }
  throw new Error("Invalid brand renderable");
}

function normalizeV2Team(value: unknown): EffectiveThemeResponse["team"] {
  const raw = objectValue(value);
  requireExactKeys(
    raw,
    ["id", "themeRevision", "revisionId", "configurationType"],
    "team",
  );
  if (typeof raw.id !== "string" || !raw.id)
    throw new Error("Invalid effective team id");
  if (
    !["legacy", "bowhead", "preset", "custom"].includes(
      String(raw.configurationType),
    )
  ) {
    throw new Error("Invalid effective configuration type");
  }
  const syntheticLegacy =
    raw.configurationType === "legacy" &&
    raw.themeRevision === null &&
    raw.revisionId === null;
  if (
    !syntheticLegacy &&
    (typeof raw.themeRevision !== "number" ||
      !Number.isSafeInteger(raw.themeRevision) ||
      raw.themeRevision < 1)
  ) {
    throw new Error("Invalid effective theme revision");
  }
  if (
    !syntheticLegacy &&
    (typeof raw.revisionId !== "string" || !BRAND_ID.test(raw.revisionId))
  ) {
    throw new Error("Invalid effective theme revision id");
  }
  return {
    id: raw.id,
    themeRevision: raw.themeRevision as number | null,
    revisionId: raw.revisionId as string | null,
    configurationType: raw.configurationType as
      "legacy" | "bowhead" | "preset" | "custom",
  };
}

function normalizeLegacyTeam(value: unknown): EffectiveThemeResponse["team"] {
  const raw = objectValue(value);
  return {
    brandName: typeof raw.brandName === "string" ? raw.brandName : null,
  };
}

function normalizeLocks(value: unknown): EffectiveThemeResponse["locks"] {
  const raw = objectValue(value);
  requireAllowedKeys(raw, ["colorProfile", "colorScheme"], "locks");
  const locks: EffectiveThemeResponse["locks"] = {};
  for (const key of ["colorProfile", "colorScheme"] as const) {
    if (raw[key] !== undefined) {
      if (typeof raw[key] !== "boolean")
        throw new Error(`Invalid lock: ${key}`);
      locks[key] = raw[key];
    }
  }
  return locks;
}

function normalizeSources(value: unknown): EffectiveThemeResponse["source"] {
  const raw = objectValue(value);
  requireAllowedKeys(raw, [...THEME_FIELDS_CLIENT, "colorScheme"], "source");
  const source: EffectiveThemeResponse["source"] = {};
  for (const [key, candidate] of Object.entries(raw)) {
    if (
      !["product", "team_default", "user", "team_lock"].includes(
        String(candidate),
      )
    ) {
      throw new Error(`Invalid theme source: ${key}`);
    }
    source[key as ThemeField | "colorScheme"] = candidate as
      "product" | "team_default" | "user" | "team_lock";
  }
  return source;
}

const THEME_FIELDS_CLIENT: ThemeField[] = [
  "mode",
  "colorProfile",
  "typographyProfile",
  "typeScale",
  "density",
  "radius",
  "motion",
  "chartPalette",
];

function validateSparseUserConfig(value: unknown): void {
  const raw = objectValue(value);
  requireAllowedKeys(raw, ["version", ...THEME_FIELDS_CLIENT], "userConfig");
  if (raw.version !== 1) throw new Error("Invalid user theme config version");
}

function identityText(value: unknown, label: string): string {
  if (
    typeof value !== "string" ||
    !value.trim() ||
    [...value].length > 80 ||
    /[\u0000-\u001F\u007F\u202A-\u202E\u2066-\u2069]/.test(value)
  ) {
    throw new Error(`Invalid ${label}`);
  }
  return value.trim();
}

function requireExactKeys(
  value: Record<string, unknown>,
  expected: string[],
  label: string,
): void {
  if (
    Object.keys(value).length !== expected.length ||
    expected.some((key) => !(key in value))
  ) {
    throw new Error(`Invalid ${label} shape`);
  }
}

function requireAllowedKeys(
  value: Record<string, unknown>,
  allowed: string[],
  label: string,
): void {
  if (Object.keys(value).some((key) => !allowed.includes(key))) {
    throw new Error(`Invalid ${label} shape`);
  }
}

export function loadThemeConfig(): ThemeConfig {
  if (typeof window === "undefined") return DEFAULT_THEME_CONFIG;

  const fallbackMode = readLegacyTheme() ?? DEFAULT_THEME_CONFIG.mode;
  const fallbackChartPalette =
    readLegacyChartPalette() ?? DEFAULT_THEME_CONFIG.chartPalette;
  try {
    const raw = window.localStorage?.getItem?.(THEME_CONFIG_STORAGE_KEY);
    if (!raw)
      return {
        ...DEFAULT_THEME_CONFIG,
        mode: fallbackMode,
        chartPalette: fallbackChartPalette,
      };
    return normalizeThemeConfig(
      JSON.parse(raw),
      fallbackMode,
      fallbackChartPalette,
    );
  } catch {
    return {
      ...DEFAULT_THEME_CONFIG,
      mode: fallbackMode,
      chartPalette: fallbackChartPalette,
    };
  }
}

export function resolveThemeMode(mode: ThemeMode): Theme {
  if (mode === "light" || mode === "dark") return mode;
  if (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches
  ) {
    return "dark";
  }
  return "light";
}

export function applyThemeConfigToDoc(
  config: ThemeConfig,
  resolvedTheme = resolveThemeMode(config.mode),
): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  const colorProfile = config.colorProfile.id;

  root.dataset.theme = resolvedTheme;
  root.dataset.themeMode = config.mode;
  root.dataset.colorProfile = colorProfile;
  root.dataset.themeAccent = legacyAccentFromColorProfile(colorProfile);
  root.dataset.typographyProfile = config.typographyProfile.id;
  root.dataset.typeScale = config.typeScale;
  root.dataset.themeDensity = config.density;
  root.dataset.themeRadius = config.radius;
  root.dataset.themeMotion = config.motion;
  root.dataset.chartPalette = config.chartPalette;
  root.style.colorScheme = resolvedTheme;
}

export function saveThemeConfig(config: ThemeConfig): void {
  if (typeof window === "undefined") return;
  const normalized = normalizeThemeConfig(config, config.mode);
  try {
    window.localStorage.setItem(
      THEME_CONFIG_STORAGE_KEY,
      JSON.stringify(normalized),
    );
    if (normalized.mode === "system") {
      window.localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      window.localStorage.setItem(THEME_STORAGE_KEY, normalized.mode);
    }
  } catch {
    /* privacy mode / quota exceeded: the current tab still gets the DOM update */
  }
  applyThemeConfigToDoc(normalized);
  window.dispatchEvent(new CustomEvent(THEME_CONFIG_CHANGED_EVT));
}

export function resetThemeConfig(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(THEME_CONFIG_STORAGE_KEY);
    window.localStorage.removeItem(THEME_STORAGE_KEY);
    window.localStorage.removeItem(LEGACY_CHART_PRESET_KEY);
    window.localStorage.removeItem(LEGACY_CHART_OVERRIDE_KEY);
  } catch {
    /* ignore */
  }
  applyThemeConfigToDoc(DEFAULT_THEME_CONFIG);
  window.dispatchEvent(new CustomEvent(THEME_CONFIG_CHANGED_EVT));
}
