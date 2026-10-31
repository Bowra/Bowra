import { expect, test } from "@playwright/test";

for (const scenario of [
  { name: "desktop-en", width: 1440, locale: "en-US", theme: "light" as const },
  { name: "mobile-zh", width: 412, locale: "zh-CN", theme: "dark" as const },
]) {
  test.describe(scenario.name, () => {
    test.use({ viewport: { width: scenario.width, height: 1000 }, locale: scenario.locale, colorScheme: scenario.theme });
    test("keeps runtime defaults visible and saves a model-call deadline", async ({ page, browserName }, testInfo) => {
      page.on("dialog", (dialog) => dialog.accept());
      await page.context().addInitScript((theme) => localStorage.setItem("bowhead.theme", theme), scenario.theme);
      await page.context().addCookies([{ name: "NEXT_LOCALE", value: scenario.locale, domain: "localhost", path: "/" }]);
      await page.context().route(/\/api\/v1\/auth\/me$/, async (route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...await response.json(), team_role: "owner", is_team_admin: true, ui_language: scenario.locale } });
      });
      await page.context().route("**/api/v1/theme/effective", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        await route.fulfill({ response, json: { ...body, effective: { ...body.effective, mode: scenario.theme } } });
      });
      const defaults = { turn_time_limit_s: 1800, step_limit: null, tool_call_limit: 300, model_call_time_limit_s: null as number | null };
      let limits = { ...defaults, defaults, runtime_controls: { contract: "agent-runtime-controls.v1", system_step_limit: 1000, effective_step_limit: 1000, stall_timeout_s: 300 }, wrap_up_reserve_s: 300, is_default: true, version: "default", ranges: {
        turn_time_limit_s: { min: 600, max: 36000 }, step_limit: { min: 1, max: 10000 },
        tool_call_limit: { min: 1, max: 10000 }, model_call_time_limit_s: { min: 30, max: 36000 },
      } };
      const writes: Record<string, unknown>[] = [];
      const executionDefaults = { sandbox_default_timeout_s: 120, sandbox_max_timeout_s: 600, html_max_bytes: 26214400 };
      let executionLimits = { ...executionDefaults, defaults: executionDefaults, is_default: true, version: "default", ranges: {
        sandbox_default_timeout_s: { min: 5, max: 600 }, sandbox_max_timeout_s: { min: 5, max: 600 }, html_max_bytes: { min: 1, max: 52428800 },
      } };
      const executionWrites: Record<string, unknown>[] = [];
      await page.context().route("**/api/v1/team-settings/execution-limits", async (route) => {
        if (route.request().method() === "PUT") {
          const body = route.request().postDataJSON();
          executionWrites.push(body);
          executionLimits = { ...executionLimits, ...(body.limits ?? executionDefaults), version: "saved-execution", is_default: body.limits === null };
        }
        await route.fulfill({ json: executionLimits });
      });
      await page.context().route("**/api/v1/team-settings/agent-run-limits", async (route) => {
        if (route.request().method() === "PUT") {
          const body = route.request().postDataJSON();
          writes.push(body);
          limits = { ...limits, ...body, version: "saved", is_default: false };
        }
        await route.fulfill({ json: limits });
      });
      await page.context().route("**/api/v1/admin/llm-config", (route) => route.fulfill({ json: {
        version: 1, providers: [], features: {}, config: {
          schema_version: 2,
          providers: { demo: { type: "openai_compatible", base_url: "https://example.test/v1", models: [{ id: "demo", capabilities: ["chat", "tool"] }] } },
          tiers: { primary: { provider: "demo", model: "demo" } }, kb: { models: {}, active: {} },
        },
      } }));

      await page.goto("/team/settings/models/assignments/", { waitUntil: "domcontentloaded" });
      const count = page.locator("#kb-retrieval-count");
      await expect(count).toHaveValue("15");
      await count.focus();
      await count.selectOption("30");
      await expect(count).toHaveValue("30");
      await expect(page.locator("#kb-retrieval-count-hint")).toContainText("15");
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await count.scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath("knowledge-count.png") });
      await page.getByRole("button", { name: /^(Discard|放弃)$/ }).click();
      await expect(page.getByRole("button", { name: /^(Review and apply|检查并应用)$/ })).toBeHidden();

      const modelsPage = page;
      page = await page.context().newPage();
      await modelsPage.close();
      await page.goto("/team/settings/subagents/", { waitUntil: "domcontentloaded" });
      await page.getByRole("tab", { name: /^(Run limits|运行上限)$/ }).click();
      await expect(page.getByText(/1000.*1000.*300/)).toBeVisible();
      const input = page.locator("#arl-model-call");
      await expect(input).toBeDisabled();
      await expect(page.getByRole("radio", { name: /^(Follow turn time|跟随单轮时长)$/ })).toHaveAttribute("aria-checked", "true");
      await page.getByRole("radio", { name: /^(At most|最多)$/ }).first().click();
      await input.fill("900");
      await input.press("Tab");
      await page.getByRole("button", { name: /^(Save|保存)$/ }).click();
      await expect.poll(() => writes.length).toBe(1);
      expect(writes[0].model_call_time_limit_s).toBe(900);
      expect(writes[0]).not.toHaveProperty("runtime_controls");
      await expect(page.getByRole("button", { name: /^(Save|保存)$/ })).toBeHidden();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await input.scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath("model-call-time.png") });
      await page.reload();
      await page.getByRole("tab", { name: /^(Run limits|运行上限)$/ }).click();
      await expect(page.locator("#arl-model-call")).toHaveValue("900");
      const sandboxDefault = page.locator("#execution-sandbox_default_timeout_s");
      const sandboxMaximum = page.locator("#execution-sandbox_max_timeout_s");
      const htmlMaximum = page.locator("#execution-html_max_bytes");
      await expect(sandboxDefault).toHaveValue("120");
      await expect(sandboxMaximum).toHaveValue("600");
      await expect(htmlMaximum).toHaveValue("25");
      await sandboxMaximum.fill("60");
      await expect(sandboxDefault).toHaveAttribute("aria-invalid", "true");
      const executionSave = page.getByRole("button", { name: /^(Save sandbox and HTML limits|保存沙箱和 HTML 上限)$/ });
      await expect(executionSave).toBeDisabled();
      await sandboxMaximum.fill("500");
      await sandboxDefault.fill("240");
      await htmlMaximum.fill("8");
      if (browserName === "chromium") {
        await htmlMaximum.press("Tab");
        await page.keyboard.press("Tab");
      } else {
        // Safari's native Tab preference can skip buttons; explicit focus still supports keyboard activation.
        await executionSave.focus();
      }
      await expect(executionSave).toBeFocused();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      expect(await page.locator(".execution-limits .agent-run-limits__rows").first().evaluate((element) => getComputedStyle(element).paddingLeft)).toBe("16px");
      await page.locator(".execution-limits").screenshot({ path: testInfo.outputPath("sandbox-html-settings.png") });
      await executionSave.press("Enter");
      await expect.poll(() => executionWrites.length).toBe(1);
      expect(executionWrites[0]).toMatchObject({ limits: { sandbox_default_timeout_s: 240, sandbox_max_timeout_s: 500, html_max_bytes: 8388608 }, expected_version: "default" });
      await expect(executionSave).toBeHidden();
      await page.reload();
      await page.getByRole("tab", { name: /^(Run limits|运行上限)$/ }).click();
      await expect(page.locator("#execution-html_max_bytes")).toHaveValue("8");
      await expect(page.locator("#execution-sandbox_default_timeout_s")).toHaveValue("240");
    });
  });
}
