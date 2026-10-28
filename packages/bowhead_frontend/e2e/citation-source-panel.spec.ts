import { expect, test, type Locator } from "@playwright/test";

/**
 * Citation → SourcePanel e2e (citation-audit M3 onward: gen3 `value [N]` only).
 *
 * Fixture: /spike/p31 (real-canvas mock + gen3 [N] + one sources entry)
 *
 * Cover PRD §11 behavior:
 *  - Open panel on citation-chip click (mouse + keyboard)
 *  - Source details use the shared Dialog drawer: right-hand drawer on wide screens, bottom sheet below 640px
 *  - ESC + Close + backdrop click dismiss
 *  - Panel ↔ popover mutually exclusive (no popover while panel open)
 *  - Artifact actions never expose /data?focus= route
 *
 * ── M3 change record (do not restore from older version) ──────────────────────
 * Three removed test groups retired with gen2; **not bypassed**:
 *  1. `sentence <mark>` (`#highlight=` sentence highlighting): M1 already parsed
 *     then discarded `#highlight` in `sources_resolver`; these only passed because spike fixture
 *     hardcoded gen2 markers for frontend decoration, testing a dev-fixture-only path.
 *  2. `F22 fallback for unknown sourceId`: gen2 fallback for an unresolved
 *     `:src:<unknown id>`. Gen3 `[N]` is a **number**, not id; out-of-range numbers (e.g. `[9]`)
 *     are never decorated as chips, so clicking an unresolvable source is impossible.
 *  3. `legacy no-source marker` (`.citation-num--no-src` amber): exclusive to gen2
 *     markers without `:src:`; its only producer (NUM_OLD_RE pass) was removed in M3.
 * Entire spec previously **failed** (including gen3): `waitForReportReady` awaited gen2 pill
 * `[data-citation-kind='num'][data-highlight]`, absent after M3 → every case timed out at 15s.
 *
 * ── B1a source projection (from c80341f3d) ──────────────────────────────
 * Frontend accepts only gateway-projected sources: source-ref-v1 aliases + access_state + safe_*,
 * never raw artifact_id/tool; opening panel probes safe_preview_url once,
 * showing source name, preview, and report-scoped download only after success. Fixture uses projected shape;
 * intercept probe here to simulate viewer access to this source.
 */

const NUMBERED_CSS = "[data-citation-kind='numbered']";
// The source details drawer is the modal dialog. Hover cards on citation chips are non-modal dialogs too: once the drawer
// closes with the pointer still over a chip, that chip's hover card may show.
const DIALOG = "[role='dialog'][aria-modal='true']";

const SOURCE_URL = "/api/v1/canvas/by-session/sess_spike_p31/artifacts/art_demo_numbered_001";

async function waitForReportReady(page: import("@playwright/test").Page) {
  await page.route(`**${SOURCE_URL}/preview**`, (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ family: "text", mode: "text", text: "P0-A numbered source preview" }),
  }));
  await page.goto("/spike/p31");
  // Wait for demo narrative [N] chip decoration.
  await page.waitForSelector(NUMBERED_CSS, { state: "attached", timeout: 15000 });
}

async function clickCitation(locator: Locator) {
  await locator.evaluate((el) => {
    el.scrollIntoView({ block: "center", inline: "nearest" });
  });
  await locator.click();
}


// ─── Desktop 1440×900 ──────────────────────────────────────


