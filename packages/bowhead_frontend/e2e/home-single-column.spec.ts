import { expect, test, type Locator, type Page } from "@playwright/test";
import referenceFixtures from "../../../contracts/chat-references/catalog-v1.json";
import { parseReferenceCatalog } from "../lib/reference-catalog-api";

/**
 * Page-acceptance finish: single-column home (Workbench Spec §1 “标题 → 输入框 → 三张提议卡 → 分组”).
 * Runs Chromium/WebKit (CROSS_BROWSER_SPECS), desktop 1440 and phone 412.
 * Visual/DOM/Tab order align; composer slot below title; no component-center points covered by other layers;
 * existing New Work, ?session= deep links, back/forward-to-home work unchanged; composer remains slotted on return.
 * Uses gate-mode mock Gateway (sessions s2 / s3) with a fixed home snapshot/proposals.
 */

const S2 = "华东 Q3 下滑归因";

const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });

async function setup(page: Page, { width }: { width: number }) {
  await page.setViewportSize({ width, height: width < 700 ? 915 : 900 });
  await page.route("**/health", (route) =>
    route.fulfill(json({ status: "ok", version: "e2e", frontend_min: "e2e", force_reload: false, feature_flags: {} })));
  await page.route("**/api/v1/metrics/event", (route) => route.fulfill(json({})));
  await page.route("**/api/v1/home/snapshot**", (route) => {
    const workspaceId = new URL(route.request().url()).searchParams.get("workspace_id") ?? "ws_e2e";
    return route.fulfill(json({
      version: "home.snapshot.v1",
      generatedAt: "2026-09-29T08:00:00Z",
      scope: { workspaceId, workspaceName: "E2E space" },
      attentionScope: "user",
      attention: [],
      active: [{
        id: "chat:turn_home_1", engine: "chat", nativeStatus: "running", title: "华东 Q3 下滑归因", subtitle: null, progress: null,
        actionUrl: "/?session=s2", sessionId: "s2", startedAt: "2026-09-29T07:50:00Z", updatedAt: "2026-09-29T07:58:00Z", metadata: {},
      }],
      outputs: [],
      counts: { attention: 0, active: 1, outputs: 0 },
      partial: false,
      issues: [],
    }));
  });
  await page.route("**/api/v1/home/proposals**", (route) => route.fulfill(json({
    version: "home.proposals.v1", generatedAt: "2026-09-29T08:00:00Z", strategy: "deterministic", cached: false, generationId: "hp_e2e",
    items: ["analyze", "create", "automate"].map((kind, index) => ({
      id: `hpi_${index}`, kind, title: `提议 ${index + 1}`, reason: "e2e 固定提议", prompt: `提议 ${index + 1} 的提示`,
      capabilityRefs: [], contextRefs: [],
    })),
  })));
}

async function openHome(page: Page, width = 1440) {
  await setup(page, { width });
  await page.goto("/");
  await expect(page.locator(".home-work-proposal").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".home-work-row").first()).toBeVisible();
}

/** Center-point hit is the element itself (or its descendant)—uncovered by other layers. */
async function hitsItself(target: Locator): Promise<boolean> {
  await target.scrollIntoViewIfNeeded();
  return target.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return Boolean(hit && (hit === el || el.contains(hit)));
  });
}

const top = async (locator: Locator) => (await locator.first().boundingBox())!.y;

for (const width of [1440, 412]) {
  test(`${width}: title → composer → proposals → work groups; visual, DOM and Tab order agree and nothing is covered`, async ({ page, browserName }) => {
    await openHome(page, width);
    const title = page.locator(".home-work-header h1");
    const composer = page.locator(".home-work-composer-slot .v2-composer");
    const proposal = page.locator(".home-work-proposal").first();
    const row = page.locator(".home-work-row").first();
    await expect(composer).toBeVisible();
    const editor = composer.locator("textarea, .tiptap").first();
    expect((await editor.boundingBox())!.height).toBeGreaterThanOrEqual(112);
    if (width >= 1200) expect((await composer.boundingBox())!.width).toBeGreaterThanOrEqual(780);
    await page.screenshot({ path: test.info().outputPath(`home-composer-${width}.png`) });
    // No second composer dock at page bottom.
    await expect(page.locator(".home-work-page > .home-work-composer-dock")).toHaveCount(0);

    const ys = [await top(title), await top(composer), await top(proposal), await top(row)];
    expect([...ys].sort((a, b) => a - b)).toEqual(ys);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);

    for (const target of [title, composer, proposal, row]) expect(await hitsItself(target)).toBe(true);

    // Tab order: leave composer through proposals, then work rows (composer tool-row buttons may intervene).
    await page.locator(".home-work-composer-slot textarea").first().focus();
    const visited: string[] = [];
    for (let i = 0; i < 20 && !visited.includes("row"); i += 1) {
      // Safari default Tab visits form controls only; ⌥Tab visits buttons (matching system Highlight Each Item with Tab setting).
      await page.keyboard.press(browserName === "webkit" ? "Alt+Tab" : "Tab");
      const kind = await page.evaluate(() => {
        const el = document.activeElement;
        if (!el) return "none";
        if (el.closest(".home-work-proposal")) return "proposal";
        if (el.closest(".home-work-row")) return "row";
        if (el.closest(".home-work-composer-slot")) return "composer";
        return "other";
      });
      if (visited.at(-1) !== kind) visited.push(kind);
    }
    const meaningful = visited.filter((k) => k !== "other");
    // After composer, visit proposal then work row, never jump back into composer.
    expect(meaningful.filter((k) => k !== "composer")).toEqual(["proposal", "row"]);
    expect(meaningful.lastIndexOf("composer")).toBeLessThan(meaningful.indexOf("proposal"));
  });
}

