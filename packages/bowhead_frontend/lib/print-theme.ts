// Mirror of the server-frozen `bowhead_core.print_theme` recipe. "v3": every token is copied
// from a compiled light scheme under the compiler's own token names. PRINT_THEME_SNAPSHOT is the
// product default (SaaS Blue accent family, cool neutrals, warning pair); a Business team with a
// published brand gets a snapshot of the same recipe built from its brand. The export-only
// renderer accepts any snapshot of this recipe whose checksum matches its tokens.
// `test_export_visual_identity_parity.py` pins the default tokens and checksum against the server.
import { sha256HexFallback } from "./sha256-fallback";

export const PRINT_THEME_RECIPE_VERSION = "bowhead-print-theme-v3";

// Exported paper (the visual frame and the chart surface its PNG is drawn on) is always white,
// whatever the team theme. Mirror of `bowhead_core.print_theme.PRINT_PAPER`.
export const PRINT_PAPER = "#FFFFFF";

export const PRINT_THEME_TOKEN_KEYS = [
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
  "warning",
  "warningSoft",
] as const;

export type PrintThemeTokenKey = (typeof PRINT_THEME_TOKEN_KEYS)[number];

export interface PrintThemeSnapshot {
  schemaVersion: 1;
  recipeVersion: typeof PRINT_THEME_RECIPE_VERSION;
  mode: "light";
  tokens: Record<PrintThemeTokenKey, string>;
  fontBundleVersion: "bowhead-system-fonts-v1";
  checksum: string;
}

export const PRINT_THEME_SNAPSHOT: PrintThemeSnapshot = {
  schemaVersion: 1,
  recipeVersion: PRINT_THEME_RECIPE_VERSION,
  mode: "light",
  tokens: {
    background: "#F4F7FB",
    surface: "#FFFFFF",
    surface2: "#E5EFFF",
    surface3: "#D5E6FF",
    line: "#77879F",
    line2: "#A8B9D1",
    ink: "#061223",
    ink2: "#2B394D",
    ink3: "#4F5F74",
    ink4: "#63728A",
    accent: "#1754DB",
    accentHover: "#003ABC",
    accentInk: "#FFFFFF",
    accentSoft: "#E1ECFF",
    accentTint: "#F5F9FF",
    warning: "#8A5A00",
    warningSoft: "#FFF0C2",
  },
  fontBundleVersion: "bowhead-system-fonts-v1",
  checksum: "sha256:f1b2e4faa632f9ebe955cfd0fe6617cb0750529c48e913b159232fc97f736738",
};

const SNAPSHOT_KEYS = [
  "schemaVersion",
  "recipeVersion",
  "mode",
  "tokens",
  "fontBundleVersion",
  "checksum",
] as const;

const HEX_TOKEN = /^#[0-9A-F]{6}$/;
const CHECKSUM = /^sha256:[0-9a-f]{64}$/;

// Structural check only: the server freezes either the product default or a team-brand
// snapshot. Whether the tokens still match their digest is checked asynchronously by
// `printThemeChecksumMatches`.
export function parsePrintThemeSnapshot(value: unknown): PrintThemeSnapshot | null {
  if (!isRecord(value) || !hasExactKeys(value, SNAPSHOT_KEYS)) return null;
  const tokens = value.tokens;
  if (!isRecord(tokens) || !hasExactKeys(tokens, PRINT_THEME_TOKEN_KEYS)) return null;
  if (
    value.schemaVersion !== PRINT_THEME_SNAPSHOT.schemaVersion
    || value.recipeVersion !== PRINT_THEME_SNAPSHOT.recipeVersion
    || value.mode !== PRINT_THEME_SNAPSHOT.mode
    || value.fontBundleVersion !== PRINT_THEME_SNAPSHOT.fontBundleVersion
    || typeof value.checksum !== "string"
    || !CHECKSUM.test(value.checksum)
    || PRINT_THEME_TOKEN_KEYS.some((key) => typeof tokens[key] !== "string" || !HEX_TOKEN.test(tokens[key] as string))
  ) return null;
  return value as unknown as PrintThemeSnapshot;
}

/**
 * Recompute the server checksum (`sha256` over `json.dumps(sort_keys=True,
 * separators=(",", ":"))` of the snapshot without `checksum`) so the painted tokens are the
 * ones the visual identity hash was keyed on. The digest is unkeyed: it catches a snapshot whose
 * tokens drifted from its checksum, not a forged one (the server alone freezes snapshots).
 */
export async function printThemeChecksumMatches(snapshot: PrintThemeSnapshot): Promise<boolean> {
  const { checksum, ...payload } = snapshot;
  const bytes = new TextEncoder().encode(sortedJson(payload));
  // The export browser may load this page over plain HTTP, where crypto.subtle is absent.
  const hex = typeof crypto !== "undefined" && crypto.subtle
    ? Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("")
    : sha256HexFallback(bytes);
  return checksum === `sha256:${hex}`;
}

function sortedJson(value: unknown): string {
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${sortedJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
