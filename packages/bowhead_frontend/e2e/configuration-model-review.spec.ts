import { expect, test } from "@playwright/test";

for (const scenario of [
  { name: "desktop-en", width: 1440, locale: "en-US" },
  { name: "mobile-zh", width: 412, locale: "zh-CN" },
]) {
  test.describe(scenario.name, () => {
    test.use({ viewport: { width: scenario.width, height: 1000 }, locale: scenario.locale });
    test("loads a Configuration Agent draft and saves only after original review", async ({ page }) => {
      await page.context().addCookies([{ name: "NEXT_LOCALE", value: scenario.locale, domain: "localhost", path: "/" }]);
      await page.context().route(/\/api\/v1\/auth\/me$/, async (route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...await response.json(), team_role: "owner", is_team_admin: true, ui_language: scenario.locale } });
      });
      let config = {
        schema_version: 2,
        providers: { demo: { type: "openai_compatible", base_url: "https://example.test/v1", models: [{ id: "demo", capabilities: ["chat", "tool"] }] } },
        tiers: { primary: { provider: "demo", model: "demo" }, fast: { follow: "primary" } as Record<string, string> },
        kb: { models: {}, active: {} }, media: { models: {}, active: {} }, search: { models: {}, active: {} },
      };
      const candidate = structuredClone(config);
      candidate.tiers.fast = { provider: "demo", model: "demo" };
      const writes: Record<string, unknown>[] = [];
      await page.context().route("**/api/v1/config-proposals/ccd_models/model-review", (route) => route.fulfill({ json: { proposal_id: "ccd_models", version: 7, config: candidate } }));
      await page.context().route("**/api/v1/config-proposals/ccd_models", (route) => route.fulfill({ json: { proposal_id: "ccd_models", form_id: "team_model_config", status: writes.length ? "applied" : "proposed", workspace_id: null } }));
      await page.context().route("**/api/v1/admin/llm-config/validate", (route) => route.fulfill({ json: { valid: true, version: 7, issues: [], has_changes: true, changed_sections: ["tiers"] } }));
      await page.context().route("**/api/v1/admin/llm-config", async (route) => {
        if (route.request().method() === "PUT") {
          const body = route.request().postDataJSON();
          writes.push(body);
          config = body.config;
        }
        await route.fulfill({ json: { version: writes.length ? 8 : 7, config, providers: [], features: {} } });
      });
      await page.goto("/team/settings/models/?configuration_proposal=ccd_models", { waitUntil: "domcontentloaded" });
      const review = page.getByRole("button", { name: /^(Review and apply|检查并应用)$/ });
      await expect(review).toBeVisible();
      expect(writes).toHaveLength(0);
      await review.click();
      const apply = page.getByRole("button", { name: /^(Apply once|一次应用)$/ });
      await expect(apply).toBeVisible();
      expect(writes).toHaveLength(0);
      await apply.click();
      await expect.poll(() => writes.length).toBe(1);
      expect(writes[0].configuration_proposal_id).toBe("ccd_models");
      expect(config.tiers.fast).toEqual(candidate.tiers.fast);
      expect(writes[0].expected_version).toBe(7);
      await expect(review).toBeHidden();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const readback = await page.context().newPage();
      await readback.goto("/team/settings/models/assignments/");
      await readback.getByRole("button", { name: /^(Advanced:|进阶：)/ }).click();
      await expect(readback.locator("select").filter({ has: readback.locator("option:checked[value=\"demo::demo\"]") })).toHaveCount(2);
    });
  });
}
