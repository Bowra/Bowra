import { expect, test } from "@playwright/test";

for (const width of [1440, 768, 390]) {
  test(`empty model response details remain readable at ${width}px`, async ({ page }, testInfo) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width, height: 900 });
    await page.context().addCookies([{ name: "NEXT_LOCALE", value: "zh-CN", domain: "localhost", path: "/" }]);
    await page.context().route(/\/api\/v1\/auth\/me$/, async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...await response.json(), team_role: "owner", is_team_admin: true, ui_language: "zh-CN" } });
    });
    const item = { provider_id: "llmp_test", provider_display_name: "Test service", model_id: "test-model", capability: "chat", testability: "testable", provider_request_count: 1, testability_contract_version: "v1", probe_contract_version: "v1", probe_execution_policy_version: "v1" };
    const diagnostics = {
      request: { model: "test-model", max_tokens: 8, thinking: { type: "disabled" } },
      response_summary: { model: "test-model", stop_reason: "max_tokens", usage: { output_tokens: 8 }, content_types: ["thinking"] },
      response_body: JSON.stringify({ content: [], stop_reason: "max_tokens", metadata: "x".repeat(500) }, null, 2),
      response_truncated: true,
    };
    await page.context().route("**/api/v1/admin/llm-config", (route) => route.fulfill({ json: {
      config: {}, version: 0, providers: [], provider_identities: {}, features: { bowra_connect: true, model_discovery_v2: true },
    } }));
    await page.context().route("**/api/v1/admin/llm-config/bowra/imports/test-import", (route) => route.fulfill({ json: { base_url: "https://models.example/v1", api_key: "synthetic-key", models: ["test-model"] } }));
    await page.context().route("**/api/v1/admin/llm-config/providers/precheck", (route) => route.fulfill({ json: { provider_id: "llmp_test" } }));
    await page.context().route("**/api/v1/admin/llm-config/verification-plans", (route) => route.fulfill({ json: {
      plan_id: "plan", confirmation_token: "token", expires_at: "2099-01-01", item_count: 1, provider_request_count: 1, max_concurrency: 1, notices: [], items: [item],
    } }));
    await page.context().route("**/api/v1/admin/llm-config/verification-batches?*", (route) => route.fulfill({ json: { batches: [] } }));
    await page.context().route("**/api/v1/admin/llm-config/verification-batches", (route) => route.fulfill({ status: 202, json: { batch_id: "batch", plan_id: "plan", state: "completed", revision: 2 } }));
    await page.context().route("**/api/v1/admin/llm-config/verification-batches/batch*", (route) => route.fulfill({ json: {
      batch_id: "batch", plan_id: "plan", state: "completed", revision: 2, base_config_version: 0, counts: { failed: 1 },
      items: [{ ...item, item_id: "item", ordinal: 0, state: "failed", revision: 2, safe_code: "empty_completion", safe_http_status: 200, diagnostics }],
    } }));
    await page.goto("/team/settings/models/?bowra_import=test-import", { waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "确认并开始验证", exact: true }).click();
    await expect(page.getByText("模型服务已响应，但没有可显示的回答。请查看详情，进一步排查原因。")).toBeVisible();
    const details = page.locator(".verification-response-details");
    await expect(details).not.toHaveAttribute("open");
    const summary = details.locator("summary");
    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(details).toHaveAttribute("open", "");
    await expect(details.getByText("实际返回的模型")).toBeVisible();
    await expect(details.getByText(/这次测试达到了回复长度上限/)).toBeVisible();
    await expect(details.getByText(/返回内容超过 64 KiB/)).toBeVisible();
    await expect(details.getByText(/关闭结果或刷新页面后/)).toBeVisible();
    await expect(details.getByText(/不含密钥或完整请求/)).toBeVisible();
    let copied = "";
    await page.exposeFunction("captureDiagnosticCopy", (text: string) => { copied = text; });
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: (text: string) => (window as unknown as { captureDiagnosticCopy: (text: string) => Promise<void> }).captureDiagnosticCopy(text) } });
    });
    await details.getByRole("button", { name: "复制排查详情" }).click();
    await expect.poll(() => copied).toBe(JSON.stringify(diagnostics, null, 2));
    await expect(details.getByRole("status")).toHaveText("已复制");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await details.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`diagnostics-${width}.png`), fullPage: true });
    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(details).not.toHaveAttribute("open");
  });
}
