import { expect, test } from "@playwright/test";

/**
 * WP-3 (TD-4 / FR-1.4) — unified visibility, real-browser verification (AC-1.4b both directions).
 *
 * (i) Chat-mode turn (canvas.sections_hidden arrives) → hidden sections remain hidden after F5 (no leftover flash).
 * (ii) Drop sections_hidden → done briefly reveals section → terminal reconciliation (by-session already filters hidden)
 *      converges by removing it, ending hidden. Proves recovery when hide signal is lost (response_kind no longer controls visibility).
 */

async function resetMock(page: import("@playwright/test").Page) {
  await page.request.post("http://localhost:18080/api/v1/_test/reset");
}
async function sendMessage(page: import("@playwright/test").Page, msg: string) {
  const textarea = page.locator("textarea").first();
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill(msg);
  await textarea.press("Enter");
}
function visibleCount(page: import("@playwright/test").Page): Promise<number> {
  // selectVisibleSections: sections present but not buffered. Approximate via projection length
  // (projection includes actual sections[] only; reducer already removed buffered-hidden sections).
  return page.evaluate(
    () => ((window as unknown as Record<string, () => { sections: unknown[] }>).__CANVAS_SNAPSHOT__()).sections.length,
  );
}

test("AC-1.4b(i): chat-mode turn — sections hidden, stays empty across F5", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  await sendMessage(page, "chatmode");
  // sections_hidden removes sections + reconciliation stays empty → converges to 0.
  await expect.poll(() => visibleCount(page), { timeout: 20_000 }).toBe(0);
  // F5 hydrates empty by-session → still 0 (no leftover flash).
  await page.reload();
  await page.waitForTimeout(1_500);
  expect(await visibleCount(page)).toBe(0);
});

test("AC-1.4b(ii): dropped sections_hidden → reconcile converges to hidden (empty)", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  // Drop sections_hidden frame: done reveals sec_lead briefly;
  // terminal reconciliation reads empty server-hidden by-session → converges by removal.
  await sendMessage(page, "chatmode ::drop=canvas.sections_hidden");
  await expect.poll(() => visibleCount(page), { timeout: 20_000 }).toBe(0);
});

// Three-perspective review P0 machine guard: canvas-mode Lead narrative turn (buffered sec_lead_* + canvas.done,
// no sections_hidden). Old bug: canvas.done→onCanvasDone emptied buffer → done misread no content →
// cleared report sections (reconciliation restored, drift>0). Fixed: done reveal-only already correct, reconciliation no-op.
test("P0-regression: canvas-mode Lead-narrative section stays visible at done (no clear), lastDiff==0", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  await sendMessage(page, "leadcanvas");
  // Section must reveal and remain visible (old bug flashed then done cleared it).
  await expect.poll(() => visibleCount(page), { timeout: 20_000 }).toBe(1);
  await page.waitForTimeout(1_500); // Allow reconciliation to land.
  // Decisive: correct live state → reconciliation detects no drift (wrong done clearing means local 0 vs server 1 → drift>0).
  const stats = await page.evaluate(
    () => (window as unknown as Record<string, () => { count: number; lastDiff: number }>).__CANVAS_RECONCILE_STATS__(),
  );
  expect(stats.lastDiff).toBe(0);
  expect(await visibleCount(page)).toBe(1); // Still 1 after convergence (not cleared).
});

test("DF-04: sandbox code/data cannot summon Report; explicit Data focus still opens", async ({ page }) => {
  await resetMock(page);
  await page.route(
    (url) => url.pathname === "/api/v1/results/capabilities",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          dto_version: 1,
          workspace: true,
          complete_list: false,
          scale_nav: false,
          max_page_size: 50,
        }),
      });
    },
  );
  await page.addInitScript(() => {
    window.localStorage.setItem("bowhead.feature.results_workspace", "1");
  });
  await page.goto("/");
  await sendMessage(page, "pythondata");

  // Fault-injection keeps the auxiliary code section in both live and F5
  // snapshots. Even so, it cannot count as a report surface.
  await expect.poll(() => visibleCount(page), { timeout: 20_000 }).toBe(1);
  await expect(page.locator(".v2-ws-right")).toBeHidden();
  await expect.poll(() => page.evaluate(() => {
    const snapshot = (window as unknown as {
      __TURN_STORE_SNAPSHOT__: (turnId: string) => { steps: unknown[] } | undefined;
    }).__TURN_STORE_SNAPSHOT__("t_canvas_1");
    return snapshot?.steps.length ?? 0;
  })).toBe(1);

  await page.getByRole("button", { name: /展开运行过程/ }).click();
  await page.locator(".stp-head", { hasText: "运行代码" }).click();
  const openOutputButton = page.getByRole("button", {
    name: "在成果中打开 sandbox-stdout.txt",
  });
  await openOutputButton.click();

  await expect(page.locator(".v2-ws-right")).toBeVisible();
  await expect(page).toHaveURL(/result=art_stdout/);
  await expect(
    page.locator(".artifact-viewer-head").getByText("sandbox-stdout.txt", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("19×29=551")).toBeVisible();

  const returnToChat = page.getByRole("button", { name: "返回对话", exact: true });
  await expect(returnToChat).toBeVisible();
  await returnToChat.click();
  await expect(page.locator(".v2-ws-right")).toBeHidden();
  await expect(openOutputButton).toBeFocused();

  // Same-URL edge: close the currently selected object, open that same object
  // from Chat, then Back must pop the trusted source entry instead of replacing it.
  await openOutputButton.click();
  await expect(page.locator(".v2-ws-right")).toBeVisible();

  await page.getByRole("button", {
    name: "关闭成果并返回对话独占视图",
  }).click();
  await expect(page.getByRole("button", { name: "重开成果" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重开报告" })).toHaveCount(0);
  // Synthetic click deliberately does not focus the source first, matching
  // Safari's pointer behavior and exercising capture-phase source recovery.
  await openOutputButton.dispatchEvent("click");
  await expect(page.locator(".v2-ws-right")).toBeVisible();
  await page.getByRole("button", { name: "返回对话", exact: true }).click();
  await expect(page.locator(".v2-ws-right")).toBeHidden();
  await expect(page).toHaveURL(/result=art_stdout/);
  await expect(openOutputButton).toBeFocused();
});
