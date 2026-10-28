import { PRODUCT_NAME } from "../lib/product-identity";
import { expect, test, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

const TITLE_ID = "bowhead-document-title";
const FAVICON_ID = "bowhead-runtime-favicon";
const COLOR_TOKEN_KEYS = [
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
  "focus",
  "positive",
  "positiveSoft",
  "warning",
  "warningSoft",
  "error",
  "errorSoft",
  "info",
  "infoSoft",
] as const;

async function expectSingleOwnedPair(page: Page) {
  await expect(page.locator(`#${TITLE_ID}`)).toHaveCount(1);
  await expect(page.locator(`#${FAVICON_ID}`)).toHaveCount(1);
  await expect(
    page.locator(
      "link[data-bowhead-public-favicon], link[data-bowhead-brand-favicon]",
    ),
  ).toHaveCount(0);
  await expect(page.locator(`#${TITLE_ID}`)).toHaveAttribute(
    "data-bowhead-metadata-owned",
    "",
  );
  await expect(page.locator(`#${FAVICON_ID}`)).toHaveAttribute(
    "data-bowhead-metadata-owned",
    "",
  );
}

async function titleText(page: Page): Promise<string | null> {
  return page
    .locator(`#${TITLE_ID}`)
    .evaluate((element) => element.textContent);
}

async function countIdleHeadMutations(page: Page): Promise<number> {
  return page.evaluate(async () => {
    let count = 0;
    const observer = new MutationObserver((records) => {
      count += records.filter((record) => {
        const target =
          record.target instanceof Element
            ? record.target
            : record.target.parentElement;
        if (target?.closest("[data-bowhead-metadata-owned]")) return true;
        return [...record.addedNodes, ...record.removedNodes].some(
          (node) =>
            node instanceof Element &&
            node.hasAttribute("data-bowhead-metadata-owned"),
        );
      }).length;
    });
    observer.observe(document.head, {
      attributes: true,
      childList: true,
      characterData: true,
      subtree: true,
    });
    await new Promise((resolve) => window.setTimeout(resolve, 500));
    observer.disconnect();
    return count;
  });
}

test("public login owns one canonical title/favicon pair without theme API", async ({
  page,
}) => {
  let effectiveThemeRequests = 0;
  page.on("request", (request) => {
    if (request.url().includes("/api/v1/theme/effective")) {
      effectiveThemeRequests += 1;
    }
  });
  await page.route("**/api/v1/bootstrap/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        required: false,
        deployment_mode: "local",
      }),
    });
  });

  await page.goto("/login/", { waitUntil: "networkidle" });

  await expectSingleOwnedPair(page);
  expect(await titleText(page)).toBe(`${PRODUCT_NAME} — Verified Intelligence`);
  await expect(page.locator(`#${FAVICON_ID}`)).toHaveAttribute(
    "href",
    /\/bowhead-icon\.svg$/,
  );
  await expect(page.locator('input[type="email"]')).toBeEditable();
  expect(effectiveThemeRequests).toBe(0);
  expect(await countIdleHeadMutations(page)).toBe(0);
});

test("authenticated hydration preserves the single fixed-node owner", async ({
  page,
}) => {
  await mockAuthedApp(page);

  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expectSingleOwnedPair(page);
  // The bootstrap script owns the pair first ("canonical"); hydration hands it to the live
  // controller once. Count idle mutations only after that hand-off, so the check measures
  // churn between owners rather than racing the single hand-off in a fast production build.
  await expect(page.locator(`#${TITLE_ID}`)).toHaveAttribute(
    "data-bowhead-metadata-source",
    "live",
  );
  await expect(page.locator(`#${FAVICON_ID}`)).toHaveAttribute(
    "data-bowhead-metadata-source",
    "live",
  );
  await expectSingleOwnedPair(page);
  expect(await titleText(page)).toBe(`${PRODUCT_NAME} — Verified Intelligence`);
  expect(await countIdleHeadMutations(page)).toBe(0);
});

