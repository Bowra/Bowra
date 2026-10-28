import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

const lightTokens = {
  background: "#F3F0FA",
  surface: "#FFFDFD",
  surface2: "#EAE5F5",
  surface3: "#DDD5EE",
  line: "#CFC5E2",
  line2: "#B7A8D2",
  ink: "#221A32",
  ink2: "#493C60",
  ink3: "#6E617F",
  ink4: "#9388A1",
  accent: "#365CF5",
  accentHover: "#2849D8",
  accentInk: "#FFFFFF",
  accentSoft: "#BFCBFF",
  accentTint: "#E7EBFF",
  focus: "#365CF5",
  positive: "#28785B",
  positiveSoft: "#D9EFE7",
  warning: "#A86618",
  warningSoft: "#F7E9D1",
  error: "#B53E4A",
  errorSoft: "#F8DEE2",
  info: "#30718E",
  infoSoft: "#DDEDF4",
};

const darkTokens = {
  background: "#111827",
  surface: "#1E293B",
  surface2: "#29364B",
  surface3: "#35445C",
  line: "#475569",
  line2: "#64748B",
  ink: "#F8FAFC",
  ink2: "#E2E8F0",
  ink3: "#CBD5E1",
  ink4: "#94A3B8",
  accent: "#F4B860",
  accentHover: "#FFD184",
  accentInk: "#1A1206",
  accentSoft: "#735423",
  accentTint: "#342919",
  focus: "#F4B860",
  positive: "#72C69F",
  positiveSoft: "#203D32",
  warning: "#E0A958",
  warningSoft: "#44331E",
  error: "#EC858C",
  errorSoft: "#4A282D",
  info: "#83BCD6",
  infoSoft: "#243B48",
};

const catalogItem = {
  kind: "agent_catalog_item",
  schema_version: 1,
  agent_id: "osa_theme",
  display_name: "Theme Auditor",
  summary: "Checks cross-surface appearance consistency.",
  source: { kind: "team", owner_label: "Design Systems", owner_user_id: null },
  facets: { domains: ["design"], jobs: ["audit"], outputs: [] },
  capabilities: [
    {
      id: "cap_theme_audit",
      label_key: "cap_theme_audit",
      label: "主题审计",
      category: "design",
    },
  ],
  risk: { level: "low", reasons: [] },
  safety: { status: "pass", checked_subject: "release", reasons: [] },
  assignment: { status: "not_hired", source: null, row_version: null },
  team_default: { enabled: false, policy_version: null, row_version: null },
  availability: {
    discoverable: true,
    hireable: true,
    runnable: false,
    reasons: ["not_hired"],
  },
  actions: {
    view_detail: true,
    hire: true,
    enable: false,
    disable: false,
    unhire: false,
    manage: false,
    manage_team_default: false,
    create: true,
  },
  action_reasons: {},
  relationship: "team",
  updated_label: { kind: "saved_at", at: "2026-09-06T00:00:00Z" },
  manager_url: null,
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
      colorScheme: {
        tokenSchemaVersion: 2,
        light: lightTokens,
        dark: darkTokens,
        checksum: `sha256:${"c".repeat(64)}`,
      },
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

async function mockCatalog(page: Page) {
  await page.route("**/api/v1/agents/catalog**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "agent_catalog_collection",
        schema_version: 1,
        items: [catalogItem],
        collections: [
          {
            id: "recently_updated",
            label_key: "collection_recently_updated",
            agent_ids: [catalogItem.agent_id],
            items: [catalogItem],
          },
        ],
        total_count: 1,
        facets: {
          capabilities: [{ ...catalogItem.capabilities[0], count: 1 }],
          domains: [{ id: "design", count: 1 }],
          jobs: [{ id: "audit", count: 1 }],
          team_tags: [],
        },
        actions: { create: true },
        next_cursor: null,
      }),
    });
  });
}

async function verifyAgentTheme(
  page: Page,
  testInfo: TestInfo,
  mode: "light" | "dark",
) {
  const tokens = mode === "light" ? lightTokens : darkTokens;
  await mockAuthedApp(page, {
    effectiveTheme: effectiveTheme(mode),
    subagentManagement: true,
  });
  await mockCatalog(page);

  await page.goto("/agents/marketplace/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { name: "Theme Auditor" }).first(),
  ).toBeVisible();
  // The brand appears only in the sidebar header (P2); the Agents page header carries the page title, not a wordmark.
  await expect(page.locator(".ui-page-header h1")).toHaveText("市场");
  await expect(page.locator(".am-page").getByText("Northstar")).toHaveCount(0);

  await expect
    .poll(() =>
      page.evaluate(() => ({
        mode: document.documentElement.dataset.theme,
        background: getComputedStyle(document.documentElement)
          .getPropertyValue("--c-bg")
          .trim(),
        surface: getComputedStyle(document.documentElement)
          .getPropertyValue("--c-surface")
          .trim(),
        accentInk: getComputedStyle(document.documentElement)
          .getPropertyValue("--c-accent-ink")
          .trim(),
        privateInk: getComputedStyle(document.querySelector(".am-page")!)
          .getPropertyValue("--am-ink")
          .trim(),
      })),
    )
    .toEqual({
      mode,
      background: tokens.background,
      surface: tokens.surface,
      accentInk: tokens.accentInk,
      privateInk: "",
    });

  await expect(page.locator(".am-card").first()).toHaveCSS(
    "background-color",
    mode === "light" ? "rgb(255, 253, 253)" : "rgb(30, 41, 59)",
  );
  await expect(page.locator(".am-hire-button").first()).toHaveCSS(
    "color",
    mode === "light" ? "rgb(255, 255, 255)" : "rgb(26, 18, 6)",
  );

  await page.screenshot({
    path: testInfo.outputPath(`agent-marketplace-${mode}.png`),
    fullPage: true,
  });
}

test("Agent marketplace consumes the canonical light token contract", async ({
  page,
}, testInfo) => {
  await verifyAgentTheme(page, testInfo, "light");
});

test("Agent marketplace consumes the canonical dark token contract", async ({
  page,
}, testInfo) => {
  await verifyAgentTheme(page, testInfo, "dark");
});
