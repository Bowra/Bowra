import { PRODUCT_NAME } from "@/lib/product-identity";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BRAND_BOOTSTRAP_SCRIPT } from "./brand-bootstrap-script";
import {
  BRAND_METADATA_FAVICON_ID,
  BRAND_METADATA_TITLE_ID,
} from "./brand-metadata";
import {
  BRAND_CACHE_POINTER_KEY,
  BRAND_CACHE_PREFIX,
  COLOR_SCHEME_TOKEN_KEYS,
} from "./theme-config";

function cacheBrand(configurationType: "bowhead" | "custom") {
  const tokens = Object.fromEntries(
    COLOR_SCHEME_TOKEN_KEYS.map((key) => [key, "#123456"]),
  );
  return {
    version: 2,
    teamId: "team_1",
    response: {
      schemaVersion: 2,
      team: {
        id: "team_1",
        revisionId: `tbr_${"1".repeat(24)}`,
        configurationType,
      },
      effective: {
        colorScheme: {
          tokenSchemaVersion: 2,
          checksum: `sha256:${"a".repeat(64)}`,
          light: tokens,
          dark: tokens,
        },
      },
      brand: {
        applicationName: "Bowhead",
        favicon: { kind: "generatedInitial", text: "B" },
      },
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

function runBootstrap(configurationType: "bowhead" | "custom") {
  window.localStorage.setItem(BRAND_CACHE_POINTER_KEY, "team_1");
  window.localStorage.setItem(
    `${BRAND_CACHE_PREFIX}team_1`,
    JSON.stringify(cacheBrand(configurationType)),
  );
  Function(BRAND_BOOTSTRAP_SCRIPT)();
  return document.getElementById(
    BRAND_METADATA_FAVICON_ID,
  ) as HTMLLinkElement | null;
}

beforeEach(() => {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: createStorage(),
  });
});

afterEach(() => {
  window.localStorage.clear();
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
  document.getElementById(BRAND_METADATA_TITLE_ID)?.remove();
  document.getElementById(BRAND_METADATA_FAVICON_ID)?.remove();
  document.getElementById("bowhead-team-brand-tokens")?.remove();
  document.documentElement.removeAttribute("data-application-name");
  document.documentElement.removeAttribute("data-brand-checksum");
  document.documentElement.removeAttribute("data-brand-team");
  document.documentElement.removeAttribute("data-brand-revision");
  document.documentElement.style.removeProperty("--c-bg");
  document.documentElement.style.removeProperty("--surface");
  document.title = "Bowhead";
});

describe("authenticated brand bootstrap", () => {
  it("creates canonical fixed nodes without a cached team", () => {
    Function(BRAND_BOOTSTRAP_SCRIPT)();

    expect(document.getElementById(BRAND_METADATA_TITLE_ID)?.textContent).toBe(
      `${PRODUCT_NAME} — Verified Intelligence`,
    );
    expect(
      document
        .getElementById(BRAND_METADATA_FAVICON_ID)
        ?.getAttribute("href"),
    ).toBe("/bowhead-icon.svg");
  });

  it("uses the canonical whale favicon under the verified prefix", () => {
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: "pe_1",
      teamId: "team_1",
      baseUrl: "https://example.com/bowhead",
      basePath: "/bowhead",
      matchSource: "trusted_header",
    };

    expect(runBootstrap("bowhead")?.getAttribute("href")).toBe(
      "/bowhead/bowhead-icon.svg",
    );
  });

  it("keeps the generated initial for custom identity ownership", () => {
    expect(runBootstrap("custom")?.getAttribute("href")).toMatch(
      /^data:image\/svg\+xml,/,
    );
  });

  it("takes token ownership from legacy inline styles with one fixed node", () => {
    document.documentElement.style.setProperty("--c-bg", "#FAF8F3");
    document.documentElement.style.setProperty("--surface", "#FFFFFF");

    runBootstrap("custom");
    runBootstrap("custom");

    expect(document.documentElement.style.getPropertyValue("--c-bg")).toBe("");
    expect(document.documentElement.style.getPropertyValue("--surface")).toBe(
      "",
    );
    expect(
      document.querySelectorAll("#bowhead-team-brand-tokens"),
    ).toHaveLength(1);
  });

  it("replaces an invalid fixed-node collision with a style element", () => {
    const collision = document.createElement("div");
    collision.id = "bowhead-team-brand-tokens";
    document.head.append(collision);

    runBootstrap("custom");

    expect(
      document.getElementById("bowhead-team-brand-tokens")?.tagName,
    ).toBe("STYLE");
  });

  it("does not create legacy metadata nodes or install correction listeners", () => {
    Function(BRAND_BOOTSTRAP_SCRIPT)();

    expect(BRAND_BOOTSTRAP_SCRIPT).not.toMatch(
      /data-bowhead-brand-favicon|addEventListener|MutationObserver/,
    );
  });
});
