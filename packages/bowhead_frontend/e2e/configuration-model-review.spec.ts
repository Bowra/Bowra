import { expect, test } from "@playwright/test";

for (const scenario of [
  { name: "desktop-en", width: 1440, locale: "en-US" },
  { name: "tablet-en", width: 834, locale: "en-US" },
  { name: "mobile-zh", width: 412, locale: "zh-CN" },
]) {
  test.describe(scenario.name, () => {
    test.use({ viewport: { width: scenario.width, height: 1000 }, locale: scenario.locale });
    test("shows task assignments, keeps verification on demand, and reviews draft changes", async ({ page }, testInfo) => {
      await page.context().addCookies([{ name: "NEXT_LOCALE", value: scenario.locale, domain: "localhost", path: "/" }]);
      await page.context().route(/\/api\/v1\/auth\/me$/, async (route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...await response.json(), team_role: "owner", is_team_admin: true, ui_language: scenario.locale } });
      });
      let config = {
        schema_version: 2,
        providers: { demo: { type: "openai_compatible", base_url: "https://example.test/v1", models: [
          { id: "main", capabilities: ["chat", "tool"] }, { id: "judge", capabilities: ["chat", "tool"] },
        ] } },
        tiers: { primary: { provider: "demo", model: "main" }, fast: { follow: "primary" } as Record<string, string>, judge: { auto: true } },
        kb: { models: {}, active: {} }, media: { models: {}, active: {} }, search: { models: {}, active: {} },
      };
      let writes = 0;
      let probes = 0;
      const readiness = { rows: [], blocking: [], warnings: [], required_acceptances: [], external_rows: [], may_apply: true, baseline_will_invalidate: false, draft_semantic_hash: "hash", as_of: "2026-11-04" };
      await page.context().route("**/api/v1/admin/llm-config/verification-batches**", (route) => route.fulfill({ json: { batches: [] } }));
      await page.context().route("**/api/v1/admin/llm-config/outbound-adaptations**", (route) => route.fulfill({ json: { items: [] } }));
      await page.context().route("**/api/v1/admin/llm-config/verification-plans", (route) => { probes++; return route.fulfill({ status: 500, json: {} }); });
      await page.context().route("**/api/v1/admin/llm-config/validate", (route) => {
        const draft = route.request().postDataJSON().config as typeof config;
        const rows = ["primary", "fast", "judge"].map((role) => ({
          role_key: `tier.${role}`, provider_id: "llmp_demo", provider_display_name: "demo",
          model_id: role === "primary" ? draft.tiers.primary.model : role === "fast" ? draft.tiers.fast.model || draft.tiers.primary.model : "judge",
          capability: "chat", fact_state: "unknown", declared: true, routing_critical: true, changed: false, status: "warning",
        }));
        return route.fulfill({ json: { valid: true, version: 7 + writes, issues: [], has_changes: true, changed_sections: ["tiers"], readiness: { ...readiness, rows } } });
      });
      await page.context().route("**/api/v1/admin/llm-config", async (route) => {
        if (route.request().method() === "PUT") { writes++; config = route.request().postDataJSON().config; }
        await route.fulfill({ json: { version: 7 + writes, config, providers: [], provider_identities: { demo: "llmp_demo" }, features: { model_discovery_v2: true } } });
      });
      await page.goto("/team/settings/models/");
      await page.getByRole("tab", { name: /^(Model assignments|模型分工)$/ }).click();
      const groups = page.locator(".assignment-group");
      await expect(groups).toHaveCount(2);
      const fast = page.locator(".assignment-summary-row").filter({ hasText: /Light model|轻量模型/ });
      await expect(fast).toContainText("demo · main");
      await page.locator('a[href="#mset-overview-evidence"]').click();
      await expect(page.getByRole("tab", { name: /^(Model assignments|模型分工)$/ })).toHaveAttribute("aria-selected", "true");
      await page.getByRole("button", { name: /^(Hide verification details|收起验证详情)$/ }).click();
      await expect(page.locator("#assignment-verification-details")).toBeHidden();
      const details = page.getByRole("button", { name: /^(Show verification details|展开验证详情)$/ });
      await details.focus();
      await page.keyboard.press("Enter");
      await expect(page.locator("#assignment-verification-details")).toBeVisible();
      expect(probes).toBe(0);
      expect(writes).toBe(0);
      await page.screenshot({ path: testInfo.outputPath("assignment-overview.png"), fullPage: true });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.getByRole("button", { name: /^(Edit assignments|编辑分工)$/ }).click();
      await page.getByRole("combobox", { name: /^(Main model|主模型)$/ }).waitFor();
      await expect(page.getByRole("combobox", { name: /^(Light model|轻量模型)$/ })).toBeHidden();
      await page.getByRole("button", { name: /^(Advanced:|进阶：)/ }).click();
      await page.getByRole("combobox", { name: /^(Light model|轻量模型)$/ }).selectOption("demo::judge");
      await expect(page.getByRole("button", { name: /^(Verify current assignments|验证当前分工)$/ })).toHaveCount(0);
      const verifyDraft = page.getByRole("button", { name: /^(Verify draft assignments|验证草稿分工)$/ });
      await expect(verifyDraft).toBeEnabled();
      await verifyDraft.click();
      const selection = page.getByRole("dialog");
      await expect(selection.locator(".verification-selector-row").filter({ hasText: "judge" })).toBeVisible();
      expect(probes).toBe(0);
      expect(writes).toBe(0);
      await selection.getByRole("button", { name: /^(Close|关闭)$/ }).click();
      await expect(page.getByRole("combobox", { name: /^(Light model|轻量模型)$/ })).toHaveValue("demo::judge");
      await page.getByRole("button", { name: /^(Review and apply|检查并应用)$/ }).click();
      await expect(page.getByRole("heading", { name: /^(Assignment changes|分工修改)$/ })).toBeVisible();
      expect(writes).toBe(0);
      await page.screenshot({ path: testInfo.outputPath("assignment-review.png"), fullPage: true });
      await page.getByRole("button", { name: /^(Apply once|一次应用)$/ }).click();
      await expect.poll(() => writes).toBe(1);
      await expect(page.getByText(/^(Assignments updated\.|分工已更新。)$/)).toBeVisible();
      expect(config.tiers.fast).toEqual({ provider: "demo", model: "judge" });
      await page.getByRole("button", { name: /^(Back to overview|返回概览)$/ }).click();
      await expect(page).toHaveURL(/\/team\/settings\/models\/?#assignments$/);
      await expect(page.getByRole("tab", { name: /^(Model assignments|模型分工)$/ })).toHaveAttribute("aria-selected", "true");
      await expect(page.locator(".assignment-summary-row").filter({ hasText: /Light model|轻量模型/ })).toContainText("demo · judge");
      await page.reload();
      await expect(page.getByRole("tab", { name: /^(Model assignments|模型分工)$/ })).toHaveAttribute("aria-selected", "true");
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });

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
