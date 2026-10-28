import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { effectiveChartStyleV2Fixture } from "@/__tests__/chart-style-export-fixture";

import {
  THEME_CONFIG_STORAGE_KEY,
  COLOR_SCHEME_TOKEN_KEYS,
  applyThemeConfigToDoc,
  canonicalEffectiveThemeFallback,
  isCanonicalBrandBundle,
  loadThemeConfig,
  parseEffectiveThemeResponse,
  serializeUserThemeConfig,
} from "./theme-config";

function createStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key: string) => values.get(key) ?? null,
    key: (index: number) => Array.from(values.keys())[index] ?? null,
    removeItem: (key: string) => values.delete(key),
    setItem: (key: string, value: string) => values.set(key, String(value)),
  };
}

beforeEach(() => {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: createStorage(),
  });
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-theme-mode");
  document.documentElement.removeAttribute("data-color-profile");
  document.documentElement.removeAttribute("data-theme-accent");
  document.documentElement.removeAttribute("data-chart-palette");
});

describe("theme config", () => {
  it("migrates legacy accent config to colorProfile", () => {
    window.localStorage.setItem(
      THEME_CONFIG_STORAGE_KEY,
      JSON.stringify({ mode: "dark", accent: "ocean", density: "compact" }),
    );
    window.localStorage.setItem("bowhead.chart_theme_preset", "colorblind-safe");

    const config = loadThemeConfig();

    expect(config.mode).toBe("dark");
    expect(config.colorProfile).toEqual({ type: "preset", id: "ocean" });
    expect(config.density).toBe("compact");
    expect(config.chartPalette).toBe("colorblind-safe");
  });

  it("writes new dataset attributes and legacy accent compatibility", () => {
    const config = loadThemeConfig();

    applyThemeConfigToDoc({
      ...config,
      colorProfile: { type: "preset", id: "bowhead" },
      typographyProfile: { type: "preset", id: "readable" },
      typeScale: "large",
      chartPalette: "colorblind-safe",
    }, "dark");

    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.dataset.colorProfile).toBe("bowhead");
    expect(document.documentElement.dataset.themeAccent).toBe("default");
    expect(document.documentElement.dataset.typographyProfile).toBe("readable");
    expect(document.documentElement.dataset.typeScale).toBe("large");
    expect(document.documentElement.dataset.chartPalette).toBe("colorblind-safe");
  });

  it("serializes sparse user config without product defaults", () => {
    expect(serializeUserThemeConfig({
      version: 1,
      mode: "dark",
      chartPalette: "colorblind-safe",
    })).toBe('{"version":1,"mode":"dark","chartPalette":"colorblind-safe"}');
  });

  it("classifies canonical identity from configuration ownership, not its name", () => {
    const canonical = canonicalEffectiveThemeFallback();
    expect(isCanonicalBrandBundle(canonical)).toBe(true);

    canonical.schemaVersion = 2;
    canonical.team = {
      id: "team_1",
      themeRevision: 1,
      revisionId: `tbr_${"1".repeat(24)}`,
      configurationType: "bowhead",
    };
    expect(isCanonicalBrandBundle(canonical)).toBe(true);

    canonical.team.configurationType = "custom";
    expect(isCanonicalBrandBundle(canonical)).toBe(false);
  });

  it("strictly parses the optional effective chart-style projection", () => {
    const canonical = canonicalEffectiveThemeFallback();
    const parsed = parseEffectiveThemeResponse({
      ...canonical,
      chartStyle: {
        schemaVersion: 1,
        preset: {
          id: "clean-blue",
          revision: "2026.09.1",
          checksum: "sha256:551ee4ce83f3af7b7b33bf992de42339aeda15a3da0e57594898e9570dc72a54",
        },
        selection: {
          source: "user",
          savedPresetId: "clean-blue",
          unavailableByTeamPolicy: false,
        },
        accessibility: {
          contrast: "high",
          colorVision: "standard",
          source: { contrast: "user", colorVision: "product" },
        },
        teamPolicy: {
          version: 1,
          defaultPresetId: "bowhead-editorial",
          enabledPresetIds: ["bowhead-editorial", "clean-blue"],
          etag: `W/"chart-style-policy:${"a".repeat(64)}"`,
        },
      },
    });

    expect(parsed.chartStyle?.schemaVersion === 1 ? parsed.chartStyle.preset.id : null).toBe("clean-blue");
    expect(parsed.chartStyle?.accessibility.contrast).toBe("high");
    expect(() => parseEffectiveThemeResponse({
      ...canonical,
      chartStyle: { ...parsed.chartStyle, arbitraryRendererOptions: {} },
    })).toThrow("Invalid chartStyle shape");

    expect(() => parseEffectiveThemeResponse({
      ...canonical,
      chartStyle: {
        ...parsed.chartStyle,
        teamPolicy: {
          ...parsed.chartStyle?.teamPolicy,
          defaultPresetId: "analytical-neutral",
        },
      },
    })).toThrow("Invalid chart style enabled presets");
  });

  it("accepts the negotiated V2 display projection and rejects registry drift", () => {
    const canonical = canonicalEffectiveThemeFallback();
    const chartStyle = effectiveChartStyleV2Fixture();
    const colorTokens = Object.fromEntries(
      COLOR_SCHEME_TOKEN_KEYS.map((key) => [key, "#334455"]),
    );
    const bundle = {
      ...canonical,
      schemaVersion: 2,
      effective: {
        ...canonical.effective,
        colorScheme: {
          tokenSchemaVersion: 2,
          light: colorTokens,
          dark: colorTokens,
          checksum: `sha256:${"c".repeat(64)}`,
        },
      },
      team: {
        id: "team_1",
        themeRevision: null,
        revisionId: null,
        configurationType: "legacy",
      },
    };

    const parsed = parseEffectiveThemeResponse({
      ...bundle,
      chartStyle,
    });

    expect(parsed.chartStyle?.schemaVersion).toBe(2);
    expect(parsed.chartStyle?.schemaVersion === 2 ? parsed.chartStyle.style.id : null)
      .toBe("bowhead-editorial");

    expect(() => parseEffectiveThemeResponse({
      ...bundle,
      chartStyle: {
        ...chartStyle,
        builtInRegistryChecksum: `sha256:${"0".repeat(64)}`,
      },
    })).toThrow("Invalid chart style V2 response");
  });
});
