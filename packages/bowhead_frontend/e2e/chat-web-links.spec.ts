import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";
import { routeTerminalChatExecution } from "./_composer-recovery-fixtures";

const FIRST = "https://example.com/weather?day=30&city=tokyo#forecast";
const SECOND = "https://example.com/weather?day=31#tomorrow";
const LONG_HOST = `${Array.from({ length: 4 }, (_, index) => `${"w".repeat(54)}${index}`).join(".")}.example`;
const LONG_URL = `https://${LONG_HOST}/forecast?day=30`;
const QUESTION = `Please check https://example.com/user-request?literal=1

Bare URL: ${FIRST}

Another page: ${SECOND}

Surplus parenthesis: [https://example.com/weather/)](https://example.com/weather/)

Named link: [Forecast details](https://example.com/details?city=tokyo#day30)

Local port: http://127.0.0.1:8080/forecast?day=30#hourly

Literal URL: \`${FIRST}\`

Reference identifier: ${"A".repeat(320)}`;
const ANSWER = `[Forecast](https://example.com/named) ([${FIRST})](${FIRST}))\n\n${SECOND}\n\n${LONG_URL}\n\nLiteral URL: \`${FIRST}\``;

for (const width of [412, 1440]) {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`web links preserve destinations through SSE and refresh (${width}, ${colorScheme})`, async ({ page, context, browserName }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      await mockAuthedApp(page);
      await routeTerminalChatExecution(page, () => "legacy_v0");
      const sessionId = "s_web_links";
      const turnId = "t_web_links";
      const now = "2026-10-29T00:00:00Z";
      let sends = 0;
      let sourceRequests = 0;
      await context.route("https://example.com/**", async (route) => {
        sourceRequests += 1;
        await route.fulfill({ contentType: "text/plain", body: "Original source destination" });
      });
      const events = [
        { event_type: "turn_started", payload: { user_text: QUESTION, session_id: sessionId, execution_generation: 1 } },
        { event_type: "step_list_card_created", payload: { card_id: turnId } },
        { event_type: "narrative.delta", payload: { delta: ANSWER, card_id: turnId } },
        { event_type: "final_answer", payload: { text: ANSWER, response_kind: "chat" } },
        { event_type: "done", payload: { status: "completed", response_kind: "chat", duration_ms: 1, cost_usd: 0 } },
      ].map((event, index) => ({ ...event, seq: index + 1, occurred_at: now, payload: { ...event.payload, seq: index + 1, turn_id: turnId } }));
      await page.route("**/api/v1/chat/send", async (route) => {
        sends += 1;
        await route.fulfill({
          json: { session_id: sessionId, turn_id: turnId, stream_url: `/api/v1/chat/stream/${turnId}`, status: "started", execution_generation: 1 },
        });
      });
      await page.route("**/api/v1/chat/stream/*", async (route) => {
        await route.fulfill({
          contentType: "text/event-stream",
          body: events.map((event) => `id: ${event.seq}\nevent: ${event.event_type}\ndata: ${JSON.stringify(event.payload)}\n\n`).join(""),
        });
      });
      await page.route("**/api/v1/turns/*/events", async (route) => route.fulfill({ json: { events } }));
      await page.route(
        (url) => /^\/api\/v1\/sessions\/[^/]+$/.test(url.pathname),
        async (route) => {
          await route.fulfill({
            json: {
              session: {
                id: sessionId,
                workspace_id: "ws_smoke",
                title: "Web references",
                started_at: now,
                last_active_at: now,
                status: "completed",
                cost_usd: 0,
                tokens_total: 0,
                intents: [],
              },
              turns: [
                {
                  id: turnId,
                  seq: 1,
                  user_text: QUESTION,
                  intent: "open_qa",
                  status: "completed",
                  execution_generation: 1,
                  started_at: now,
                  ended_at: now,
                  final_answer: ANSWER,
                  trace_steps: [],
                  summary: null,
                },
              ],
            },
          });
        },
      );
      const links = page.locator(".lead-narrative-md a[data-compact-web-link]");
      const assertLinks = async () => {
        await expect(links).toHaveText(["example.com", "example.com", LONG_HOST]);
        for (const [index, href] of [FIRST, SECOND, LONG_URL].entries()) {
          await expect(links.nth(index)).toHaveAttribute("href", href);
          await expect(links.nth(index)).toHaveAttribute("title", href);
          await expect(links.nth(index)).toHaveAccessibleName(href);
        }
        await expect(page.locator(".lead-narrative-md code")).toHaveText(FIRST);
        await expect(page.getByTestId("user-msg")).toContainText("https://example.com/user-request?literal=1");
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        const messageWidths = await page.locator(".v2-messages").evaluateAll((nodes) =>
          nodes.map((node) => ({
            client: node.clientWidth,
            scroll: node.scrollWidth,
          })),
        );
        for (const dimensions of messageWidths) {
          expect(dimensions.scroll, `Message pane dimensions: ${JSON.stringify(messageWidths)}`).toBeLessThanOrEqual(dimensions.client + 1);
        }
        const ellipsis = await links.last().evaluate((node) => ({
          overflow: node.scrollWidth > node.clientWidth,
          style: getComputedStyle(node).textOverflow,
        }));
        expect(ellipsis).toEqual({ overflow: true, style: "ellipsis" });
      };
      await page.goto("/");
      const composer = page.locator(".v2-composer");
      await composer.locator("textarea").fill(QUESTION);
      await composer.locator(".v2-send-btn").click();
      await assertLinks();
      expect(sourceRequests).toBe(0);
      await links.first().hover();
      await links.first().focus();
      // WebKit uses the macOS Option modifier to include links in keyboard navigation.
      await page.keyboard.press(browserName === "webkit" ? "Alt+Tab" : "Tab");
      await expect(links.nth(1)).toBeFocused();
      await page.keyboard.press(browserName === "webkit" ? "Alt+Shift+Tab" : "Shift+Tab");
      await expect(links.first()).toBeFocused();
      const opened = context.waitForEvent("page");
      await page.keyboard.press("Enter");
      const popup = await opened;
      await expect(popup).toHaveURL(FIRST);
      await popup.close();
      expect(sourceRequests).toBe(1);
      await page.reload();
      await assertLinks();
      expect(sends).toBe(1);
      expect(sourceRequests).toBe(1);
      await page.screenshot({ path: test.info().outputPath(`web-links-${width}-${colorScheme}.png`), fullPage: true });
    });
  }
}

