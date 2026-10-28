import { expect, test, type Locator, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * Chat-workbench/report-canvas batch 3 (canvas appearance): key visuals and layers in real browsers.
 * Runs Chromium/WebKit (CROSS_BROWSER_SPECS). Mock/fixture pages only, no real chat requests.
 * Chapter buttons 28, menu rows 32, selection toolbar 34 with overlay material, source popover 320; none covered by body/charts/tables;
 * canvas/chat badges share filled background.
 */

async function open(page: Page, url: string) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockAuthedApp(page);
  await page.goto(url);
  await expect(page.locator(".v2-section-chrome").first()).toBeAttached({ timeout: 20_000 });
}

/** Center-point hit is the element itself (or its descendant)—uncovered by other layers. */
async function hitsItself(target: Locator): Promise<boolean> {
  return target.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return Boolean(hit && (hit === el || el.contains(hit)));
  });
}

async function selectFirstWords(page: Page) {
  const para = page.locator(".ProseMirror p").first();
  await para.scrollIntoViewIfNeeded();
  await para.evaluate((el) => {
    const node = document.createTreeWalker(el, NodeFilter.SHOW_TEXT).nextNode()!;
    const range = document.createRange();
    range.setStart(node, 0);
    range.setEnd(node, Math.min(6, node.textContent!.length));
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
  });
  const box = (await para.boundingBox())!;
  await page.mouse.move(box.x + 5, box.y + 5);
  await page.mouse.up();
}

/** Resolve token to computed color for comparison with element computed values. */
async function tokenColor(page: Page, token: string): Promise<string> {
  return page.evaluate((t) => {
    const probe = document.createElement("div");
    probe.style.background = `var(${t})`;
    document.body.appendChild(probe);
    const value = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return value;
  }, token);
}

test("chapter action buttons are 28 and chapter menu rows 32, not covered by body or charts", async ({ page }) => {
  await open(page, "/spike/p31?designEdit=1");
  const para = page.locator(".ProseMirror p").first();
  // Only hovered section (innermost when nested) shows actions; other sections' actions are transparent/pointer-inert.
  // Fixture still relayouts after first screen (charts/fonts); retry if hover misses.
  const actions = page.locator(".v2-section-chrome:hover .rc-actions").last();
  await expect(async () => {
    await para.scrollIntoViewIfNeeded();
    await para.hover();
    await expect(actions).toHaveCSS("opacity", "1", { timeout: 1_000 });
  }).toPass({ timeout: 15_000 });
  const menuBtn = actions.getByRole("button", { name: "操作菜单" });
  await expect(menuBtn).toBeVisible();
  expect(Math.round((await menuBtn.boundingBox())!.height)).toBe(28);
  expect(await hitsItself(menuBtn)).toBe(true);
  await menuBtn.click();
  const item = page.locator(".rc-menu-pop .rc-mi").first();
  await expect(item).toBeVisible();
  // Since batch 4b, shared menu has entry-scale animation; wait before measuring.
  await expect.poll(async () => Math.round((await item.boundingBox())!.height)).toBe(32);
  expect(await hitsItself(item)).toBe(true);
  const pop = page.locator(".rc-menu-pop").first();
  expect((await pop.boundingBox())!.width).toBeGreaterThanOrEqual(220);
});

test("selection toolbar is 34 high with overlay material and uncovered", async ({ page }) => {
  await open(page, "/spike/p31?designEdit=1");
  await selectFirstWords(page);
  const bar = page.locator(".rc-sel-toolbar");
  await expect(bar).toBeVisible();
  expect(Math.round((await bar.boundingBox())!.height)).toBe(34);
  expect(await hitsItself(bar.locator(".rc-sel-btn").first())).toBe(true);
  const bg = await bar.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(bg).not.toBe(await tokenColor(page, "--c-surface"));
});

