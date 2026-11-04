import { expect, test } from "@playwright/test";

for (const [count, width] of [[1, 1440], [2, 768], [2, 390]]) {
  test(`Bowra import prepares ${count} models at ${width}px independently of assignments`, async ({ page }, testInfo) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width, height: 900 });
    await page.context().addCookies([{ name: "NEXT_LOCALE", value: "en-US", domain: "localhost", path: "/" }]);
    await page.context().route(/\/api\/v1\/auth\/me$/, async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), team_role: "owner", is_team_admin: true, ui_language: "en-US" } });
    });
    let saved: Record<string, unknown> = {};
    const writes: Record<string, unknown>[] = [];
    let providerCalls = 0;
    const imported = { base_url: "https://api.bowra.test/custom/v1", api_key: "private-imported-key", models: ["first", "second"].slice(0, count) };
    const items = imported.models.flatMap((model_id) => ["chat", "tool"].map((capability) => ({
      provider_id: "llmp_bowra", provider_display_name: "Bowra", model_id, capability, testability: "testable",
      provider_request_count: 1, testability_contract_version: "v1", probe_execution_policy_version: "v1",
    })));
    const plan = { plan_id: "plan", confirmation_token: "token", expires_at: "2099-01-01", item_count: items.length, provider_request_count: items.length, max_concurrency: 3, notices: [], items };
    const features = { bowra_connect: true, model_discovery_v2: true, model_assignment_reconciliation: true };
    const response = () => ({ config: saved, version: writes.length, providers: [], provider_identities: writes.length ? { bowra: "llmp_bowra" } : {}, verification: { as_of: "2026-11-02", entries: writes.length && providerCalls ? items.map((item) => ({ ...item, fact_state: "verified" })) : [] }, features });
    await page.context().route("**/api/v1/admin/llm-config/bowra/imports/opaque-import", (route) => route.fulfill({ json: imported }));
    await page.context().route("**/api/v1/admin/llm-config/providers/precheck", (route) => route.fulfill({ json: { provider_id: "llmp_bowra" } }));
    await page.context().route("**/api/v1/admin/llm-config/verification-plans", async (route) => {
      expect(route.request().postDataJSON().items).toHaveLength(items.length);
      await route.fulfill({ json: plan });
    });
    await page.context().route("**/api/v1/admin/llm-config/verification-batches?*", (route) => route.fulfill({ json: { batches: [] } }));
    await page.context().route("**/api/v1/admin/llm-config/verification-batches", async (route) => {
      providerCalls += items.length;
      await route.fulfill({ status: 202, json: { batch_id: "batch", plan_id: "plan", state: "completed", revision: 2 } });
    });
    await page.context().route("**/api/v1/admin/llm-config/verification-batches/batch*", (route) => route.fulfill({ json: {
      batch_id: "batch", plan_id: "plan", state: "completed", revision: 2, base_config_version: 0,
      counts: { verified: items.length }, items: items.map((item, ordinal) => ({ ...item, state: "verified", item_id: String(ordinal), ordinal, revision: 2 })),
    } }));
    await page.context().route("**/api/v1/admin/llm-config/reconcile-assignments", (route) => route.fulfill({ json: response() }));
    await page.context().route("**/api/v1/admin/llm-config/validate", (route) => route.fulfill({ json: {
      valid: true, version: 0, issues: [], has_changes: true, changed_sections: ["providers"],
      readiness: { as_of: "2026-11-02", draft_semantic_hash: "a".repeat(64), may_apply: true, rows: [], blocking: [], warnings: [], required_acceptances: [], external_rows: [], baseline_will_invalidate: false },
    } }));
    await page.context().route("**/api/v1/admin/llm-config", async (route) => {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON();
        writes.push(body);
        saved = body.config;
      }
      await route.fulfill({ json: response() });
    });
    await page.goto("/team/settings/models/", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("button", { name: "Use Bowra service" })).toBeVisible({ timeout: 30_000 });
    let startHeader = "";
    await page.context().route("**/api/v1/admin/llm-config/bowra/start", async (route) => {
      startHeader = route.request().headers()["x-bowhead-public-base"] || "";
      await route.fulfill({ json: { authorize_url: "https://auth.bowra.test/connect/authorize" } });
    });
    const returnUrl = new URL("/team/settings/models/?bowra_import=opaque-import", page.url()).href;
    await page.context().route("https://auth.bowra.test/connect/authorize", (route) => route.fulfill({ contentType: "text/html", body: `<script>location.replace(${JSON.stringify(returnUrl)})</script>` }));
    await page.getByRole("button", { name: "Use Bowra service" }).click();
    await expect(page.getByRole("heading", { name: "Confirm verification plan" })).toBeVisible();
    expect(startHeader).toBe(new URL(page.url()).origin);
    await expect(page).toHaveURL(/\/team\/settings\/models\/$/);
    await expect(page.locator('select[name="primary_model"]')).toHaveCount(0);
    expect(providerCalls).toBe(0);
    expect(writes).toHaveLength(0);
    const save = page.getByRole("button", { name: "Save", exact: true });
    // Real geometry: actions stay in the viewport at both ends of a long editor.
    await page.locator('input[name="provider_name"]').first().scrollIntoViewIfNeeded().catch(() => page.evaluate(() => window.scrollTo(0, 0)));
    const box = await save.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.y + box!.height).toBeLessThanOrEqual(900);
    await page.screenshot({ path: testInfo.outputPath(`editor-${width}.png`), fullPage: true });
    await page.getByRole("button", { name: "Confirm and start", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Model capability check finished" })).toBeVisible();
    expect(providerCalls).toBe(items.length);
    await expect(save).toBeEnabled();
    await save.click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].config).toMatchObject({
      providers: { bowra: { type: "openai_compatible", base_url: imported.base_url, api_key: imported.api_key } },
      tiers: {},
    });
    await expect(page.getByRole("button", { name: "Edit connection Bowra" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Verify", exact: true })).toBeVisible();
    await expect(page.getByText("Chat · Verified", { exact: true }).first()).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.evaluate(() => Object.values(localStorage).join("") + Object.values(sessionStorage).join(""))).not.toContain(imported.api_key);
    await page.screenshot({ path: testInfo.outputPath(`connections-${width}.png`), fullPage: true });
  });
}
