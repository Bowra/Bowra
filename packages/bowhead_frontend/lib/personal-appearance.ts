"use client";

import { parseFieldContract, type FieldContract } from "./configuration-field-guidance";
import { apiFetch, readApiError } from "./api-client";
import { readCanonicalEtag } from "./entity-tag";
import {
  parseEffectiveColorScheme,
  type EffectiveColorScheme,
  type ThemeDensity,
  type ThemeMode,
  type ThemeMotion,
  type ThemeRadius,
  type TypographyProfileId,
  type TypeScale,
} from "./theme-config";

export const PERSONAL_APPEARANCE_REFRESH_EVENT = "bowhead:personal-appearance-refresh";

/** Invalidate original page controllers after an actual settings save receipt. */
export function notifyPersonalAppearanceSaved(teamId: string): void {
  window.dispatchEvent(new CustomEvent(PERSONAL_APPEARANCE_REFRESH_EVENT, { detail: { teamId } }));
}

export type PersonalAppearanceSource = "product" | "team_default" | "user";

export type PersonalAccent =
  | { kind: "preset"; registryVersion: 1; id: string }
  | { kind: "custom"; seed: string };

export type PersonalBackground =
  | { kind: "preset"; registryVersion: 1; id: string }
  | { kind: "custom"; light: string; dark: string };

export interface PersonalPalette {
  accent: PersonalAccent;
  background: PersonalBackground;
}

export interface PersonalAppearanceOverrides {
  mode?: ThemeMode;
  accent?: PersonalAccent;
  background?: PersonalBackground;
  typographyProfile?: { type: "preset"; id: TypographyProfileId };
  typeScale?: TypeScale;
  density?: ThemeDensity;
  radius?: ThemeRadius;
  motion?: ThemeMotion;
}

export type PersonalAppearanceChange = {
  [Key in keyof PersonalAppearanceOverrides]?:
    | PersonalAppearanceOverrides[Key]
    | null;
};

export interface PersonalAppearanceEffective {
  mode: ThemeMode;
  typographyProfile: { type: "preset"; id: TypographyProfileId };
  typeScale: TypeScale;
  density: ThemeDensity;
  radius: ThemeRadius;
  motion: ThemeMotion;
  colorScheme: EffectiveColorScheme;
}

export interface PersonalAppearanceRegistryItem<T> {
  id: string;
  labelKey: string;
  value: T;
  swatch: {
    light?: { primary: string; onPrimary: string; secondary?: string };
    dark?: { primary: string; onPrimary: string; secondary?: string };
  };
  availability: "available" | "unavailable";
  reason?: string;
}

export interface PersonalAppearanceRegistries {
  palette: {
    version: 1;
    items: PersonalAppearanceRegistryItem<PersonalPalette>[];
  };
  accent: {
    version: 1;
    items: PersonalAppearanceRegistryItem<PersonalAccent>[];
  };
  background: {
    version: 1;
    items: PersonalAppearanceRegistryItem<PersonalBackground>[];
  };
}

export interface PersonalAppearanceResource {
  field_contract?: FieldContract;
  schemaVersion: 3;
  materialized: boolean;
  rowVersion: number;
  overrides: PersonalAppearanceOverrides;
  effective: PersonalAppearanceEffective;
  sources: Record<keyof PersonalAppearanceOverrides, PersonalAppearanceSource>;
  team: { revisionId: string | null; revisionNumber: number };
  registries: PersonalAppearanceRegistries;
  degradations: Array<{ field: string; reason: string }>;
  pendingUndo: { resetId: string; expiresAt: string } | null;
}

export interface PersonalAppearanceSnapshot {
  resource: PersonalAppearanceResource;
  etag: string;
}

export interface PersonalAppearanceValidation {
  status?: string;
  blocking: unknown[];
  warnings: unknown[];
  blockingCount?: number;
  warningCount?: number;
}

export interface PersonalAppearanceCompileResult {
  candidateId: string;
  baseEtag: string;
  team: { revisionId: string | null; revisionNumber: number };
  personalOverlayVersion: number;
  colorScheme: EffectiveColorScheme;
  inputSeeds: Record<string, unknown>;
  validation: PersonalAppearanceValidation;
  contextRecommendations: PersonalAppearanceRegistries;
}

