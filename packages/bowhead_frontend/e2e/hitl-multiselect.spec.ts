import { expect, test, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * ClarificationCard (Chat Cards Spec §1, D2) — multi_select grouped + Continue, and single_select
 * select-then-Continue.
 * Pure mock mode.
 */

type ResolveCall = {
  chosen_value?: string;
  chosen_values?: string[];
  answers?: Array<
    | {
        question: string;
        source: "candidate" | "custom";
        answer: string;
        option_index?: number;
      }
    | { question: string; source: "skip"; skipped: true }
  >;
  skip?: boolean;
};

async function captureResolve(page: Page): Promise<ResolveCall[]> {
  const calls: ResolveCall[] = [];
  await page.route("**/api/v1/clarifications/*/resolve", async (route) => {
    let body: ResolveCall = {};
    try {
      body = route.request().postDataJSON() as ResolveCall;
    } catch {
      /* non-JSON */
    }
    calls.push(body);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, action: "noop", request_id: "x", workspace_id: "ws_smoke" }),
    });
  });
  return calls;
}

test("multi_select: 多问题分组 + 累积勾选 + 提交计数 + answers/chosen_values", async ({ page }) => {
  await mockAuthedApp(page);
  const calls = await captureResolve(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?scenario=hitl_ms");

  // Two questions form separate groups (not collapsed).
  await expect(page.getByText("主要看哪个区域?")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("重点看哪些指标?")).toBeVisible();
  await expect(page.locator(".ui-card-question__head b").first()).toHaveText("问题 1");

  // Initially: submission disabled, no count.
  const submit = page.locator('[data-testid="clarification-card"] .settings-button--primary');
  await expect(submit).toBeDisabled();
  await expect(submit).toHaveText("继续");

  // Select 3 items across two groups.
  await page.locator(".ui-card-option", { hasText: "华东" }).first().click();
  await page.locator(".ui-card-option", { hasText: "华南" }).first().click();
  await page.locator(".ui-card-option", { hasText: "销售额" }).first().click();

  // Submission enabled with count; each group has a live gmeta count.
  await expect(submit).toBeEnabled();
  await expect(submit).toHaveText("继续（已回答 2 个）");
  await expect(page.locator(".ui-card-option.is-on")).toHaveCount(3);

  await page.screenshot({ path: "shot-ms-1440.png", fullPage: true });

  await submit.click();
  await expect.poll(() => calls.length, { timeout: 10_000 }).toBeGreaterThan(0);
  // >1 question → chosen_values carry the question prefix (Lead keeps the mapping)
  expect(new Set(calls[0]?.chosen_values)).toEqual(
    new Set(["主要看哪个区域?: 华东", "主要看哪个区域?: 华南", "重点看哪些指标?: 销售额"]),
  );
  expect(calls[0]?.answers).toEqual([
    { question: "主要看哪个区域?", source: "candidate", answer: "华东", option_index: 1 },
    { question: "主要看哪个区域?", source: "candidate", answer: "华南", option_index: 2 },
    { question: "重点看哪些指标?", source: "candidate", answer: "销售额", option_index: 0 },
  ]);
  expect(calls[0]?.chosen_value).toBeUndefined();
});

test("multi_select: 移动 412 渲染", async ({ page }) => {
  await mockAuthedApp(page);
  await captureResolve(page);
  await page.setViewportSize({ width: 412, height: 915 });
  await page.goto("/?scenario=hitl_ms");
  await expect(page.locator(".ui-card-option", { hasText: "全国" }).first()).toBeVisible({ timeout: 20_000 });
  await page.locator(".ui-card-option", { hasText: "全国" }).first().click();
  await expect(page.locator('[data-testid="clarification-card"] .settings-button--primary')).toBeEnabled();
  await page.screenshot({ path: "shot-ms-412.png", fullPage: true });
});

test("multi_select: 每题可自定义回答，没选的题留空交给模型", async ({ page }) => {
  await mockAuthedApp(page);
  const calls = await captureResolve(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?scenario=hitl_ms");

  await expect(page.getByText("主要看哪个区域?")).toBeVisible({ timeout: 20_000 });
  await page.locator(".ui-card-option-input").first().fill("华中");

  const submit = page.locator('[data-testid="clarification-card"] .settings-button--primary');
  await expect(submit).toBeEnabled();
  await expect(submit).toHaveText("继续（已回答 1 个）");
  await submit.click();

  await expect.poll(() => calls.length, { timeout: 10_000 }).toBeGreaterThan(0);
  expect(new Set(calls[0]?.chosen_values)).toEqual(new Set(["主要看哪个区域?: 华中"]));
  expect(calls[0]?.answers).toEqual([
    { question: "主要看哪个区域?", source: "custom", answer: "华中" },
  ]);
});

test("single_select: 点选只是选中，「继续」才提交 chosen_value（D2）", async ({ page }) => {
  await mockAuthedApp(page);
  const calls = await captureResolve(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?scenario=hitl_single");

  // A single question becomes the heading; picking only selects.
  await expect(page.getByText("您想分析哪个范围?")).toBeVisible({ timeout: 20_000 });
  await page.locator(".ui-card-option", { hasText: "重点产品表现" }).first().click();
  await page.waitForTimeout(300);
  expect(calls).toHaveLength(0);
  await page.getByRole("button", { name: "继续" }).click();
  await expect.poll(() => calls.length, { timeout: 10_000 }).toBeGreaterThan(0);
  expect(calls[0]?.chosen_value).toBe("重点产品表现");
  expect(calls[0]?.chosen_values).toBeUndefined();

  await page.screenshot({ path: "shot-single-1440.png", fullPage: true });
});

test("single_select: 可输入自定义回答并提交", async ({ page }) => {
  await mockAuthedApp(page);
  const calls = await captureResolve(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?scenario=hitl_single");

  await expect(page.getByText("您想分析哪个范围?")).toBeVisible({ timeout: 20_000 });
  await page.locator(".ui-card-option-input").fill("按 KA 客户复盘");
  await page.getByRole("button", { name: "继续" }).click();

  await expect.poll(() => calls.length, { timeout: 10_000 }).toBeGreaterThan(0);
  expect(calls[0]?.chosen_value).toBe("按 KA 客户复盘");
  expect(calls[0]?.chosen_values).toBeUndefined();
});

test("single_select: 可跳过整张澄清卡片", async ({ page }) => {
  await mockAuthedApp(page);
  const calls = await captureResolve(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?scenario=hitl_single");

  await expect(page.getByText("您想分析哪个范围?")).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "跳过，让模型判断" }).click();

  await expect.poll(() => calls.length, { timeout: 10_000 }).toBeGreaterThan(0);
  expect(calls[0]?.skip).toBe(true);
  expect(calls[0]?.chosen_value).toBeUndefined();
  expect(calls[0]?.chosen_values).toBeUndefined();
});
