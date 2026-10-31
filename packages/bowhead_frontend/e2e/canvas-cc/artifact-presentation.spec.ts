import { expect, test, type Page } from "@playwright/test";

async function showFileLink(page: Page) {
  const expand = page.getByRole("button", { name: /展开运行过程/ });
  await expect(expand).toBeVisible({ timeout: 20_000 });
  if (await expand.isVisible()) await expand.click();
  const link = page.getByRole("button", { name: "在成果中打开 计算说明 · calc.md" });
  if (!await link.isVisible()) await page.locator(".stp-head", { hasText: "交付已有文件" }).click();
  await expect(link).toBeVisible();
  return link;
}

test("retained files keep their identity, title and navigation after replay", async ({ page }) => {
  const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";
  await page.request.post(`http://localhost:${apiPort}/api/v1/_test/reset`);
  await page.addInitScript(() => window.localStorage.setItem("bowhead.feature.results_workspace", "1"));
  await page.goto("/");
  const composer = page.locator("textarea").first();
  await expect(composer).toBeVisible();
  const streamResponse = page.waitForResponse((response) => response.url().includes("/stream"));
  await composer.fill("presentfiles");
  await composer.press("Enter");
  await expect(page.getByRole("button", { name: /展开运行过程/ })).toBeVisible({ timeout: 20_000 });
  const link = await showFileLink(page);
  await expect(link).toHaveAttribute("title", "复用已保存的原文件");
  await expect(page.locator(".v2-ws-right")).toBeHidden();
  await link.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/result=art_calc/);
  await expect(page.locator(".artifact-viewer-head").getByText("calc.md", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "返回对话", exact: true }).click();
  // The shared canvas fixture intentionally returns an empty chat history.
  // Replay the exact live frames through the real Session hydration reader.
  // Read the retained mock stream as UTF-8 bytes. Chromium's DevTools response
  // capture can reinterpret EventSource text even though the live reader does not.
  const stream = await (await page.request.get((await streamResponse).url())).text();
  const events = stream.split("\n\n").flatMap((frame, index) => {
    const eventType = frame.match(/^event: (.+)$/m)?.[1];
    const data = frame.match(/^data: (.+)$/m)?.[1];
    return eventType && data ? [{ seq: index + 1, event_type: eventType, payload: JSON.parse(data), occurred_at: "2026-10-28T00:00:00Z" }] : [];
  });
  expect(events.some((event) => event.event_type === "step_done")).toBe(true);
  await page.route("**/api/v1/sessions/s_e2e", async (route) => {
    const detail = await (await route.fetch()).json();
    await route.fulfill({ json: { ...detail, turns: [{ id: "t_canvas_1", seq: 1, user_text: "presentfiles", intent: "analysis",
      status: "completed", started_at: "2026-10-28T00:00:00Z", ended_at: "2026-10-28T00:00:01Z",
      final_answer: "完成。", trace_steps: [], summary: null }] } });
  });
  await page.route("**/api/v1/turns/t_canvas_1/events", (route) => route.fulfill({ json: { turn_id: "t_canvas_1", events } }));
  await page.goto("/?session=s_e2e");
  const replayed = await showFileLink(page);
  await replayed.click();
  await expect(page).toHaveURL(/result=art_calc/);
  await expect(page.locator(".artifact-viewer-head").getByText("calc.md", { exact: true })).toBeVisible();
});