const ETAG_RE = /^"personal-v3:[0-9a-f]{64}"$/;
const HEX_RE = /^#[0-9A-F]{6}$/;
const RESET_ID_RE = /^[0-9a-f]{32}$/;
const MODES = new Set(["system", "light", "dark"]);
const TYPOGRAPHY = new Set([
  "bowhead",
  "system",
  "readable",
  "serif-display",
]);
const TYPE_SCALES = new Set(["compact", "standard", "large", "extra-large"]);
const DENSITIES = new Set(["compact", "comfortable", "spacious"]);
const RADII = new Set(["sharp", "balanced", "soft"]);
const MOTIONS = new Set(["standard", "reduced"]);
const OVERRIDE_FIELDS = [
  "mode",
  "accent",
  "background",
  "typographyProfile",
  "typeScale",
  "density",
  "radius",
  "motion",
] as const;

export function parsePersonalAppearanceResource(
  value: unknown,
): PersonalAppearanceResource {
  const raw = recordValue(value, "personal appearance");
  exactKeys(
    raw,
    [
      ...(raw.field_contract === undefined ? [] : ["field_contract"]),
      "schemaVersion",
      "materialized",
      "rowVersion",
      "overrides",
      "effective",
      "sources",
      "team",
      "registries",
      "degradations",
      "pendingUndo",
    ],
    "personal appearance",
  );
  if (raw.schemaVersion !== 3 || typeof raw.materialized !== "boolean") {
    throw new Error("Invalid personal appearance metadata");
  }
  if (!isNonNegativeInteger(raw.rowVersion)) {
    throw new Error("Invalid personal appearance row version");
  }
  const sourcesRaw = recordValue(raw.sources, "personal appearance sources");
  exactKeys(sourcesRaw, [...OVERRIDE_FIELDS], "personal appearance sources");
  const sources = {} as PersonalAppearanceResource["sources"];
  for (const field of OVERRIDE_FIELDS) {
    const source = sourcesRaw[field];
    if (!isOneOf(source, ["product", "team_default", "user"])) {
      throw new Error(`Invalid personal appearance source: ${field}`);
    }
    sources[field] = source;
  }
  return {
    field_contract: parseFieldContract(raw.field_contract),
    schemaVersion: 3,
    materialized: raw.materialized,
    rowVersion: raw.rowVersion,
    overrides: parseOverrides(raw.overrides),
    effective: parseEffective(raw.effective),
    sources,
    team: parseTeam(raw.team),
    registries: parseRegistries(raw.registries),
    degradations: arrayValue(
      raw.degradations,
      "personal appearance degradations",
    ).map(parseDegradation),
    pendingUndo: parsePendingUndo(raw.pendingUndo),
  };
}

export function parsePersonalAppearanceCompileResult(
  value: unknown,
): PersonalAppearanceCompileResult {
  const raw = recordValue(value, "personal appearance compile result");
  exactKeys(
    raw,
    [
      "candidateId",
      "baseEtag",
      "team",
      "personalOverlayVersion",
      "colorScheme",
      "inputSeeds",
      "validation",
      "contextRecommendations",
    ],
    "personal appearance compile result",
  );
  if (
    typeof raw.candidateId !== "string" ||
    !raw.candidateId ||
    !validEtag(raw.baseEtag) ||
    !isNonNegativeInteger(raw.personalOverlayVersion)
  ) {
    throw new Error("Invalid personal appearance compile metadata");
  }
  return {
    candidateId: raw.candidateId,
    baseEtag: raw.baseEtag,
    team: parseTeam(raw.team),
    personalOverlayVersion: raw.personalOverlayVersion,
    colorScheme: parseEffectiveColorScheme(raw.colorScheme),
    inputSeeds: recordValue(raw.inputSeeds, "personal appearance input seeds"),
    validation: parseValidation(raw.validation),
    contextRecommendations: parseRegistries(raw.contextRecommendations),
  };
}

export async function getPersonalAppearance(
  options: { etag?: string; signal?: AbortSignal } = {},
): Promise<PersonalAppearanceSnapshot | null> {
  const response = await apiFetch("/api/v1/theme/personal", {
    headers: options.etag ? { "If-None-Match": options.etag } : undefined,
    signal: options.signal,
  });
  if (response.status === 304) return null;
  if (!response.ok) throw await readApiError(response);
  return snapshotFromResponse(response);
}

export async function patchPersonalAppearance(
  etag: string,
  changes: PersonalAppearanceChange,
): Promise<PersonalAppearanceSnapshot> {
  requireEtag(etag);
  const response = await apiFetch("/api/v1/theme/personal", {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      "If-Match": etag,
    },
    body: JSON.stringify({ changes }),
  });
  if (!response.ok) throw await readApiError(response);
  return snapshotFromResponse(response);
}

