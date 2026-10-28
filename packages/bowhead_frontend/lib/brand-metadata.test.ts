import { PRODUCT_NAME } from "@/lib/product-identity";
import { afterEach, describe, expect, it } from "vitest";

import {
  BRAND_METADATA_OWNED_ATTR,
  BRAND_METADATA_TITLE_ID,
  BRAND_METADATA_FAVICON_ID,
  applyOwnedDocumentMetadata,
  composeDocumentTitle,
  resolveBrandMetadata,
} from "./brand-metadata";
import {
  canonicalEffectiveThemeFallback,
  type EffectiveThemeResponse,
} from "./theme-config";

function customBundle(): EffectiveThemeResponse {
  const bundle = canonicalEffectiveThemeFallback();
  bundle.schemaVersion = 2;
  bundle.brand = {
    ...bundle.brand!,
    applicationName: "Acme Intelligence",
    logoAlt: "Acme Intelligence",
    mark: { kind: "generatedInitial", text: "A" },
    favicon: { kind: "generatedInitial", text: "A" },
  };
  bundle.team = {
    id: "team_1",
    themeRevision: 2,
    revisionId: `tbr_${"2".repeat(24)}`,
    configurationType: "custom",
  };
  return bundle;
}

afterEach(() => {
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
  document.getElementById(BRAND_METADATA_TITLE_ID)?.remove();
  document.getElementById(BRAND_METADATA_FAVICON_ID)?.remove();
  document.head
    .querySelectorAll(
      "link[data-bowhead-public-favicon], link[data-bowhead-brand-favicon]",
    )
    .forEach((node) => node.remove());
});

describe("brand metadata contract", () => {
  it("composes titles from structured page and application names", () => {
    expect(
      composeDocumentTitle({
        applicationName: " Acme $& <Labs> ",
        pageTitle: " Q3 📈 ",
      }),
    ).toBe("Q3 📈 · Acme $& <Labs>");
    expect(
      composeDocumentTitle({
        applicationName: "Acme",
        pageTitle: "   ",
      }),
    ).toBe("Acme — Verified Intelligence");
  });

  it("drops the tagline for a white-labelled customer brand on public pages only", () => {
    expect(composeDocumentTitle({ applicationName: "Acme", pageTitle: null, whiteLabel: true })).toBe("Acme");
    expect(composeDocumentTitle({ applicationName: PRODUCT_NAME, pageTitle: null, whiteLabel: true })).toBe(
      `${PRODUCT_NAME} — Verified Intelligence`,
    );
    expect(composeDocumentTitle({ applicationName: "Acme", pageTitle: "Q3", whiteLabel: true })).toBe("Q3 · Acme");
  });

  it("resolves canonical and custom favicon ownership deterministically", () => {
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: "pe_1",
      teamId: "team_1",
      baseUrl: "https://example.com/bowhead",
      basePath: "/bowhead",
      matchSource: "trusted_header",
    };
    expect(
      resolveBrandMetadata(
        canonicalEffectiveThemeFallback(),
        null,
        "canonical",
      ).faviconHref,
    ).toBe("/bowhead/bowhead-icon.svg");

    const custom = customBundle();
    custom.brand!.applicationName = "Bowhead";
    custom.brand!.favicon = { kind: "generatedInitial", text: "<" };
    const first = resolveBrandMetadata(custom, " Dashboard ", "live");
    const second = resolveBrandMetadata(custom, " Dashboard ", "live");
    expect(first.documentTitle).toBe("Dashboard · Bowhead");
    expect(first.faviconHref).toBe(second.faviconHref);
    expect(decodeURIComponent(first.faviconHref)).toContain("&lt;");
    expect(decodeURIComponent(first.faviconHref)).not.toContain(
      ">><text",
    );

    // Legacy no-resolver gateway bundle: no `brand`, only `team.brandName`. The title falls back to the
    // canonical brand, so the favicon must be the canonical icon too, not a generated "B".
    const legacy = canonicalEffectiveThemeFallback();
    delete legacy.brand;
    legacy.team = { brandName: null };
    const legacyMetadata = resolveBrandMetadata(legacy, null, "live");
    expect(legacyMetadata.documentTitle).toBe(`${PRODUCT_NAME} — Verified Intelligence`);
    expect(legacyMetadata.faviconHref).toBe("/bowhead/bowhead-icon.svg");

    custom.brand!.favicon = {
      kind: "asset",
      artifactId: `art_${"3".repeat(24)}`,
      path: `/api/v1/branding/assets/art_${"3".repeat(24)}`,
    };
    expect(resolveBrandMetadata(custom, null, "live").faviconHref).toBe(
      `/bowhead/api/v1/branding/assets/art_${"3".repeat(24)}`,
    );
  });

  it("owns fixed nodes, removes only legacy branding nodes and is idempotent", async () => {
    const legacyPublic = document.createElement("link");
    legacyPublic.setAttribute("data-bowhead-public-favicon", "");
    document.head.append(legacyPublic);
    const legacyAuthenticated = document.createElement("link");
    legacyAuthenticated.setAttribute("data-bowhead-brand-favicon", "");
    document.head.append(legacyAuthenticated);
    const unrelated = document.createElement("link");
    unrelated.rel = "icon";
    unrelated.id = "third-party-icon";
    document.head.append(unrelated);

    const state = resolveBrandMetadata(customBundle(), "Q3", "live");
    const first = applyOwnedDocumentMetadata(state);
    expect(first).toEqual({
      titleCreated: true,
      titleChanged: true,
      faviconCreated: true,
      faviconChanged: true,
      legacyNodesRemoved: 2,
    });
    expect(document.getElementById("third-party-icon")).toBe(unrelated);
    expect(
      document
        .getElementById(BRAND_METADATA_TITLE_ID)
        ?.hasAttribute(BRAND_METADATA_OWNED_ATTR),
    ).toBe(true);
    expect(
      document.getElementById(BRAND_METADATA_TITLE_ID)?.textContent,
    ).toBe("Q3 · Acme Intelligence");

    await Promise.resolve();
    const mutations: MutationRecord[] = [];
    const observer = new MutationObserver((records) => mutations.push(...records));
    observer.observe(document.head, {
      attributes: true,
      childList: true,
      characterData: true,
      subtree: true,
    });

    const second = applyOwnedDocumentMetadata(state);
    await Promise.resolve();
    observer.disconnect();

    expect(second).toEqual({
      titleCreated: false,
      titleChanged: false,
      faviconCreated: false,
      faviconChanged: false,
      legacyNodesRemoved: 0,
    });
    expect(mutations).toEqual([]);
  });

  it("recovers a missing owned node without replacing the surviving node", () => {
    const state = resolveBrandMetadata(customBundle(), null, "live");
    applyOwnedDocumentMetadata(state);
    const title = document.getElementById(BRAND_METADATA_TITLE_ID);
    document.getElementById(BRAND_METADATA_FAVICON_ID)?.remove();

    const result = applyOwnedDocumentMetadata(state);

    expect(result.titleCreated).toBe(false);
    expect(result.titleChanged).toBe(false);
    expect(result.faviconCreated).toBe(true);
    expect(document.getElementById(BRAND_METADATA_TITLE_ID)).toBe(title);
  });
});
