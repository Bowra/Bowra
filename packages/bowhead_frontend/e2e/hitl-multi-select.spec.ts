import { expect, test } from "@playwright/test";

/**
 * D8 e2e: V2.7 P1/P2 ClarificationCard multi-select (kind=options/task_plan).
 *
 * Mock SSE_MODE; scenarios.ts injects hitl_options + hitl_task_plan, pickScenarioFromText
 * selects scenes with __e2e_hitl_*__ magic tokens.
 *
 * Do not test backend resolve-route network layer (mock sends nothing; click must simply not throw).
 *
 * Note (2026-04-28): V2.7 backend shipped, frontend multi-select UI not built (memory:V2.7 HITL extension
 * frontend multi-select UI pending); ClarificationCard absent, skip until implemented.
 */

const STAB_TIMEOUT = 8_000;

test.skip("kind=options · 多选 chip + 确认 N 项", async ({ page }) => {
  await page.goto("/");

  // Enter magic token in composer to trigger hitl_options scenario.
  const composer = page.locator("textarea, input[type=text]").first();
  await composer.fill("__e2e_hitl_options__");
  // Find Send: reuse existing Enter-to-send behavior.
  await composer.press("Enter");

  // ClarificationCard appears (data-kind=options).
  const card = page.locator('.v2-clarification[data-kind="options"]');
  await expect(card).toBeVisible({ timeout: STAB_TIMEOUT });

  // All 5 options render.
  const opts = card.locator(".options-multi li");
  await expect(opts).toHaveCount(5);

  // Confirm initially disabled (picked 0).
  const confirm = card.locator(".actions .primary");
  await expect(confirm).toBeDisabled();

  // Pick 2 → Confirm label becomes "确认 (2)" and enabled.
  await opts.nth(0).click();
  await opts.nth(2).click();
  await expect(confirm).toContainText("确认 (2)");
  await expect(confirm).toBeEnabled();

  // Selection state (.on class).
  await expect(opts.nth(0)).toHaveClass(/on/);
  await expect(opts.nth(1)).not.toHaveClass(/on/);
  await expect(opts.nth(2)).toHaveClass(/on/);

  // Rank: first pick = #1, second = #2.
  await expect(opts.nth(0).locator(".rank")).toContainText("#1");
  await expect(opts.nth(2).locator(".rank")).toContainText("#2");
});


test.skip("kind=task_plan · 4 子任务 + ↑↓ 排序 + '用默认前 3' 兜底", async ({ page }) => {
  await page.goto("/");

  const composer = page.locator("textarea, input[type=text]").first();
  await composer.fill("__e2e_hitl_task_plan__");
  await composer.press("Enter");

  const card = page.locator('.v2-clarification[data-kind="task_plan"]');
  await expect(card).toBeVisible({ timeout: STAB_TIMEOUT });

  const opts = card.locator(".options-multi li");
  await expect(opts).toHaveCount(4);

  // task_plan has separate "用默认 (前 3)" fallback button.
  const defaultBtn = card.locator(".actions .secondary");
  await expect(defaultBtn).toContainText("用默认");

  // Pick 2 (idx 1 → idx 0); expect #1=idx1, #2=idx0.
  await opts.nth(1).click();
  await opts.nth(0).click();
  await expect(opts.nth(1).locator(".rank")).toContainText("#1");
  await expect(opts.nth(0).locator(".rank")).toContainText("#2");

  // Only task_plan has ↑↓ reorder buttons.
  const upBtn = opts.nth(0).locator(".reord-btn").first();
  await expect(upBtn).toBeVisible();
  // Move idx 0 (currently #2) up → swaps to #1.
  await upBtn.click();
  await expect(opts.nth(0).locator(".rank")).toContainText("#1");
  await expect(opts.nth(1).locator(".rank")).toContainText("#2");
});
