import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";
import { routeTerminalChatExecution } from "./_composer-recovery-fixtures";

test("an editable answer restores the saved assumptions in the current and an older turn", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockAuthedApp(page);
  await routeTerminalChatExecution(page, () => "legacy_v0");
  const sessionId = "s_calculator";
  const turnId = "t_calculator";
  const question = "单价 100，单位变动成本 60，固定成本 12000，请给我一个盈亏平衡试算。";
  const answer = "盈亏平衡销量为 300 件。";
  const now = "2026-11-25T00:00:00Z";
  const card = { kind: "break_even", version: 1, initial: { unit_price: "100", unit_variable_cost: "60", fixed_costs: "12000" } };
  const events = [
    { event_type: "turn_started", payload: { user_text: question, session_id: sessionId, execution_generation: 1 } },
    { event_type: "step_list_card_created", payload: { card_id: turnId } },
    { event_type: "lead.final.payload", payload: { answer_card: card } },
    { event_type: "lead.final.payload", payload: { answer_card: card } },
    { event_type: "final_answer", payload: { text: answer, card_id: turnId } },
    { event_type: "done", payload: { status: "completed", response_kind: "chat", duration_ms: 1, cost_usd: 0 } },
  ].map((e, i) => ({ ...e, seq: i + 1, occurred_at: now, payload: { ...e.payload, seq: i + 1, turn_id: turnId } }));
  const first = { id: turnId, seq: 1, user_text: question, intent: "open_qa", status: "completed", execution_generation: 1, started_at: now, ended_at: now, final_answer: answer, trace_steps: [], summary: null, attachments: [] };
  let older = false;
  let sends = 0;
  await page.route("**/api/v1/chat/send", route => {
    sends++;
    return route.fulfill({ json: { session_id: sessionId, turn_id: turnId, stream_url: `/api/v1/chat/stream/${turnId}`, status: "started", execution_generation: 1 } });
  });
  await page.route("**/api/v1/chat/stream/*", route => route.fulfill({
    contentType: "text/event-stream",
    headers: { "Access-Control-Allow-Origin": new URL(page.url()).origin, "Access-Control-Allow-Credentials": "true" },
    body: events.map(e => `id: ${e.seq}\nevent: ${e.event_type}\ndata: ${JSON.stringify(e.payload)}\n\n`).join(""),
  }));
  await page.route("**/api/v1/turns/*/events", route => route.fulfill({ json: { events: route.request().url().includes(turnId) ? events : [] } }));
  await page.route(`**/api/v1/sessions/${sessionId}`, route => route.fulfill({ json: {
    session: { id: sessionId, workspace_id: "ws_smoke", title: question, started_at: now, last_active_at: now, status: "completed", cost_usd: 0, tokens_total: 0, intents: [] },
    turns: older ? [first, { ...first, id: "t_later_text", seq: 2, user_text: "继续", final_answer: "好的。" }] : [first],
  } }));
  await page.goto("/");
  const composer = page.locator(".v2-composer");
  await composer.locator("textarea").fill(question);
  await composer.locator(".v2-send-btn").click();
  const region = page.getByRole("region", { name: "盈亏平衡试算" });
  const price = region.getByRole("textbox", { name: "销售单价" });
  const status = region.getByRole("status");
  await expect(status).toContainText("300");
  const slider = region.getByRole("slider", { name: "调整销售单价" });
  await slider.focus();
  await slider.press("ArrowLeft");
  await expect(price).toHaveValue("99");
  await expect(status).toContainText("308");
  await expect(status).toContainText("+8");
  await region.getByRole("button", { name: "还原数值" }).click();
  await expect(price).toHaveValue("100");
  await price.focus();
  await price.press("ControlOrMeta+a");
  await price.press("Backspace");
  await expect(status).toContainText("请填写销售单价");
  await price.pressSequentially("90");
  await expect(status).toContainText("400");
  expect(await region.evaluate(el => el.getBoundingClientRect().right <= window.innerWidth)).toBe(true);
  await expect(status).toContainText("+100");
  for (const width of [390, 1280]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(region).toBeVisible();
    expect(await region.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await testInfo.attach(`calculator-${width}`, { body: await region.screenshot(), contentType: "image/png" });
  }
  await expect(page).toHaveURL(new RegExp(`session=${sessionId}`));
  await page.reload();
  await expect(price).toHaveValue("100");
  await expect(status).toContainText("300");
  older = true;
  await page.reload();
  await expect(page.getByText("好的。", { exact: true })).toBeVisible();
  await expect(region).toHaveCount(1);
  await expect(status).toContainText("300");
  expect(sends).toBe(1);
});