export async function compilePersonalAppearance(
  etag: string,
  candidate: PersonalAppearanceOverrides,
  candidateId: string,
  signal?: AbortSignal,
): Promise<PersonalAppearanceCompileResult> {
  requireEtag(etag);
  const response = await apiFetch("/api/v1/theme/personal/compile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ baseEtag: etag, candidate, candidateId }),
    signal,
  });
  if (!response.ok) throw await readApiError(response);
  return parsePersonalAppearanceCompileResult(await response.json());
}

export async function resetPersonalAppearance(
  etag: string,
  idempotencyKey: string,
): Promise<{
  snapshot: PersonalAppearanceSnapshot;
  undo: { resetId: string; expiresAt: string } | null;
}> {
  requireEtag(etag);
  const response = await apiFetch("/api/v1/theme/personal", {
    method: "DELETE",
    headers: {
      "If-Match": etag,
      "Idempotency-Key": idempotencyKey,
    },
  });
  if (!response.ok) throw await readApiError(response);
  const body = recordValue(await response.json(), "personal appearance reset");
  exactKeys(body, ["resource", "undo"], "personal appearance reset");
  const etagNext = readCanonicalEtag(response);
  requireEtag(etagNext);
  return {
    snapshot: {
      resource: parsePersonalAppearanceResource(body.resource),
      etag: etagNext,
    },
    undo: parsePendingUndo(body.undo),
  };
}

export async function undoPersonalAppearance(
  etag: string,
  resetId: string,
): Promise<PersonalAppearanceSnapshot> {
  requireEtag(etag);
  if (!RESET_ID_RE.test(resetId)) throw new Error("Invalid reset id");
  const response = await apiFetch("/api/v1/theme/personal/undo", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "If-Match": etag,
    },
    body: JSON.stringify({ resetId }),
  });
  if (!response.ok) throw await readApiError(response);
  return snapshotFromResponse(response);
}

async function snapshotFromResponse(
  response: Response,
): Promise<PersonalAppearanceSnapshot> {
  const etag = readCanonicalEtag(response);
  requireEtag(etag);
  return {
    resource: parsePersonalAppearanceResource(await response.json()),
    etag,
  };
}

function parseOverrides(value: unknown): PersonalAppearanceOverrides {
  const raw = recordValue(value, "personal appearance overrides");
  allowedKeys(raw, [...OVERRIDE_FIELDS], "personal appearance overrides");
  const result: PersonalAppearanceOverrides = {};
  if (raw.mode !== undefined) {
    result.mode = enumValue(raw.mode, MODES, "mode") as ThemeMode;
  }
  if (raw.accent !== undefined) result.accent = parseAccent(raw.accent);
  if (raw.background !== undefined) {
    result.background = parseBackground(raw.background);
  }
  if (raw.typographyProfile !== undefined) {
    result.typographyProfile = parseTypography(raw.typographyProfile);
  }
  if (raw.typeScale !== undefined) {
    result.typeScale = enumValue(
      raw.typeScale,
      TYPE_SCALES,
      "typeScale",
    ) as TypeScale;
  }
  if (raw.density !== undefined) {
    result.density = enumValue(
      raw.density,
      DENSITIES,
      "density",
    ) as ThemeDensity;
  }
  if (raw.radius !== undefined) {
    result.radius = enumValue(raw.radius, RADII, "radius") as ThemeRadius;
  }
  if (raw.motion !== undefined) {
    result.motion = enumValue(raw.motion, MOTIONS, "motion") as ThemeMotion;
  }
  return result;
}

function parseAccent(value: unknown): PersonalAccent {
  const raw = recordValue(value, "personal accent");
  if (raw.kind === "preset") {
    exactKeys(
      raw,
      ["kind", "registryVersion", "id"],
      "personal accent preset",
    );
    if (raw.registryVersion !== 1 || typeof raw.id !== "string" || !raw.id) {
      throw new Error("Invalid personal accent preset");
    }
    return { kind: "preset", registryVersion: 1, id: raw.id };
  }
  if (raw.kind === "custom") {
    exactKeys(raw, ["kind", "seed"], "personal accent custom value");
    return { kind: "custom", seed: hexValue(raw.seed, "accent seed") };
  }
  throw new Error("Invalid personal accent");
}

function parseBackground(value: unknown): PersonalBackground {
  const raw = recordValue(value, "personal background");
  if (raw.kind === "preset") {
    exactKeys(
      raw,
      ["kind", "registryVersion", "id"],
      "personal background preset",
    );
    if (raw.registryVersion !== 1 || typeof raw.id !== "string" || !raw.id) {
      throw new Error("Invalid personal background preset");
    }
    return { kind: "preset", registryVersion: 1, id: raw.id };
  }
  if (raw.kind === "custom") {
    exactKeys(
      raw,
      ["kind", "light", "dark"],
      "personal background custom value",
    );
    return {
      kind: "custom",
      light: hexValue(raw.light, "light background"),
      dark: hexValue(raw.dark, "dark background"),
    };
  }
  throw new Error("Invalid personal background");
}

