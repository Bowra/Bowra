import { describe, expect, it } from "vitest";

import {
  PRINT_THEME_SNAPSHOT,
  PRINT_THEME_TOKEN_KEYS,
  parsePrintThemeSnapshot,
  printThemeChecksumMatches,
  type PrintThemeSnapshot,
} from "./print-theme";

// Built on the server by `build_print_theme_snapshot` from a compiled teal team brand
// (primarySeed #0F766E); the checksum is the server's own.
const TEAM_BRAND_SNAPSHOT: PrintThemeSnapshot = {
  ...PRINT_THEME_SNAPSHOT,
  tokens: {
    background: "#F2F7FF",
    surface: "#FCFDFF",
    surface2: "#E5EFFF",
    surface3: "#D5E6FF",
    line: "#77879F",
    line2: "#A8B9D1",
    ink: "#061223",
    ink2: "#2B394D",
    ink3: "#4F5F74",
    ink4: "#63728A",
    accent: "#07736B",
    accentHover: "#005A53",
    accentInk: "#FFFFFF",
    accentSoft: "#A6FFF4",
    accentTint: "#E5FFFB",
    warning: "#8A5A00",
    warningSoft: "#FFF0C2",
  },
  checksum: "sha256:08a2b62c3345d082f1362fe2b05f8670b4fc72a2a6cfd6c767f52eb2808628cd",
};

describe("print theme snapshot", () => {
  it("accepts the product default and a team brand of the same recipe", () => {
    expect(parsePrintThemeSnapshot(structuredClone(PRINT_THEME_SNAPSHOT))).toEqual(
      PRINT_THEME_SNAPSHOT,
    );
    expect(parsePrintThemeSnapshot(structuredClone(TEAM_BRAND_SNAPSHOT))).toEqual(
      TEAM_BRAND_SNAPSHOT,
    );
  });

  it("carries the compiled product default tokens", () => {
    expect(PRINT_THEME_SNAPSHOT.recipeVersion).toBe("bowhead-print-theme-v3");
    expect(Object.keys(PRINT_THEME_SNAPSHOT.tokens)).toEqual([...PRINT_THEME_TOKEN_KEYS]);
    expect(PRINT_THEME_SNAPSHOT.tokens.accent).toBe("#1754DB");
    expect(PRINT_THEME_SNAPSHOT.tokens.accentInk).toBe("#FFFFFF");
    expect(PRINT_THEME_SNAPSHOT.tokens.ink).toBe("#061223");
  });

  it.each([
    ["unknown field", (value: Record<string, unknown>) => { value.extra = true; }],
    ["recipe version", (value: Record<string, unknown>) => { value.recipeVersion = "bowhead-print-theme-v2"; }],
    ["lowercase token", (value: Record<string, unknown>) => {
      (value.tokens as Record<string, unknown>).surface = "#fffdf8";
    }],
    ["missing token", (value: Record<string, unknown>) => {
      delete (value.tokens as Record<string, unknown>).warningSoft;
    }],
    ["v2 warm neutral token", (value: Record<string, unknown>) => {
      (value.tokens as Record<string, unknown>).mutedInk = "#56503F";
    }],
    ["checksum format", (value: Record<string, unknown>) => { value.checksum = "sha256:zz"; }],
  ])("rejects mutated %s", (_label, mutate) => {
    const value = structuredClone(PRINT_THEME_SNAPSHOT) as unknown as Record<string, unknown>;
    mutate(value);

    expect(parsePrintThemeSnapshot(value)).toBeNull();
  });

  it("recomputes the server checksum for the default and a team brand", async () => {
    await expect(printThemeChecksumMatches(PRINT_THEME_SNAPSHOT)).resolves.toBe(true);
    await expect(printThemeChecksumMatches(TEAM_BRAND_SNAPSHOT)).resolves.toBe(true);
  });

  it("detects tokens that the checksum does not cover", async () => {
    const tampered = structuredClone(TEAM_BRAND_SNAPSHOT);
    tampered.tokens.accent = PRINT_THEME_SNAPSHOT.tokens.accent;

    expect(parsePrintThemeSnapshot(tampered)).not.toBeNull();
    await expect(printThemeChecksumMatches(tampered)).resolves.toBe(false);
  });
});
