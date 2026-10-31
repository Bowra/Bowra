import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";
import { routeTerminalChatExecution } from "./_composer-recovery-fixtures";

for (const runtimeLimit of [false, true]) {
test(`${runtimeLimit ? "Step limit" : "Model failure"} cause and advice survive live SSE, refresh and history`, async ({ page }) => {
  await mockAuthedApp(page);
  await routeTerminalChatExecution(page, () => "legacy_v0");
  const sessionId = "s_model_failure";
  const turnId = "t_model_failure";
  const now = "2026-10-26T00:00:00Z";
  const question = "请查询上月的销售情况";
  let sends = 0;
  const reason = runtimeLimit ? "agent_step_limit" : "llm_request_rejected";
  const diagnostics = runtimeLimit ? {
    schema_version: 1, disclosure: "authenticated", diagnostic_code: "chat_turn_failed",
    code: reason, turn_id: turnId, occurrence_id: "b".repeat(32), retryable: false, step_usage: { used: 14, limit: 14 },
    safe_detail: "phase=turn; failure_class=limit",
  } : {
    schema_version: 1, disclosure: "authenticated", diagnostic_code: "chat_llm_failed",
    code: "llm_request_rejected", turn_id: turnId, occurrence_id: "a".repeat(32), retryable: false,
    safe_detail: "phase=invoke; failure_class=request; provider_family=openai_compatible; http_status=400; stream_state=not_started; fallback=not_attempted; attempts=1; api_surface=responses; outcome=unknown",
    provider_message: "Unsupported request format",
  };
  const events = [
    { event_type: "turn_started", payload: { user_text: question, session_id: sessionId, execution_generation: 1 } },
    { event_type: "turn_failed", payload: { reason_code: reason, diagnostics, recovery_action: runtimeLimit ? "edit_and_resend" : "copy_diagnostics" } },
    { event_type: "done", payload: { status: "failed", reason: "terminal_protocol_malformed" } },
  ].map((event, index) => ({ ...event, seq: index + 1, occurred_at: now, payload: { ...event.payload, seq: index + 1, turn_id: turnId } }));
  await page.route("**/api/v1/chat/send", async (route) => {
    sends++;
    await route.fulfill({ json: { session_id: sessionId, turn_id: turnId, stream_url: `/api/v1/chat/stream/${turnId}`, status: "started", execution_generation: 1 } });
  });
  await page.route("**/api/v1/chat/stream/*", async (route) => {
    await route.fulfill({ contentType: "text/event-stream", body: events.map((event) => `id: ${event.seq}\nevent: ${event.event_type}\ndata: ${JSON.stringify(event.payload)}\n\n`).join("") });
  });
  await page.route("**/api/v1/turns/*/events", async (route) => route.fulfill({ json: { events } }));
  await page.route((url) => /^\/api\/v1\/sessions\/[^/]+$/.test(url.pathname), async (route) => {
    await route.fulfill({ json: {
      session: { id: sessionId, workspace_id: "ws_smoke", title: "Model failure", started_at: now, last_active_at: now, status: "failed", cost_usd: 0, tokens_total: 0, intents: [] },
      turns: [{ id: turnId, seq: 1, user_text: question, intent: "open_qa", status: "failed", error_kind: "llm_failed", execution_generation: 1, started_at: now, ended_at: now, final_answer: "", trace_steps: [], summary: null }],
    } });
  });
  const assertFailure = async () => {
    const card = page.getByTestId("final-fail");
    await expect(card).toBeVisible();
    if (runtimeLimit) {
      await expect(card).toContainText("达到任务步骤上限");
      await expect(card).toContainText("14 / 14");
      await expect(card).toContainText("查看保留结果");
    } else {
    await expect(card).toContainText("模型服务返回 HTTP 400");
    await expect(card).toContainText("Unsupported request format");
    await expect(card).toContainText("如果该网关或模型不支持 Responses");
    await expect(card).toContainText("Chat Completions");
    await expect(card).toContainText("系统已停止自动重试");
    }
    await expect(card.getByRole("button", { name: "重试", exact: true })).toHaveCount(0);
  };
  await page.goto("/");
  const composer = page.locator(".v2-composer");
  await composer.locator("textarea").fill(question);
  await composer.locator(".v2-send-btn").click();
  await assertFailure();
  await expect(page).toHaveURL(new RegExp(`session=${sessionId}`));
  await page.reload();
  await assertFailure();
  await page.goto("/");
  await page.goto(`/?session=${sessionId}`);
  await assertFailure();
  expect(sends).toBe(1);
});

}