test("mounts the home composer once while the Space list loads", async ({ page }) => {
  await page.addInitScript(() => {
    const events: string[] = [];
    (window as unknown as { __composerMounts: string[] }).__composerMounts = events;
    const holdsComposer = (node: Node) => node instanceof Element
      && (node.matches(".v2-composer-wrap") || node.querySelector(".v2-composer-wrap") !== null);
    new MutationObserver((records) => {
      for (const record of records) {
        record.addedNodes.forEach((node) => { if (holdsComposer(node)) events.push("add"); });
        record.removedNodes.forEach((node) => { if (holdsComposer(node)) events.push("remove"); });
      }
    }).observe(document, { childList: true, subtree: true });
  });
  let releaseSpaces!: () => void;
  const spacesHeld = new Promise<void>((resolve) => { releaseSpaces = resolve; });
  await page.route("**/api/v1/auth/me/workspaces", async (route) => {
    await spacesHeld;
    await route.continue();
  });
  await setup(page, { width: 1440 });
  // The held Space list request would also hold the load event; wait for the DOM only.
  await page.goto("/", { waitUntil: "domcontentloaded" });
  // The composer is already on screen (send disabled) while the Space list is held.
  await expect(page.locator(".home-work-composer-slot .v2-composer-wrap")).toBeVisible({ timeout: 15_000 });
  releaseSpaces();
  await expect(page.locator(".home-work-proposal").first()).toBeVisible({ timeout: 15_000 });
  const textarea = page.locator(".home-work-composer-slot .v2-composer textarea");
  await textarea.click();
  await page.keyboard.type("draft after the Space resolves");
  await page.waitForTimeout(300);
  await expect(textarea).toBeFocused();
  await expect(textarea).toHaveValue("draft after the Space resolves");
  expect(await page.evaluate(() => (window as unknown as { __composerMounts: string[] }).__composerMounts))
    .toEqual(["add"]);
});

test("New Work, ?session= deep links and back / forward to home work as before; back on home the composer is still in the slot under the title", async ({ page }) => {
  await setup(page, { width: 1440 });
  await page.goto("/?session=s2");
  const row = page.locator(".unified-work-item", { hasText: S2 }).locator(".unified-work-item-main");
  await expect(row).toHaveAttribute("aria-current", "page", { timeout: 15_000 });
  // Same path as session-url-history: deep-link s3 (new entry), New Work replaces current entry, back returns to s2.
  await page.evaluate(() => window.history.pushState(null, "", `${window.location.pathname}?session=s3`));
  await expect(page).toHaveURL(/\?session=s3$/);

  await page.getByTestId("unified-nav-new_work").click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator(".home-work-composer-slot .v2-composer")).toBeVisible({ timeout: 15_000 });
  await page.evaluate(() => { (window as unknown as { __same?: boolean }).__same = true; });

  await page.goBack();
  await expect(page).toHaveURL(/\?session=s2$/);
  await expect(row).toHaveAttribute("aria-current", "page");
  await page.goForward();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator(".home-work-composer-slot .v2-composer")).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __same?: boolean }).__same === true)).toBe(true);

  // Home work-row session deep link still opens normally.
  await page.locator(".home-work-row").first().click();
  await expect(page).toHaveURL(/\?session=s2$/);
});


test("home rich editor keeps the larger input and accepts an available system Agent", async ({ page }) => {
  await setup(page, { width: 1440 });
  await page.route("**/api/v1/auth/me", async (route) => {
    const response = await route.fetch();
    const me = await response.json();
    me.capabilities = { ...me.capabilities, composer: { ...me.capabilities?.composer, reference_input: true } };
    await route.fulfill(json(me));
  });
  const catalog = parseReferenceCatalog(referenceFixtures.cases.find((item) => item.name === "system_agent")!.response, {
    trigger: "@", query: "", entryMode: "ordinary", selectedRefs: [],
  });
  for (const cell of catalog.entry_support) if (cell.resource_kind === "agent") Object.assign(cell, {
    adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null,
  });
  const item = catalog.groups.flatMap((group) => group.items)[0];
  Object.assign(item, { availability: "baseline", reason_code: null });
  await page.route("**/api/v1/chat/references/catalog", (route) => route.fulfill(json(catalog)));
  await page.goto("/");
  const editor = page.locator('[data-composer-surface="home"] .tiptap');
  await expect(editor).toBeVisible();
  expect((await editor.boundingBox())!.height).toBeGreaterThanOrEqual(112);
  await editor.fill("@");
  const option = page.getByRole("option", { name: new RegExp(item.label) });
  await expect(option).toHaveAttribute("aria-disabled", "false");
  await option.click();
  await expect(editor.locator(".composer-reference-preview__reference")).toHaveText(`@${item.label}`);
  await expect(page.locator(".composer-reference-menu")).toBeHidden();
  await page.screenshot({ path: test.info().outputPath("home-rich-system-reference.png") });
});