test("authenticated dark mode removes public inline light tokens and colors hubs consistently", async ({
  page,
}) => {
  const colorTokens = (value: string) =>
    Object.fromEntries(COLOR_TOKEN_KEYS.map((key) => [key, value]));
  const effectiveTheme = {
    schemaVersion: 2,
    brand: {
      applicationName: "Acme",
      logoAlt: "Acme",
      logoOnLight: { kind: "text", text: "Acme" },
      logoOnDark: { kind: "text", text: "Acme" },
      mark: { kind: "generatedInitial", text: "A" },
      favicon: { kind: "generatedInitial", text: "A" },
    },
    effective: {
      version: 1,
      mode: "dark",
      colorProfile: { type: "preset", id: "bowhead" },
      colorScheme: {
        tokenSchemaVersion: 2,
        light: colorTokens("#111111"),
        dark: colorTokens("#222222"),
        checksum: `sha256:${"a".repeat(64)}`,
      },
      typographyProfile: { type: "preset", id: "system" },
      typeScale: "standard",
      density: "comfortable",
      radius: "balanced",
      motion: "standard",
      chartPalette: "default",
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
      motion: "product",
      chartPalette: "product",
    },
    userConfig: { version: 1, mode: "dark" },
    team: {
      id: "team_1",
      themeRevision: 1,
      revisionId: `tbr_${"1".repeat(24)}`,
      configurationType: "custom",
    },
  };
  await page.addInitScript((response) => {
    localStorage.setItem("bowhead.brand.active-team:v2", "team_1");
    localStorage.setItem(
      "bowhead.brand.bundle:v2:team_1",
      JSON.stringify({ version: 2, teamId: "team_1", response }),
    );
    document.documentElement.style.setProperty("--c-bg", "#FAF8F3");
    document.documentElement.style.setProperty("--surface", "#FFFFFF");
  }, effectiveTheme);
  await mockAuthedApp(page, { effectiveTheme });

  const themeReady = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/theme/effective") &&
      response.status() === 200,
  );
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await themeReady;

  await expect
    .poll(() =>
      page.evaluate(() => {
        const root = document.documentElement;
        const computed = getComputedStyle(root);
        return {
          theme: root.dataset.theme,
          inlineBackground: root.style.getPropertyValue("--c-bg"),
          inlineSurface: root.style.getPropertyValue("--surface"),
          background: computed.getPropertyValue("--c-bg").trim(),
          surface: computed.getPropertyValue("--surface").trim(),
          styleNodes: document.querySelectorAll(
            "#bowhead-team-brand-tokens",
          ).length,
        };
      }),
    )
    .toEqual({
      theme: "dark",
      inlineBackground: "",
      inlineSurface: "",
      background: "#222222",
      surface: "#222222",
      styleNodes: 1,
    });

  await page.evaluate(() => {
    document.documentElement.dataset.theme = "light";
  });
  await expect
    .poll(() =>
      page.evaluate(() =>
        getComputedStyle(document.documentElement)
          .getPropertyValue("--c-bg")
          .trim(),
      ),
    )
    .toBe("#111111");

  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  await expect
    .poll(() =>
      page.evaluate(() =>
        getComputedStyle(document.documentElement)
          .getPropertyValue("--c-bg")
          .trim(),
      ),
    )
    .toBe("#222222");

  for (const path of ["/files/", "/capabilities/"]) {
    await page.goto(path, { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("unified-sidebar")).toBeVisible();
    await expect
      .poll(() =>
        page.evaluate(() => {
          const root = document.documentElement;
          return {
            theme: root.dataset.theme,
            inlineBackground: root.style.getPropertyValue("--c-bg"),
            background: getComputedStyle(root)
              .getPropertyValue("--c-bg")
              .trim(),
          };
        }),
      )
      .toEqual({
        theme: "dark",
        inlineBackground: "",
        background: "#222222",
      });
  }
});
