import { expect, test, type Page } from "@playwright/test";

import { CHART_CATALOG, chartResult } from "../__tests__/fixtures/agent-charts";
import { dashboardAgent, dashboardCollection } from "../__tests__/fixtures/agent-dashboard";
import { mockAuthedApp } from "./_app-mocks";

/**
 * In `next dev`, the first request for a route compiles it, and that compile
 * makes Fast Refresh fully reload every page still connected to the HMR
 * socket. A `page.goto` issued from an app page therefore races a
 * `location.reload()` of the page being left ("interrupted by another
 * navigation" to the old URL). Leaving through about:blank closes the old
 * page's HMR socket before the next route compiles, so nothing can reload.
 */
async function gotoFromBlank(page: Page, url: string) {
  await page.goto("about:blank");
  await page.goto(url);
}

// The charts "collecting" date is shown in the viewer's zone; pin it so the asserted day is stable on any host.
test.use({ timezoneId: "UTC" });

for (const viewport of [{ width: 1280, height: 800 }, { width: 320, height: 780 }]) {
  test(`Agent audit recovery and theme contracts at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await mockAuthedApp(page, { subagentManagement: true });
    await page.route("**/api/v1/agents/catalog**", route => route.fulfill({ json: CHART_CATALOG }));
    await page.route("**/api/v1/agents/manage?**", route => route.fulfill({ json: dashboardCollection([dashboardAgent("one")]) }));
    await page.route("**/api/v1/agents/release-inbox/count", route => route.fulfill({ json: { kind: "agent_release_inbox_count", schema_version: 1, count: 0 } }));
    await page.route("**/api/v1/agents/rankings?**", route => {
      const kind = new URL(route.request().url()).searchParams.get("ranking_kind") === "trending" ? "trending" : "popular";
      return route.fulfill({ json: { ...chartResult(kind), state: "insufficient_sample", reason: "window_incomplete", available_at: "2026-11-02T12:00:00Z", rows: [] } });
    });
    await page.route("**/api/v1/team-subagents/builtins", route => route.fulfill({ json: {
      kind: "builtin_subagent_list", schema_version: 1, manageable: true, items: [{
        name: "consultant", enabled: true, model: "analyst", description: "Internal routing payload instructions",
        display_name: "", max_iterations: 100, primary_capability: "cap_presentation", primary_capability_options: ["cap_presentation"],
        skills: ["team-sop"], prompt_append: "", defaults: { model: "analyst", description: "Internal routing payload instructions", max_iterations: 100, primary_capability: "cap_presentation" },
        overridden_fields: ["skills"], override: { skills: ["team-sop"] }, row_version: 1, etag: '"builtin:consultant:1"', disable_warning: null,
      }],
    } }));
    let skillsUnavailable = true;
    await page.route("**/api/v1/team-subagents/skills", route => skillsUnavailable
      ? route.fulfill({ status: 503, json: { detail: "Unavailable" } })
      : route.fulfill({ json: { builtin: [{ name: "pptx", description: "Presentation method" }], team_skills: [] } }));

    await page.goto("/agents/marketplace/");
    await expect(page.getByRole("heading", { name: "Directory First" })).toBeVisible();
    await page.waitForURL(/agents\/marketplace\/?$/);
    await page.screenshot({ path: testInfo.outputPath("marketplace.png"), fullPage: true });

    await gotoFromBlank(page, "/team/settings/subagents/");
    const row = page.getByTestId("builtin-row-consultant");
    // The built-in's display name is product copy (now 「战略演示顾问」); match on its role, not the exact name.
    const heading = row.getByRole("heading");
    await expect(heading).toBeVisible();
    const displayName = (await heading.textContent())?.trim() ?? "";
    expect(displayName).toContain("顾问");
    await expect(row.locator(".builtin-agent-description")).toContainText("演示");
    await page.screenshot({ path: testInfo.outputPath("builtins.png"), fullPage: true });
    // Configuration lives in the row ⋯ (polish spec 2d); focus returns to that trigger.
    const configure = row.getByRole("button", { name: `${displayName} 的更多操作`, exact: true });
    await configure.click();
    await page.getByRole("menuitem", { name: "配置", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: `配置 ${displayName}` });
    await dialog.getByRole("textbox", { name: "追加团队口径" }).fill("Keep this draft");
    const retry = dialog.getByRole("button", { name: "重试加载技能" });
    await expect(retry).toBeVisible();
    await retry.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("skills-error.png") });
    skillsUnavailable = false;
    await retry.click();
    await expect(dialog.getByRole("checkbox", { name: /pptx/ })).toBeVisible();
    await expect(dialog.getByRole("checkbox", { name: "team-sop", exact: true })).toBeChecked();
    await expect(dialog.getByRole("textbox", { name: "追加团队口径" })).toHaveValue("Keep this draft");
    await page.screenshot({ path: testInfo.outputPath("skills-recovered.png") });
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await expect(configure).toBeFocused();

    await gotoFromBlank(page, "/agents/charts/");
    await expect(page.getByText(/统计窗口最早可于.*2026年11月2日/)).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("charts-collecting.png"), fullPage: true });
    await page.route("**/api/v1/agents/rankings?**", route => route.fulfill({ json: chartResult("trending") }));
    await page.getByRole("radio", { name: "上升", exact: true }).click();
    await expect(page.getByText("+8 位活跃成员")).toBeVisible();
    await page.evaluate(() => { document.documentElement.dataset.themeRadius = "sharp"; });
    const radius = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--r-4").trim());
    await expect(page.locator(".ac-card").first()).toHaveCSS("border-top-left-radius", viewport.width <= 520 ? "0px" : radius);

    await gotoFromBlank(page, "/agents/dashboard/");
    await expect(page.getByRole("region", { name: "领域覆盖" })).toBeVisible();
    await page.evaluate(() => { document.documentElement.dataset.themeRadius = "sharp"; document.documentElement.dataset.theme = "dark"; });
    await expect(page.locator(".ad-panel").first()).toHaveCSS("border-top-left-radius", radius);
    await expect(page.locator(".ad-metrics")).toHaveCSS("border-top-left-radius", radius);
    await page.screenshot({ path: testInfo.outputPath("dashboard-dark-sharp.png"), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  });
}
