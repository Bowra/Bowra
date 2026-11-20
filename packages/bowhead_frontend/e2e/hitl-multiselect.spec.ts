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

test("a paused document restores selected answers, custom text and progress after reload", async ({ page }) => {
  await mockAuthedApp(page);
  const sessionId = "s_document_question";
  const turnId = "t_document_question";
  const requestId = "c_document_question";
  const timestamp = "2026-11-18T04:00:00Z";
  const calls: ResolveCall[] = [];
  let settled = false;
  await page.route(`**/api/v1/sessions/${sessionId}`, (route) => route.fulfill({ json: {
    session: { id: sessionId, workspace_id: "ws_smoke", title: "Word 草稿", started_at: timestamp, last_active_at: timestamp },
    turns: [{
      id: turnId, seq: 1, user_text: "生成一份内部简报", intent: "open_qa", status: settled ? "completed" : "clarification_pending",
      execution_generation: 1, started_at: timestamp, ended_at: timestamp, agent_type: "lead", attachments: [], trace_steps: [],
    }],
  } }));
  await page.route(`**/api/v1/turns/${turnId}/events`, (route) => route.fulfill({ json: {
    events: [
      { seq: 1, event_type: "clarification_required", payload: {
        turn_id: turnId, clarification_id: requestId, purpose: "document_draft", questions: [
          { question: "读者是谁？", options: ["内部团队/同事", "客户"], type: "single_select" },
          { question: "需要补充什么？", options: [], type: "free_text" },
        ],
      } },
      { seq: 2, event_type: "done", payload: { turn_id: turnId, status: settled ? "completed" : "clarification_pending" } },
    ],
  } }));
  await page.route(`**/api/v1/clarifications/turn/${turnId}`, (route) =>
    route.fulfill({ json: { items: settled ? [] : [{ request_id: requestId }] } }));
  await page.route(`**/api/v1/clarifications/${requestId}/resolve`, (route) => {
    calls.push(route.request().postDataJSON() as ResolveCall);
    settled = true;
    return route.fulfill({ json: { ok: true, action: "turn_settled", request_id: requestId } });
  });

  await page.goto(`/?session=${sessionId}`);
  const card = page.getByTestId("clarification-card");
  const audience = card.getByRole("radio", { name: "内部团队/同事" });
  const detail = card.getByRole("textbox", { name: /需要补充什么/ });
  await audience.click();
  await page.reload();
  await expect(audience).toBeChecked();
  await expect(card).toContainText(/已回答\s*1\s*\/\s*2/);
  await detail.fill("补上风险和负责人");
  await detail.press("Enter");
  await page.reload();
  await expect(audience).toBeChecked();
  await expect(detail).toHaveValue("补上风险和负责人");
  await expect(card).toContainText(/已回答\s*2\s*\/\s*2/);
  expect(calls).toHaveLength(0);
  await card.getByRole("button", { name: /继续/ }).click();
  await expect.poll(() => calls.length).toBe(1);
  expect(calls[0].answers).toEqual([
    { question: "读者是谁？", answer: "内部团队/同事", source: "candidate", option_index: 0 },
    { question: "需要补充什么？", answer: "补上风险和负责人", source: "custom" },
  ]);
});

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
