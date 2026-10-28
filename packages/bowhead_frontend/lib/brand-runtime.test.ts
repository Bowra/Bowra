import { PRODUCT_NAME } from "@/lib/product-identity";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  activateCachedBrandForTeam,
  applyBrandBundleToDocument,
  applyCanonicalBrandFallback,
  getBrandSnapshot,
} from "./brand-runtime";
import {
  BRAND_CACHE_POINTER_KEY,
  BRAND_CACHE_PREFIX,
  COLOR_SCHEME_TOKEN_KEYS,
  parseEffectiveThemeResponse,
  type ColorSchemeMode,
  type EffectiveThemeResponse,
} from "./theme-config";

function tokens(offset = 0): ColorSchemeMode {
  return Object.fromEntries(
    COLOR_SCHEME_TOKEN_KEYS.map((key, index) => [
      key,
      `#${((index + 1 + offset) % 0xffffff).toString(16).padStart(6, "0").toUpperCase()}`,
    ]),
  ) as ColorSchemeMode;
}

function response(teamId = "team_1"): EffectiveThemeResponse {
  return {
    schemaVersion: 2,
    brand: {
      applicationName: "Acme Intelligence",
      logoAlt: "Acme Intelligence",
      logoOnLight: {
        kind: "asset",
        artifactId: "art_111111111111111111111111",
        path: "/api/v1/branding/assets/art_111111111111111111111111",
      },
      logoOnDark: { kind: "text", text: "Acme Intelligence" },
      mark: { kind: "generatedInitial", text: "A" },
      favicon: {
        kind: "asset",
        artifactId: "art_222222222222222222222222",
        path: "/api/v1/branding/assets/art_222222222222222222222222",
      },
    },
    effective: {
      version: 1,
      mode: "light",
      colorProfile: { type: "preset", id: "bowhead" },
      colorScheme: {
        tokenSchemaVersion: 2,
        light: tokens(),
        dark: tokens(40),
        checksum: `sha256:${"a".repeat(64)}`,
      },
      typographyProfile: { type: "preset", id: "system" },
      typeScale: "standard",
      density: "compact",
      radius: "soft",
      motion: "reduced",
      chartPalette: "colorblind-safe",
    },
    locks: { colorProfile: true, colorScheme: true },
    source: {
      mode: "user",
      colorProfile: "team_lock",
      colorScheme: "team_lock",
      typographyProfile: "team_default",
      typeScale: "product",
      density: "team_default",
      radius: "team_default",
      motion: "user",
      chartPalette: "user",
    },
    userConfig: { version: 1, mode: "light", motion: "reduced" },
    team: {
      id: teamId,
      themeRevision: 4,
      revisionId: "tbr_333333333333333333333333",
      configurationType: "custom",
    },
  };
}

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
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
  window.__BOWHEAD_BASE_PATH__ = undefined;
  window.localStorage.clear();
  document.getElementById("bowhead-team-brand-tokens")?.remove();
  document.documentElement.removeAttribute("data-application-name");
  document.documentElement.removeAttribute("data-brand-team");
  document.documentElement.removeAttribute("data-brand-revision");
  document.documentElement.removeAttribute("data-brand-checksum");
  document.title = "Bowhead";
  applyCanonicalBrandFallback();
  window.localStorage.clear();
});