for (const width of [320, 412, 1440]) {
  test(`long archived user messages fit their message pane (${width})`, async ({ page }) => {
    await page.setViewportSize({ width, height: 915 });
    await mockAuthedApp(page);
    await routeTerminalChatExecution(page, () => "legacy_v0");
    const question =
      "这是网页引用展示验收，仅整理下面给定材料，不联网、不调用工具。两条材料都只是测试文案，不代表真实天气：\n1. 页面标题：东京天气预报；来源名称：2345天气；URL：https://tianqi.2345.com/asia_japan/tokyo/；材料内容：阴转晴。\n2. 页面标题：东京十五日天气；来源名称：墨迹天气；URL：https://tianqi.moji.com/forecast15/japan/tokyo/tokyo；材料内容：白天逐渐放晴。\n请用一句话概括材料，然后给出参考来源。";
    const samples = [
      [
        question,
        "两份材料对东京天气的描述一致。\n\n[东京天气预报 - 2345天气](https://tianqi.2345.com/asia_japan/tokyo/)\n\n[东京十五日天气 - 墨迹天气](https://tianqi.moji.com/forecast15/japan/tokyo/tokyo)",
      ],
      [
        "请给出第一条来源的完整原始网址，方便我直接复制。不要联网或调用工具。",
        "第一条来源的完整原始网址是：\n\n```text\nhttps://tianqi.2345.com/asia_japan/tokyo/\n```",
      ],
      [
        QUESTION,
        `裸网址：${FIRST}\n\n同域另一页：${SECOND}\n\n多余括号：[https://example.com/weather/)](https://example.com/weather/)\n\n命名链接：[预报详情](https://example.com/details?city=tokyo#day30)\n\n端口链接：http://127.0.0.1:8080/forecast?day=30#hourly\n\n代码原文：\`${FIRST}\``,
      ],
    ];
    const now = "2026-10-30T00:00:00Z";
    await page.route("**/api/v1/turns/*/events", (route) => route.fulfill({ json: { events: [] } }));
    await page.route(
      (url) => /^\/api\/v1\/sessions\/[^/]+$/.test(url.pathname),
      (route) =>
        route.fulfill({
          json: {
            session: {
              id: "s_long_messages",
              workspace_id: "ws_smoke",
              title: "Long user messages",
              started_at: now,
              last_active_at: now,
              status: "completed",
              cost_usd: 0,
              tokens_total: 0,
              intents: [],
            },
            turns: samples.map(([user_text, final_answer], index) => ({
              id: `t_long_${index}`,
              seq: index + 1,
              user_text,
              final_answer,
              intent: "open_qa",
              status: "completed",
              execution_generation: 1,
              started_at: now,
              ended_at: now,
              trace_steps: [],
              summary: null,
            })),
          },
        }),
    );
    await page.goto("/?session=s_long_messages");
    await expect(page.getByTestId("user-msg")).toHaveCount(3);
    for (const [index, [original]] of samples.entries()) {
      expect(await page.getByTestId("user-msg").nth(index).textContent()).toContain(original);
    }
    const geometry = await page.locator(".v2-messages").evaluate((node) => ({
      client: node.clientWidth,
      scroll: node.scrollWidth,
      children: [...node.querySelectorAll(".bh-user-msg > div:last-child, .lead-narrative-md, pre, p")].map((child) => ({
        tag: child.tagName,
        cls: child.className,
        client: child.clientWidth,
        scroll: child.scrollWidth,
        wrap: getComputedStyle(child).overflowWrap,
        width: child.getBoundingClientRect().width,
      })),
    }));
    expect(geometry.scroll, JSON.stringify(geometry)).toBeLessThanOrEqual(geometry.client + 1);
    for (const text of geometry.children.filter((child) => child.cls === "" && child.tag === "DIV")) {
      expect(text.scroll, `User message dimensions: ${JSON.stringify(text)}`).toBeLessThanOrEqual(text.client + 1);
    }
  });
}