test("canvas citation badges are filled; the hover source popover is 320 wide and not covered by charts or tables", async ({ page }) => {
  await open(page, "/spike/p31");
  const chip = page.locator(".citation-num--numbered").first();
  await chip.evaluate((el) => el.scrollIntoView({ block: "center" }));
  expect(await chip.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(await tokenColor(page, "--c-fill-2"));
  await chip.hover();
  const pop = page.locator(".citation-popover").first();
  await expect(pop).toBeVisible({ timeout: 5_000 });
  expect(Math.round((await pop.boundingBox())!.width)).toBe(320);
  expect(await hitsItself(pop.locator(".rc-prov-head").first())).toBe(true);
});

/** Read-only shared report (session detail 403, canvas readable only). Sources use actual canvas-projection shapes:
 *  [2] Non-KB query source has no destination → linked_unavailable / query_result (viewer-independent, not restricted);
 *  [3] KB document is restricted for viewer → linked_restricted / document. */
async function openSharedReport(page: Page) {
  const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
  const alias = (k: string) => `source-ref-v1:${k.padEnd(43, "0")}`;
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.route("**/health", (r) => r.fulfill(json({ feature_flags: { results_workspace: true } })));
  await page.route("**/api/v1/auth/me", (r) => r.fulfill(json({
    id: "viewer", email: "viewer@example.com", name: "Viewer", tier: "ultra",
    capabilities: { access_requests: true }, entitlement: { read_only: false },
  })));
  await page.route("**/api/v1/auth/me/workspaces", (r) => r.fulfill(json({ items: [{ workspace_id: "ws_shared", name: "Shared", description: "" }], active_id: "ws_shared" })));
  await page.route("**/api/v1/user-settings**", (r) => r.fulfill(json({ settings: {} })));
  await page.route("**/api/v1/theme/effective", (r) => r.fulfill(json({ mode: "light", tokens: {} })));
  await page.route("**/api/v1/team-settings/theme-policy", (r) => r.fulfill(json({ value: null })));
  await page.route("**/api/v1/llm/catalog", (r) => r.fulfill(json({ providers: [], defaults: {} })));
  await page.route("**/api/v1/sessions", (r) => r.fulfill(json({ groups: [{ id: "shared", label: "Shared", items: [{ id: "s_shared", title: "角标", intents: ["report"], time: "09:00", state: "done" }] }] })));
  await page.route("**/api/v1/sessions/s_shared/seen", (r) => r.fulfill(json({ ok: true })));
  await page.route("**/api/v1/sessions/s_shared", (r) => r.fulfill(json({ detail: "forbidden" }, 403)));
  await page.route("**/api/v1/canvas/by-session/s_shared**", (r) => r.fulfill(json({
    canvas: { id: "cv_shared", sessionId: "s_shared", workspaceId: "ws_shared", title: "角标", status: "done", createdAt: "2026-07-03T00:00:00Z", updatedAt: "2026-07-03T00:00:00Z" },
    sections: [
      { id: "h1_shared", kind: "h1", text: "角标" },
      { id: "n_shared", kind: "narrative", markdown: "营收增长 [1]，客单价回落 [2]，制度依据 [3]。" },
      { id: "src_shared", kind: "sources", entries: [
        { source_alias: alias("cca"), access_state: "linked_accessible", safe_label: "门店销售日表", generic_type: "query_result" },
        { source_alias: alias("ccb"), access_state: "linked_unavailable", generic_type: "query_result" },
        { source_alias: alias("ccc"), access_state: "linked_restricted", generic_type: "document" },
      ] },
    ],
    sourceRefs: [],
  })));
  await page.route("**/api/v1/memory/snapshot", (r) => r.fulfill(json({ items: [] })));
  await page.route("**/api/v1/metrics/event", (r) => r.fulfill(json({ ok: true })));
  await page.addInitScript(() => { window.localStorage.setItem("bowhead.feature.results_workspace", "1"); });
  await page.goto("/?session=s_shared");
  await expect(page.locator(".citation-num--numbered")).toHaveCount(3, { timeout: 20_000 });
}

test("只读分享视图：知识库受限来源的角标划删除线；无出口的查询来源与可查看来源不划；工具条 44 高", async ({ page }) => {
  await openSharedReport(page);
  const chips = page.locator(".citation-num--numbered");
  await expect.poll(() => chips.evaluateAll((els) => els.map((el) => el.hasAttribute("data-source-denied")))).toEqual([false, false, true]);
  expect(await chips.evaluateAll((els) => els.map((el) => getComputedStyle(el).textDecorationLine))).toEqual(["none", "none", "line-through"]);
  const toolbar = page.locator(".results-report-object-toolbar");
  await expect(toolbar).toBeVisible();
  expect(Math.round((await toolbar.boundingBox())!.height)).toBe(44);
});