test.describe("desktop 1440×900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("引用 chip 点击打开右侧滑入 panel", async ({ page }) => {
    await waitForReportReady(page);

    const pill = page.locator(NUMBERED_CSS).first();
    await expect(pill).toBeVisible();

    // Chip must really be visible (M3 removed `.citation-num{font-size:0}` along with gen2 decoration pass;
    // restoring a 0px [N] mask must fail this test).
    const fontSize = await pill.evaluate((el) => getComputedStyle(el).fontSize);
    expect(fontSize).not.toBe("0px");

    await clickCitation(pill);

    const dialog = page.locator(DIALOG);
    await expect(dialog).toBeVisible();
    await expect(dialog).not.toHaveAttribute("data-dialog-presentation", "sheet");
    await expect(dialog).toContainText("[1]");
    await expect(dialog).toContainText("P0-A canonical numbered source");
    await expect(dialog).toContainText("Q1 总营业额");
    await expect(dialog).toContainText("P0-A numbered source preview");

    await page.screenshot({
      path: "test-results/citation-gen3/desktop_panel_open.png",
      fullPage: false,
    });

    // Right-hand drawer: right edge on the viewport edge, 420 wide.
    const box = await dialog.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThan(800);
    expect(Math.round(box!.x + box!.width)).toBe(1440);
    expect(box!.width).toBeGreaterThanOrEqual(420 - 1);
  });

  test("ReportView defaults to read-only without save context", async ({ page }) => {
    await waitForReportReady(page);
    await expect(page.locator(".v2-body").first()).toHaveAttribute("data-edit-mode", "off");
    await expect(page.locator("[contenteditable='true']")).toHaveCount(0);
  });

  test("SourcePanel 只给报告作用域的下载链接", async ({ page }) => {
    await waitForReportReady(page);

    const pill = page.locator(NUMBERED_CSS).first();
    await clickCitation(pill);
    const dialog = page.locator(DIALOG);
    await expect(dialog).toBeVisible();

    await expect(dialog.getByRole("link", { name: /下载|Download/ })).toHaveAttribute("href", SOURCE_URL);
    await expect(dialog.getByRole("link", { name: "Open in Files" })).toHaveCount(0);
    await expect(page.locator("a[href^='/data?focus=']")).toHaveCount(0);
  });

  test("引用 chip 键盘可达（Enter 打开 panel）", async ({ page }) => {
    await waitForReportReady(page);

    const pill = page.locator(NUMBERED_CSS).first();
    await expect(pill).toBeVisible();
    await pill.focus();
    await expect(pill).toBeFocused();

    await page.keyboard.press("Enter");

    const dialog = page.locator(DIALOG);
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("P0-A canonical numbered source");
  });

  test("ESC closes panel", async ({ page }) => {
    await waitForReportReady(page);
    await clickCitation(page.locator(NUMBERED_CSS).first());
    await expect(page.locator(DIALOG)).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.locator(DIALOG)).toHaveCount(0);
  });

  test("close button dismisses panel", async ({ page }) => {
    await waitForReportReady(page);
    await clickCitation(page.locator(NUMBERED_CSS).first());
    await expect(page.locator(DIALOG)).toBeVisible();

    await page.locator(DIALOG).getByRole("button", { name: "关闭" }).click();
    await expect(page.locator(DIALOG)).toHaveCount(0);
  });

  test("panel suppresses popover (PRD §11.4)", async ({ page }) => {
    await waitForReportReady(page);
    const pill = page.locator(NUMBERED_CSS).first();
    await clickCitation(pill);
    await expect(page.locator(DIALOG)).toBeVisible();

    // Move the pointer over the same chip: the modal drawer's backdrop keeps it off the report, so no
    // popover appears while the panel is open.
    const box = (await pill.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.waitForTimeout(400);
    await expect(page.locator(".citation-popover")).toHaveCount(0);
  });

  test("backdrop click outside the drawer dismisses it", async ({ page }) => {
    await waitForReportReady(page);
    await clickCitation(page.locator(NUMBERED_CSS).first());
    await expect(page.locator(DIALOG)).toBeVisible();

    // Click the backdrop over the report, left of the drawer.
    await page.mouse.click(200, 450);
    await expect(page.locator(DIALOG)).toHaveCount(0);
  });

  test("退役标记经 stripLegacy bake 成纯文本（渲染层永不显示 raw marker）", async ({ page }) => {
    await waitForReportReady(page);
    // Fixture narrative embeds legacy `[num:849000:CNY:src:s_001]` / `[ref:]` / `[chart:]`
    // handled by canvas-to-pm stripLegacy fallback. Before M3 removed four gen2 decoration passes,
    // CSS masked raw markers; removing decoration removed mask, leaving only this fallback seam.
    const body = page.locator(".v2-body").first();
    await expect(body).toContainText("84.9万元");
    const text = (await body.innerText()) ?? "";
    expect(text, "裸退役标记糊在正文里").not.toContain("[num:");
    expect(text, "裸退役标记糊在正文里").not.toContain("[ref:");
    expect(text, "裸退役标记糊在正文里").not.toContain("[chart:");
  });
});


// ─── Mobile 412×915 (Pixel 7) ──────────────────────────────


test.describe("mobile 412×915", () => {
  test.use({ viewport: { width: 412, height: 915 } });

  test("引用 chip 点按打开底部抽屉", async ({ page }) => {
    await waitForReportReady(page);
    const pill = page.locator(NUMBERED_CSS).first();
    await clickCitation(pill);

    const dialog = page.locator(DIALOG);
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("data-dialog-presentation", "sheet");
    await expect(dialog).toContainText("P0-A canonical numbered source");

    // Bottom sheet: full width, anchored to the bottom, at most 92% of the viewport height.
    const sheet = page.locator(".ui-dialog-sheet");
    await sheet.evaluate((node) => Promise.all(node.getAnimations().map((animation) => animation.finished)));
    const box = await sheet.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.y).toBeGreaterThan(50);
    expect(Math.round(box!.y + box!.height)).toBe(915);
    expect(box!.height).toBeLessThanOrEqual(915 * 0.92 + 1);
    expect(box!.width).toBeGreaterThan(400);

    await page.screenshot({
      path: "test-results/citation-gen3/mobile_bottom_sheet.png",
      fullPage: false,
    });
  });

  test("ESC closes bottom-sheet", async ({ page }) => {
    await waitForReportReady(page);
    await clickCitation(page.locator(NUMBERED_CSS).first());
    await expect(page.locator(DIALOG)).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.locator(DIALOG)).toHaveCount(0);
  });
});
