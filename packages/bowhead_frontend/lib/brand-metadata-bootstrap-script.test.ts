import { PRODUCT_NAME } from "@/lib/product-identity";
import { afterEach, describe, expect, it } from "vitest";

import {
  BRAND_METADATA_FAVICON_ID,
  BRAND_METADATA_TITLE_ID,
} from "./brand-metadata";
import { CANONICAL_BRAND_METADATA_BOOTSTRAP_SCRIPT } from "./brand-metadata-bootstrap-script";

afterEach(() => {
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
  document.getElementById(BRAND_METADATA_TITLE_ID)?.remove();
  document.getElementById(BRAND_METADATA_FAVICON_ID)?.remove();
});

describe("canonical brand metadata bootstrap", () => {
  it("creates the fixed canonical nodes under the verified prefix", () => {
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: "pe_1",
      teamId: "team_1",
      baseUrl: "https://example.com/bowhead",
      basePath: "/bowhead",
      matchSource: "trusted_header",
    };

    Function(CANONICAL_BRAND_METADATA_BOOTSTRAP_SCRIPT)();

    expect(document.getElementById(BRAND_METADATA_TITLE_ID)?.textContent).toBe(
      `${PRODUCT_NAME} — Verified Intelligence`,
    );
    expect(
      document
        .getElementById(BRAND_METADATA_FAVICON_ID)
        ?.getAttribute("href"),
    ).toBe("/bowhead/bowhead-icon.svg");
  });

  it("preserves request-scoped nodes that the Gateway already initialized", () => {
    const title = document.createElement("title");
    title.id = BRAND_METADATA_TITLE_ID;
    title.textContent = "Acme — Verified Intelligence";
    title.setAttribute("data-bowhead-metadata-initialized", "");
    document.head.append(title);
    const favicon = document.createElement("link");
    favicon.id = BRAND_METADATA_FAVICON_ID;
    favicon.rel = "icon";
    favicon.href = "/api/v1/branding/assets/art_111111111111111111111111";
    favicon.setAttribute("data-bowhead-metadata-initialized", "");
    document.head.append(favicon);

    Function(CANONICAL_BRAND_METADATA_BOOTSTRAP_SCRIPT)();

    expect(title.textContent).toBe("Acme — Verified Intelligence");
    expect(favicon.getAttribute("href")).toBe(
      "/api/v1/branding/assets/art_111111111111111111111111",
    );
  });

  it("does not install metadata listeners, observers or timers", () => {
    expect(CANONICAL_BRAND_METADATA_BOOTSTRAP_SCRIPT).not.toMatch(
      /addEventListener|MutationObserver|setInterval|setTimeout/,
    );
  });
});
