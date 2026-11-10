import { expect, test } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

for (const locale of ["en-US", "zh-CN"] as const) {
  for (const history of ["live", "replay"] as const) {
    test(`shared failures, six options and selected 40-store chart: ${locale} ${history}`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.setViewportSize({ width: locale === "zh-CN" ? 400 : 1000, height: 1100 });
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.route("**/health", (route) => route.fulfill({ json: { feature_flags: { error_diagnostics: true } } }));
      await installReferenceComponent(page, "", "_tool-outcomes-entry.tsx");
      await page.goto(`/__reference_component_test?locale=${locale}&history=${history}`);
      const failedStep = page.getByRole("button", { name: /compute_fixture/ });
      const scriptFailure = page.getByText(locale === "zh-CN" ? /代码运行出错/ : /The code stopped with an error/);
      await expect(failedStep).toHaveAttribute("aria-expanded", "false");
      await expect(scriptFailure).toBeHidden();
      await failedStep.click();
      await expect(scriptFailure).toBeVisible();
      await expect(page.getByTestId("step-summary")).toContainText(locale === "zh-CN" ? "执行已结束" : "Execution ended");
      await expect(page.getByRole("note")).toContainText("Store profitability metrics");
      const failureDetail = page.getByTestId("step-failure-detail").first();
      await expect(failureDetail).toContainText(locale === "zh-CN" ? "缺少字段：bm" : "Missing key: bm");
      await expect(failureDetail).toContainText("7358d7a8095449c6a4568987c8110089");
      await expect(failureDetail).not.toContainText("tool_code_failed");
      await page.getByRole("checkbox", { name: "Traffic", exact: true }).click();
      await page.getByRole("button", { name: locale === "zh-CN" ? "继续" : "Continue", exact: true }).click();
      await expect(page.getByLabel("Submitted scope")).toHaveText("Traffic");
      await expect.poll(() => page.evaluate(() => (window as unknown as { toolOutcomeChartLabels: () => unknown[] }).toolOutcomeChartLabels().length)).toBe(40);
      await page.reload();
      await expect(failedStep).toHaveAttribute("aria-expanded", "false");
      await failedStep.click();
      await expect(scriptFailure).toBeVisible();
      await expect(page.getByTestId("step-failure-detail").first()).toContainText(locale === "zh-CN" ? "缺少字段：bm" : "Missing key: bm");
      await expect(page.getByTestId("step-summary")).toContainText(locale === "zh-CN" ? "执行已结束" : "Execution ended");
      expect(errors).toEqual([]);
      await page.screenshot({ fullPage: true, path: test.info().outputPath("tool-outcomes.png") });
    });
  }
}