function parseEffective(value: unknown): PersonalAppearanceEffective {
  const raw = recordValue(value, "personal appearance effective");
  exactKeys(
    raw,
    [
      "mode",
      "typographyProfile",
      "typeScale",
      "density",
      "radius",
      "motion",
      "colorScheme",
    ],
    "personal appearance effective",
  );
  return {
    mode: enumValue(raw.mode, MODES, "effective mode") as ThemeMode,
    typographyProfile: parseTypography(raw.typographyProfile),
    typeScale: enumValue(
      raw.typeScale,
      TYPE_SCALES,
      "effective typeScale",
    ) as TypeScale,
    density: enumValue(
      raw.density,
      DENSITIES,
      "effective density",
    ) as ThemeDensity,
    radius: enumValue(raw.radius, RADII, "effective radius") as ThemeRadius,
    motion: enumValue(raw.motion, MOTIONS, "effective motion") as ThemeMotion,
    colorScheme: parseEffectiveColorScheme(raw.colorScheme),
  };
}

function parseTypography(
  value: unknown,
): { type: "preset"; id: TypographyProfileId } {
  const raw = recordValue(value, "personal typography profile");
  exactKeys(raw, ["type", "id"], "personal typography profile");
  if (raw.type !== "preset") {
    throw new Error("Invalid typography profile type");
  }
  return {
    type: "preset",
    id: enumValue(
      raw.id,
      TYPOGRAPHY,
      "typography profile",
    ) as TypographyProfileId,
  };
}

function parseTeam(value: unknown): PersonalAppearanceResource["team"] {
  const raw = recordValue(value, "personal appearance team");
  exactKeys(
    raw,
    ["revisionId", "revisionNumber"],
    "personal appearance team",
  );
  if (
    (raw.revisionId !== null && typeof raw.revisionId !== "string") ||
    !isNonNegativeInteger(raw.revisionNumber)
  ) {
    throw new Error("Invalid personal appearance team");
  }
  return {
    revisionId: raw.revisionId,
    revisionNumber: raw.revisionNumber,
  };
}

function parseRegistries(value: unknown): PersonalAppearanceRegistries {
  const raw = recordValue(value, "personal appearance registries");
  exactKeys(
    raw,
    ["palette", "accent", "background"],
    "personal appearance registries",
  );
  return {
    palette: parseRegistry(raw.palette, "palette", parsePalette),
    accent: parseRegistry(raw.accent, "accent", parseAccent),
    background: parseRegistry(raw.background, "background", parseBackground),
  };
}

function parsePalette(value: unknown): PersonalPalette {
  const raw = recordValue(value, "personal palette");
  exactKeys(raw, ["accent", "background"], "personal palette");
  return {
    accent: parseAccent(raw.accent),
    background: parseBackground(raw.background),
  };
}

function parseRegistry<T>(
  value: unknown,
  label: string,
  parseValue: (value: unknown) => T,
): { version: 1; items: PersonalAppearanceRegistryItem<T>[] } {
  const raw = recordValue(value, `${label} registry`);
  exactKeys(raw, ["version", "items"], `${label} registry`);
  if (raw.version !== 1) throw new Error(`Invalid ${label} registry version`);
  return {
    version: 1,
    items: arrayValue(raw.items, `${label} registry items`).map((item) =>
      parseRegistryItem(item, label, parseValue),
    ),
  };
}

function parseRegistryItem<T>(
  value: unknown,
  label: string,
  parseValue: (value: unknown) => T,
): PersonalAppearanceRegistryItem<T> {
  const raw = recordValue(value, `${label} registry item`);
  allowedKeys(
    raw,
    ["id", "labelKey", "value", "swatch", "availability", "reason"],
    `${label} registry item`,
  );
  if (
    typeof raw.id !== "string" ||
    !raw.id ||
    typeof raw.labelKey !== "string" ||
    !raw.labelKey ||
    !isOneOf(raw.availability, ["available", "unavailable"]) ||
    (raw.reason !== undefined && typeof raw.reason !== "string")
  ) {
    throw new Error(`Invalid ${label} registry item`);
  }
  const swatchRaw = recordValue(raw.swatch, `${label} registry swatch`);
  allowedKeys(
    swatchRaw,
    ["light", "dark"],
    `${label} registry swatch`,
  );
  return {
    id: raw.id,
    labelKey: raw.labelKey,
    value: parseValue(raw.value),
    swatch: {
      ...(swatchRaw.light === undefined
        ? {}
        : { light: parseSwatchMode(swatchRaw.light) }),
      ...(swatchRaw.dark === undefined
        ? {}
        : { dark: parseSwatchMode(swatchRaw.dark) }),
    },
    availability: raw.availability,
    ...(raw.reason === undefined ? {} : { reason: raw.reason }),
  };
}

