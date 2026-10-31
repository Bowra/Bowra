import { expect, test } from "@playwright/test";

for (const count of [1, 2]) {
  test(`first Bowra connection with ${count} models requires explicit Review and Apply`, async ({ page }) => {
    test.setTimeout(45_000);
    await page.setViewportSize({ width: count === 1 ? 1440 : 412, height: 1000 });
    await page.context().addCookies([{ name: "NEXT_LOCALE", value: "en-US", domain: "localhost", path: "/" }]);
    await page.context().route(/\/api\/v1\/auth\/me$/, async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), team_role: "owner", is_team_admin: true, ui_language: "en-US" } });
    });
    let saved: Record<string, unknown> = {};
    const writes: Record<string, unknown>[] = [];
    const imported = { base_url: "https://api.bowra.test/custom", api_key: "private-imported-key", models: ["first", "second"].slice(0, count) };
    await page.context().route("**/api/v1/admin/llm-config/bowra/imports/opaque-import", (route) => route.fulfill({ json: imported }));
    await page.context().route("**/api/v1/admin/llm-config/validate", (route) => route.fulfill({ json: { valid: true, version: 0, issues: [], has_changes: true, changed_sections: ["providers", "tiers"], readiness: { as_of: "2026-10-30", draft_semantic_hash: "test-hash", may_apply: true, rows: [], blocking: [], warnings: [], required_acceptances: [], external_rows: [], baseline_will_invalidate: false } } }));
    await page.context().route("**/api/v1/admin/llm-config", async (route) => {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON();
        writes.push(body);
        saved = body.config;
      }
      await route.fulfill({ json: { config: saved, version: writes.length, providers: [], features: { bowra_connect: true, model_discovery_v2: true } } });
    });
    await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("button", { name: "Use Bowra service" })).toBeVisible({ timeout: 20_000 });
    let startHeader = "";
    await page.context().route("**/api/v1/admin/llm-config/bowra/start", async (route) => {
      startHeader = route.request().headers()["x-bowhead-public-base"] || "";
      await route.fulfill({ json: { authorize_url: "https://auth.bowra.test/connect/authorize" } });
    });
    const returnUrl = new URL("/team/settings/models/?bowra_import=opaque-import", page.url()).href;
    await page.context().route("https://auth.bowra.test/connect/authorize", (route) => route.fulfill({ status: 303, headers: { location: returnUrl } }));
    await page.getByRole("button", { name: "Use Bowra service" }).click();
    await expect(page.getByText(/Bowra configuration imported/)).toBeVisible();
    expect(startHeader).toBe(new URL(page.url()).origin);
    await expect(page).toHaveURL(/\/team\/settings\/models\/$/);
    const primary = page.locator('select[name="primary_model"]');
    await expect(primary).toHaveValue(count === 1 ? "bowra::first" : "");
    if (count === 2) await primary.selectOption("bowra::second");
    expect(writes).toHaveLength(0);
    await page.getByRole("button", { name: "Review and apply", exact: true }).click();
    expect(writes).toHaveLength(0);
    await page.getByRole("button", { name: "Apply once", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].config).toMatchObject({
      providers: { bowra: { type: "openai_compatible", base_url: imported.base_url, api_key: imported.api_key } },
      tiers: { primary: { provider: "bowra", model: count === 1 ? "first" : "second" } },
    });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.evaluate(() => Object.values(localStorage).join("") + Object.values(sessionStorage).join(""))).not.toContain(imported.api_key);
  });
}
