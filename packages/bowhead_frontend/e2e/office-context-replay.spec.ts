import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";
import { routeTerminalChatExecution } from "./_composer-recovery-fixtures";

// Actual Next UI + legacy-v0 EventSource + F5 hydration, controlled HTTP fixtures.
// Gateway/Core tests separately prove no Office parsing or model classification.
for (const width of [412, 1440]) {
  test(`Office references retain current order without parse steps, live and F5 (${width})`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await mockAuthedApp(page);
    await routeTerminalChatExecution(page, () => "legacy_v0");
    const refs = [
      { fileId: "art_office_first", filename: "first.docx", mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", size: 8 },
      { fileId: "art_office_second", filename: "second.pptx", mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation", size: 8 },
    ];
    const question = "第二份是模板，第一份只是参考";
    const answer = "已收到文件引用；尚未读取正文。";
    const now = "2026-09-06T00:00:00Z";
    let sessionId = "";
    let turnId = "";
    let uploads = 0;
    let sends = 0;
    let streams = 0;
    let sentBody: Record<string, unknown> = {};
    const events = () => [
      { event_type: "turn_started", payload: { user_text: question, session_id: sessionId, execution_generation: 1 } },
      { event_type: "step_list_card_created", payload: { card_id: turnId } },
      { event_type: "final_answer", payload: { text: answer, response_kind: "chat" } },
      { event_type: "done", payload: { status: "completed", response_kind: "chat", duration_ms: 1, cost_usd: 0 } },
    ].map((event, index) => ({ ...event, seq: index + 1, occurred_at: now, payload: { ...event.payload, seq: index + 1, turn_id: turnId } }));

    await page.route("**/api/v1/files/upload*", async (route) => {
      const ref = refs[uploads++];
      await route.fulfill({ status: 200, json: { file_id: ref.fileId, filename: ref.filename, mime: ref.mime, size: ref.size, url: `/api/v1/files/${ref.fileId}` } });
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      sends++;
      sentBody = route.request().postDataJSON() as Record<string, unknown>;
      sessionId = "s_office_context";
      turnId = "t_office_context";
      await route.fulfill({ status: 200, json: { session_id: sessionId, turn_id: turnId, stream_url: `/api/v1/chat/stream/${turnId}`, status: "started", execution_generation: 1 } });
    });
    await page.route("**/api/v1/chat/stream/*", async (route) => {
      streams++;
      await route.fulfill({ status: 200, contentType: "text/event-stream", headers: { "Access-Control-Allow-Origin": new URL(page.url()).origin, "Access-Control-Allow-Credentials": "true" }, body: events().map((e) => `id: ${e.seq}\nevent: ${e.event_type}\ndata: ${JSON.stringify(e.payload)}\n\n`).join("") });
    });
    await page.route("**/api/v1/turns/*/events", async (route) => {
      await route.fulfill({ status: 200, json: { events: events() } });
    });
    await page.route((url) => /^\/api\/v1\/sessions\/[^/]+$/.test(url.pathname), async (route) => {
      await route.fulfill({ status: 200, json: {
        session: { id: sessionId, workspace_id: "ws_smoke", title: "Office references", started_at: now, last_active_at: now, status: "completed", cost_usd: 0, tokens_total: 0, intents: [] },
        turns: [{ id: turnId, seq: 1, user_text: question, intent: "open_qa", status: "completed", execution_generation: 1, started_at: now, ended_at: now, final_answer: answer, trace_steps: [], summary: null,
          attachments: refs.map((ref) => ({ file_id: ref.fileId, filename: ref.filename, mime: ref.mime, size: ref.size })),
        }],
      } });
    });

    await page.goto("/");
    const composer = page.locator(".v2-composer");
    await expect(composer.locator('input[type="file"]')).toBeEnabled();
    // Separate successful uploads make their user-selected order deterministic.
    for (let index = 0; index < refs.length; index++) {
      await composer.locator('input[type="file"]').setInputFiles({ name: refs[index].filename, mimeType: "application/zip", buffer: Buffer.from("fixture!") });
      await expect(composer.locator(".v2-chip__name")).toHaveCount(index + 1);
      await expect(composer.locator(".v2-send-btn")).toBeEnabled();
    }
    await composer.locator("textarea").fill(question);
    await composer.locator(".v2-send-btn").click();
    await expect.poll(() => streams).toBe(1);
    await expect(page.getByText(answer, { exact: true })).toBeVisible();
    await expect(page.locator(".ub-attachments .v2-chip__name")).toHaveText(refs.map((ref) => ref.filename));
    expect(sentBody.attachments).toEqual(refs.map((ref) => ref.fileId));
    expect(streams).toBe(1);
    await expect(page.getByText(/^解析 (first\.docx|second\.pptx)$/)).toHaveCount(0);
    await expect(page).toHaveURL(new RegExp(`session=${sessionId}`));

    await page.reload();
    await expect(page.getByText(answer, { exact: true })).toBeVisible();
    await expect(page.locator(".ub-attachments .v2-chip__name")).toHaveText(refs.map((ref) => ref.filename));
    await expect(page.getByText(/^解析 (first\.docx|second\.pptx)$/)).toHaveCount(0);
    expect(uploads).toBe(2);
    expect(sends).toBe(1);
  });
}