describe("V2 effective brand runtime", () => {
  it("strictly parses the complete bundle and rejects partial token payloads", () => {
    const valid = response();
    expect(parseEffectiveThemeResponse(valid).brand?.applicationName).toBe(
      "Acme Intelligence",
    );

    const invalid = structuredClone(valid);
    delete (invalid.effective.colorScheme?.light as Partial<ColorSchemeMode>)
      .surface3;
    expect(() => parseEffectiveThemeResponse(invalid)).toThrow(
      "Invalid colorScheme.light shape",
    );
  });

  it("applies colors and identity state without writing document metadata", () => {
    document.title = "metadata sentinel";
    const applied = applyBrandBundleToDocument(response());
    const style = document.getElementById(
      "bowhead-team-brand-tokens",
    ) as HTMLStyleElement;

    expect(applied.team?.id).toBe("team_1");
    expect(style.textContent).toContain("--c-bg:#000001");
    expect(style.textContent).toContain("--c-accent-ink:#00000D");
    expect(style.textContent).toContain(':root[data-theme="dark"]');
    expect(document.documentElement.dataset.brandTeam).toBe("team_1");
    expect(document.documentElement.dataset.brandRevision).toBe(
      "tbr_333333333333333333333333",
    );
    expect(document.documentElement.dataset.applicationName).toBe(
      "Acme Intelligence",
    );
    expect(
      document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
        ?.content,
    ).toBe("#000001");
    expect(document.title).toBe("metadata sentinel");
    expect(
      document.querySelector("link[data-bowhead-brand-favicon]"),
    ).toBeNull();
    expect(window.localStorage.getItem(BRAND_CACHE_POINTER_KEY)).toBe("team_1");
    expect(
      JSON.parse(
        window.localStorage.getItem(`${BRAND_CACHE_PREFIX}team_1`) ?? "{}",
    ).response.userConfig,
    ).toEqual({ version: 1 });
  });

  it("removes legacy inline light tokens before applying a dark scheme", () => {
    const root = document.documentElement;
    root.style.setProperty("--c-bg", "#FAF8F3");
    root.style.setProperty("--surface", "#FFFFFF");
    const darkResponse = response();
    darkResponse.effective.mode = "dark";

    applyBrandBundleToDocument(darkResponse);

    expect(root.dataset.theme).toBe("dark");
    expect(root.style.getPropertyValue("--c-bg")).toBe("");
    expect(root.style.getPropertyValue("--surface")).toBe("");
    expect(
      document.getElementById("bowhead-team-brand-tokens")?.textContent,
    ).toContain(
      ':root[data-theme="dark"]{--c-bg:#000029;--bg:#000029',
    );
  });

  it("replaces an invalid fixed-node collision with a style element", () => {
    const collision = document.createElement("div");
    collision.id = "bowhead-team-brand-tokens";
    document.head.append(collision);

    applyBrandBundleToDocument(response());

    expect(
      document.getElementById("bowhead-team-brand-tokens")?.tagName,
    ).toBe("STYLE");
  });

  it("never leaves the previous team brand visible across a team switch", () => {
    applyBrandBundleToDocument(response("team_1"));

    expect(activateCachedBrandForTeam("team_2")).toBe(false);
    expect(document.documentElement.dataset.brandTeam).toBe("");
    expect(document.documentElement.dataset.applicationName).toBe(PRODUCT_NAME);
    expect(document.getElementById("bowhead-team-brand-tokens")).toBeNull();

    const teamTwo = response("team_2");
    teamTwo.brand!.applicationName = "Northstar";
    applyBrandBundleToDocument(teamTwo);
    applyCanonicalBrandFallback();
    expect(activateCachedBrandForTeam("team_2")).toBe(true);
    expect(getBrandSnapshot().brand?.applicationName).toBe("Northstar");
    expect(document.documentElement.dataset.brandTeam).toBe("team_2");
  });

  it("caches chart style in the same team-scoped bundle as branding", () => {
    const bundled = response("team_1");
    bundled.chartStyle = {
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
        contrast: "standard",
        colorVision: "standard",
        source: { contrast: "product", colorVision: "product" },
      },
      teamPolicy: {
        version: 1,
        defaultPresetId: "bowhead-editorial",
        enabledPresetIds: ["bowhead-editorial", "clean-blue"],
        etag: `W/"chart-style-policy:${"b".repeat(64)}"`,
      },
    };

    applyBrandBundleToDocument(bundled);
    applyCanonicalBrandFallback();
    expect(activateCachedBrandForTeam("team_1")).toBe(true);
    const chartStyle = getBrandSnapshot().chartStyle;
    expect(chartStyle?.schemaVersion === 1 ? chartStyle.preset.id : null).toBe("clean-blue");
  });

  it("clears both stylesheet and legacy inline tokens on canonical fallback", () => {
    applyBrandBundleToDocument(response());
    document.documentElement.style.setProperty("--c-bg", "#FAF8F3");

    applyCanonicalBrandFallback();

    expect(document.getElementById("bowhead-team-brand-tokens")).toBeNull();
    expect(document.documentElement.style.getPropertyValue("--c-bg")).toBe("");
  });

  it("rejects a cache whose embedded team does not match the cache key", () => {
    const mismatched = {
      version: 2,
      teamId: "team_2",
      response: response("team_1"),
    };
    window.localStorage.setItem(
      `${BRAND_CACHE_PREFIX}team_2`,
      JSON.stringify(mismatched),
    );

    expect(activateCachedBrandForTeam("team_2")).toBe(false);
    expect(document.documentElement.dataset.applicationName).toBe(PRODUCT_NAME);
  });
});
