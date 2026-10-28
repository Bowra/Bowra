import { beforeEach, describe, expect, it, vi } from "vitest";

import { COLOR_SCHEME_TOKEN_KEYS } from "./theme-config";

const api = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  readApiError: vi.fn(),
}));

vi.mock("./api-client", () => api);

import {
  getPersonalAppearance,
  parsePersonalAppearanceResource,
  patchPersonalAppearance,
} from "./personal-appearance";

const ETAG = `"personal-v3:${"a".repeat(64)}"`;

function tokens(offset = 0) {
  return Object.fromEntries(
    COLOR_SCHEME_TOKEN_KEYS.map((key, index) => [
      key,
      `#${(index + 1 + offset).toString(16).padStart(6, "0").toUpperCase()}`,
    ]),
  );
}

function resource() {
  return {
    schemaVersion: 3,
    materialized: false,
    rowVersion: 0,
    overrides: {
      accent: { kind: "preset", registryVersion: 1, id: "ocean" },
    },
    effective: {
      mode: "system",
      typographyProfile: { type: "preset", id: "bowhead" },
      typeScale: "standard",
      density: "comfortable",
      radius: "balanced",
      motion: "standard",
      colorScheme: {
        tokenSchemaVersion: 2,
        light: tokens(),
        dark: tokens(30),
        checksum: `sha256:${"b".repeat(64)}`,
      },
    },
    sources: {
      mode: "product",
      accent: "user",
      background: "team_default",
      typographyProfile: "product",
      typeScale: "product",
      density: "team_default",
      radius: "product",
      motion: "product",
    },
    team: { revisionId: null, revisionNumber: 0 },
    registries: {
      palette: {
        version: 1,
        items: [
          {
            id: "saas-blue",
            labelKey: "appearance.palette.saasBlue",
            value: {
              accent: { kind: "custom", seed: "#2563EB" },
              background: {
                kind: "custom",
                light: "#F8FAFC",
                dark: "#0F172A",
              },
            },
            swatch: {
              light: {
                primary: "#2563EB",
                secondary: "#F8FAFC",
                onPrimary: "#FFFFFF",
              },
              dark: {
                primary: "#427FFF",
                secondary: "#0F172A",
                onPrimary: "#07142D",
              },
            },
            availability: "available",
          },
        ],
      },
      accent: {
        version: 1,
        items: [
          {
            id: "ocean",
            labelKey: "appearance.accent.ocean",
            value: { kind: "preset", registryVersion: 1, id: "ocean" },
            swatch: {
              light: { primary: "#2F6F84", onPrimary: "#FFFFFF" },
              dark: { primary: "#6FAFC4", onPrimary: "#000000" },
            },
            availability: "available",
          },
        ],
      },
      background: {
        version: 1,
        items: [
          {
            id: "pure-white",
            labelKey: "appearance.background.pureWhite",
            value: {
              kind: "preset",
              registryVersion: 1,
              id: "pure-white",
            },
            swatch: {
              light: {
                primary: "#FFFFFF",
                secondary: "#F8F8F8",
                onPrimary: "#222222",
              },
              dark: {
                primary: "#121212",
                secondary: "#1A1A1A",
                onPrimary: "#EEEEEE",
              },
            },
            availability: "available",
          },
        ],
      },
    },
    degradations: [],
    pendingUndo: null,
  };
}

beforeEach(() => {
  api.apiFetch.mockReset();
  api.readApiError.mockReset();
});

describe("personal appearance contract", () => {
  it("parses the strict editable resource", () => {
    const parsed = parsePersonalAppearanceResource(resource());

    expect(parsed.overrides.accent).toEqual({
      kind: "preset",
      registryVersion: 1,
      id: "ocean",
    });
    expect(parsed.registries.background.items[0].swatch.light?.primary).toBe(
      "#FFFFFF",
    );
    expect(parsed.registries.palette.items[0].value.background).toEqual({
      kind: "custom",
      light: "#F8FAFC",
      dark: "#0F172A",
    });
  });

  it("rejects unknown fields and non-canonical colors", () => {
    expect(() =>
      parsePersonalAppearanceResource({ ...resource(), userId: "user_1" }),
    ).toThrow(/fields/);

    const invalid = resource();
    invalid.registries.accent.items[0].swatch.light!.primary = "#2f6f84";
    expect(() => parsePersonalAppearanceResource(invalid)).toThrow(
      /swatch primary/,
    );
  });

  it("uses conditional GET and preserves the response ETag", async () => {
    api.apiFetch.mockResolvedValueOnce(
      new Response(JSON.stringify(resource()), {
        status: 200,
        headers: { ETag: ETAG, "Content-Type": "application/json" },
      }),
    );

    const result = await getPersonalAppearance({ etag: ETAG });

    expect(api.apiFetch).toHaveBeenCalledWith("/api/v1/theme/personal", {
      headers: { "If-None-Match": ETAG },
      signal: undefined,
    });
    expect(result?.etag).toBe(ETAG);
  });

  it("accepts the weakened ETag a compressing CDN forwards, and echoes the strong form", async () => {
    // docs/sprints/brand-appearance-repair-2026-09-09/spike.md §2 A1: behind
    // EdgeOne the panel threw "Invalid personal appearance ETag" on load, so the
    // whole appearance panel was unusable for every member.
    api.apiFetch.mockResolvedValueOnce(
      new Response(JSON.stringify(resource()), {
        status: 200,
        headers: { ETag: `W/${ETAG}`, "Content-Type": "application/json" },
      }),
    );

    const result = await getPersonalAppearance();

    expect(result?.etag).toBe(ETAG);

    api.apiFetch.mockResolvedValueOnce(
      new Response(JSON.stringify(resource()), {
        status: 200,
        headers: { ETag: `W/${ETAG}`, "Content-Type": "application/json" },
      }),
    );

    await patchPersonalAppearance(result!.etag, { mode: "dark" });

    expect(api.apiFetch).toHaveBeenLastCalledWith(
      "/api/v1/theme/personal",
      expect.objectContaining({
        headers: { "Content-Type": "application/json", "If-Match": ETAG },
      }),
    );
  });

  it("PATCH sends only the requested fields with If-Match", async () => {
    api.apiFetch.mockResolvedValueOnce(
      new Response(JSON.stringify(resource()), {
        status: 200,
        headers: { ETag: ETAG, "Content-Type": "application/json" },
      }),
    );

    await patchPersonalAppearance(ETAG, { mode: "dark" });

    expect(api.apiFetch).toHaveBeenCalledWith(
      "/api/v1/theme/personal",
      expect.objectContaining({
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "If-Match": ETAG,
        },
        body: JSON.stringify({ changes: { mode: "dark" } }),
      }),
    );
  });
});
