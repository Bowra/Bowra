import type { PersonalPalette } from "./personal-appearance";

export const PERSONAL_PALETTE_FILE_MAX_BYTES = 16 * 1024;

export interface PersonalPaletteFile {
  version: 1;
  name: string;
  accent: string;
  background: {
    light: string;
    dark: string;
  };
}

export const PERSONAL_PALETTE_TEMPLATE = `${JSON.stringify(
  {
    version: 1,
    name: "My SaaS palette",
    accent: "#2563EB",
    background: {
      light: "#F8FAFC",
      dark: "#0F172A",
    },
  } satisfies PersonalPaletteFile,
  null,
  2,
)}\n`;

const HEX_RE = /^#[0-9A-Fa-f]{6}$/;

export function parsePersonalPaletteFile(text: string): PersonalPaletteFile {
  if (new TextEncoder().encode(text).byteLength > PERSONAL_PALETTE_FILE_MAX_BYTES) {
    throw new Error("palette_file_too_large");
  }

  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("palette_file_invalid_json");
  }
  const raw = recordValue(value);
  exactKeys(raw, ["version", "name", "accent", "background"]);
  if (raw.version !== 1) throw new Error("palette_file_invalid_version");
  if (
    typeof raw.name !== "string" ||
    !raw.name.trim() ||
    raw.name.trim().length > 64
  ) {
    throw new Error("palette_file_invalid_name");
  }
  const background = recordValue(raw.background);
  exactKeys(background, ["light", "dark"]);
  return {
    version: 1,
    name: raw.name.trim(),
    accent: normalizeHex(raw.accent),
    background: {
      light: normalizeHex(background.light),
      dark: normalizeHex(background.dark),
    },
  };
}

export function paletteFileToPersonalPalette(
  file: PersonalPaletteFile,
): PersonalPalette {
  return {
    accent: { kind: "custom", seed: file.accent },
    background: {
      kind: "custom",
      light: file.background.light,
      dark: file.background.dark,
    },
  };
}

function recordValue(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("palette_file_invalid_shape");
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
): void {
  const keys = Object.keys(value).sort();
  const expected = [...allowed].sort();
  if (
    keys.length !== expected.length ||
    keys.some((key, index) => key !== expected[index])
  ) {
    throw new Error("palette_file_invalid_shape");
  }
}

function normalizeHex(value: unknown): string {
  if (typeof value !== "string" || !HEX_RE.test(value)) {
    throw new Error("palette_file_invalid_color");
  }
  return value.toUpperCase();
}
