import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { E2E_PERSONAL_APPEARANCE, mockAuthedApp } from "./_app-mocks";

const DOC_TYPE = {
  doc_type_id: "dt_theme_001",
  workspace_id: "ws_smoke",
  name: "主题审计表",
  description: "验证知识库宿主层跟随有效主题。",
  fields: [{
    name: "状态",
    type: "string",
    description: "当前状态",
    required: true,
    cross_verify: false,
    extraction_hint: "",
  }],
  record_count: 3,
  extractor_model: "qwen-plus",
  cross_verify_enabled: true,
  created_by: "u_smoke",
  created_at: "2026-09-06T00:00:00Z",
  updated_at: "2026-09-06T00:00:00Z",
};

function effectiveTheme(mode: "light" | "dark") {
  return {
    schemaVersion: 2,
    brand: {
      applicationName: "Northstar",
      logoAlt: "Northstar",
      logoOnLight: { kind: "text", text: "Northstar" },
      logoOnDark: { kind: "text", text: "Northstar" },
      mark: { kind: "generatedInitial", text: "N" },
      favicon: { kind: "generatedInitial", text: "N" },
    },
    effective: {
      version: 1,
      mode,
      colorProfile: { type: "preset", id: "bowhead" },
      colorScheme: E2E_PERSONAL_APPEARANCE.effective.colorScheme,
      typographyProfile: { type: "preset", id: "system" },
      typeScale: "standard",
      density: "comfortable",
      radius: "balanced",
      motion: "standard",
      chartPalette: "default",
    },
    locks: {},
    source: {
      mode: "user",
      colorProfile: "team_default",
      colorScheme: "team_default",
      typographyProfile: "team_default",
      typeScale: "product",
      density: "product",
      radius: "product",
      motion: "product",
      chartPalette: "product",
    },
    userConfig: { version: 1, mode },
    team: {
      id: "team_smoke",
      themeRevision: 1,
      revisionId: `tbr_${"1".repeat(24)}`,
      configurationType: "custom",
    },
  };
}

async function verifyKnowledgeTheme(
  page: Page,
  testInfo: TestInfo,
  mode: "light" | "dark",
) {
  await page.addInitScript(() => {
    window.localStorage.setItem("bowhead.space_id", "ws_smoke");
  });
  await mockAuthedApp(page, { effectiveTheme: effectiveTheme(mode) });
  await page.route("**/api/v1/kb/doc_types**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      // The Templates view requests `include_stats=1`; the gateway then always returns `stats`.
      body: JSON.stringify({ items: [DOC_TYPE], stats: {} }),
    });
  });

  await page.goto("/knowledge/?tab=templates", { waitUntil: "domcontentloaded" });
  const row = page.getByTestId(`kb-tpl-extraction-${DOC_TYPE.doc_type_id}`);
  await expect(row).toBeVisible();

  const expected = mode === "light"
    ? {
        background: "rgb(250, 249, 246)",
        surface: "rgb(255, 255, 255)",
        accentInk: "rgb(255, 255, 255)",
        ink: "rgb(28, 25, 23)",
      }
    : {
        background: "rgb(19, 18, 16)",
        surface: "rgb(29, 27, 24)",
        accentInk: "rgb(35, 17, 10)",
        ink: "rgb(245, 242, 235)",
      };

  await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
  await expect(page.locator("body")).toHaveCSS("background-color", expected.background);
  await expect(row).toHaveCSS("background-color", expected.surface);
  // Since the in-page Knowledge navigation release, "New template" asks for the purpose first.
  await expect(page.getByTestId("kb-tpl-new")).toHaveCSS("color", expected.accentInk);

  await page.getByTestId("kb-tpl-new").click();
  await page.getByTestId("kb-tpl-purpose-extraction").click();
  const wizard = page.getByTestId("kt-wizard");
  const nameInput = page.getByTestId("kt-name");
  await expect(wizard).toHaveCSS("background-color", expected.surface);
  await expect(nameInput).toHaveCSS("background-color", expected.surface);
  await expect(nameInput).toHaveCSS("color", expected.ink);

  await page.screenshot({
    path: testInfo.outputPath(`knowledge-tables-${mode}.png`),
    fullPage: true,
  });
}

test("Knowledge Tables consumes the effective light theme", async ({ page }, testInfo) => {
  await verifyKnowledgeTheme(page, testInfo, "light");
});

test("Knowledge Tables consumes the effective dark theme", async ({ page }, testInfo) => {
  await verifyKnowledgeTheme(page, testInfo, "dark");
});

async function verifyHomeTheme(
  page: Page,
  testInfo: TestInfo,
  mode: "light" | "dark",
) {
  await mockAuthedApp(page, { effectiveTheme: effectiveTheme(mode) });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const surface = page.locator(".home-work-surface");
  await expect(surface).toBeVisible();
  // The brand appears once, in the sidebar; the home surface carries no wordmark or brand line.
  await expect(page.locator(".unified-sidebar-brand")).toHaveAccessibleName("Northstar");
  await expect(page.locator(".unified-sidebar-brand")).toContainText("Northstar");
  await expect(page.locator(".home-work-brand-copy")).toHaveCount(0);
  await expect(surface).not.toContainText("BOWHEAD");
  await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
  await expect(page.locator("body")).toHaveCSS(
    "background-color",
    mode === "light" ? "rgb(250, 249, 246)" : "rgb(19, 18, 16)",
  );
  await expect(surface).toHaveCSS(
    "color",
    mode === "light" ? "rgb(28, 25, 23)" : "rgb(245, 242, 235)",
  );

  await page.screenshot({
    path: testInfo.outputPath(`authenticated-home-${mode}.png`),
    fullPage: true,
  });
}

test("authenticated home consumes the effective light theme", async ({ page }, testInfo) => {
  await verifyHomeTheme(page, testInfo, "light");
});

test("authenticated home consumes the effective dark theme", async ({ page }, testInfo) => {
  await verifyHomeTheme(page, testInfo, "dark");
});