function parseSwatchMode(
  value: unknown,
): { primary: string; onPrimary: string; secondary?: string } {
  const raw = recordValue(value, "personal appearance swatch mode");
  allowedKeys(
    raw,
    ["primary", "onPrimary", "secondary"],
    "personal appearance swatch mode",
  );
  return {
    primary: hexValue(raw.primary, "swatch primary"),
    onPrimary: hexValue(raw.onPrimary, "swatch foreground"),
    ...(raw.secondary === undefined
      ? {}
      : { secondary: hexValue(raw.secondary, "swatch secondary") }),
  };
}

function parsePendingUndo(
  value: unknown,
): PersonalAppearanceResource["pendingUndo"] {
  if (value === null) return null;
  const raw = recordValue(value, "personal appearance undo");
  exactKeys(raw, ["resetId", "expiresAt"], "personal appearance undo");
  if (
    typeof raw.resetId !== "string" ||
    !RESET_ID_RE.test(raw.resetId) ||
    typeof raw.expiresAt !== "string" ||
    !raw.expiresAt
  ) {
    throw new Error("Invalid personal appearance undo");
  }
  return { resetId: raw.resetId, expiresAt: raw.expiresAt };
}

function parseDegradation(value: unknown): { field: string; reason: string } {
  const raw = recordValue(value, "personal appearance degradation");
  exactKeys(raw, ["field", "reason"], "personal appearance degradation");
  if (typeof raw.field !== "string" || typeof raw.reason !== "string") {
    throw new Error("Invalid personal appearance degradation");
  }
  return { field: raw.field, reason: raw.reason };
}

function parseValidation(value: unknown): PersonalAppearanceValidation {
  const raw = recordValue(value, "personal appearance validation");
  allowedKeys(
    raw,
    ["status", "blocking", "warnings", "blockingCount", "warningCount"],
    "personal appearance validation",
  );
  if (
    (raw.status !== undefined && typeof raw.status !== "string") ||
    !Array.isArray(raw.blocking) ||
    !Array.isArray(raw.warnings) ||
    (raw.blockingCount !== undefined &&
      !isNonNegativeInteger(raw.blockingCount)) ||
    (raw.warningCount !== undefined && !isNonNegativeInteger(raw.warningCount))
  ) {
    throw new Error("Invalid personal appearance validation");
  }
  return {
    ...(raw.status === undefined ? {} : { status: raw.status }),
    blocking: [...raw.blocking],
    warnings: [...raw.warnings],
    ...(raw.blockingCount === undefined
      ? {}
      : { blockingCount: raw.blockingCount }),
    ...(raw.warningCount === undefined
      ? {}
      : { warningCount: raw.warningCount }),
  };
}

function recordValue(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Invalid ${label}`);
  }
  return value as Record<string, unknown>;
}

function arrayValue(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`Invalid ${label}`);
  return value;
}

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
  label: string,
): void {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (
    actual.length !== wanted.length ||
    actual.some((key, index) => key !== wanted[index])
  ) {
    throw new Error(`Invalid ${label} fields`);
  }
}

function allowedKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
  label: string,
): void {
  const allowedSet = new Set(allowed);
  if (Object.keys(value).some((key) => !allowedSet.has(key))) {
    throw new Error(`Invalid ${label} fields`);
  }
}

function enumValue(value: unknown, allowed: Set<string>, label: string): string {
  if (typeof value !== "string" || !allowed.has(value)) {
    throw new Error(`Invalid ${label}`);
  }
  return value;
}

function hexValue(value: unknown, label: string): string {
  if (typeof value !== "string" || !HEX_RE.test(value)) {
    throw new Error(`Invalid ${label}`);
  }
  return value;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isOneOf<T extends string>(
  value: unknown,
  values: readonly T[],
): value is T {
  return typeof value === "string" && values.includes(value as T);
}

function validEtag(value: unknown): value is string {
  return typeof value === "string" && ETAG_RE.test(value);
}

function requireEtag(value: unknown): asserts value is string {
  if (!validEtag(value)) throw new Error("Invalid personal appearance ETag");
}
