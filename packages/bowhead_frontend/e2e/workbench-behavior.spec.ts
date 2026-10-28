import { expect, test, type Locator, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * Chat Workbench and report canvas batch 4a (Workbench behavior): real-browser keyboard, drag/drop, shortcuts, new-content dot.
 * Runs Chromium and WebKit (CROSS_BROWSER_SPECS). Mock only, no real chat: sessions use ?session=, reports use
 * e2e-only __CANVAS_TEST_HYDRATE__ to fill canvas store (same hydration entry as F5).
 */

const SESSION = "s_wb4a";
const SECTIONS = [
  { id: "sec_wb_h1", kind: "h1", text: "华东区销售归因分析" },
  { id: "sec_wb_narr", kind: "narrative", markdown: "华东区 GMV 环比增长 8.3%，主要由生鲜品类拉动。" },
];
const EXTRA = { id: "sec_wb_more", kind: "narrative", markdown: "补充：华南区客单价回落 1.2%。" };

async function open(page: Page, width = 1440) {
  await page.setViewportSize({ width, height: 900 });
  await mockAuthedApp(page);
  await page.goto(`/?session=${SESSION}`);
  await expect(page.locator("textarea").first()).toBeVisible({ timeout: 15_000 });
  // Composer locked until draft read completes (attachments unavailable); wait until editable.
  await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
}

/**
 * F5 for this session. The app loads the session's canvas from the server after a reload; hydrating the
 * test report before that response lands lets the (empty) server canvas replace it. Wait for the load.
 */
async function reloadSession(page: Page) {
  const canvasLoaded = page.waitForResponse((response) =>
    response.url().includes(`/api/v1/canvas/by-session/${SESSION}`),
  );
  await page.reload();
  await canvasLoaded;
  await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
}

async function hydrate(page: Page, sections: unknown[]) {
  await page.waitForFunction(() => "__CANVAS_TEST_HYDRATE__" in window, undefined, { timeout: 15_000 });
  await page.evaluate(([sid, secs]) => {
    (window as unknown as { __CANVAS_TEST_HYDRATE__: (p: unknown) => void }).__CANVAS_TEST_HYDRATE__({
      canvas: { id: `cv_${sid}`, sessionId: sid, title: "E2E 报告" },
      sections: secs,
    });
  }, [SESSION, sections] as const);
  // Report switches Workbench to split layout, remounting chat column and rereading draft; wait until editable.
  await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
}

async function chatWidth(page: Page): Promise<number> {
  return page.locator(".v2-split-chat").evaluate((el) => Math.round(el.getBoundingClientRect().width));
}

async function hitsItself(target: Locator): Promise<boolean> {
  return target.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return Boolean(hit && (hit === el || el.contains(hit)));
  });
}

/** Dispatch file drag/drop sequence on chat column (DataTransfer constructed in browser, bypassing OS). */
async function dragFiles(page: Page, target: string, files: Array<{ name: string; type: string }>, phase: "enter" | "leave" | "drop") {
  return page.evaluate(([sel, list, ph]) => {
    const el = document.querySelector(sel) as HTMLElement;
    const dt = new DataTransfer();
    for (const f of list) dt.items.add(new File(["a,b\n1,2\n"], f.name, { type: f.type }));
    const fire = (type: string, related: EventTarget | null = null) => {
      const ev = new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt, relatedTarget: related });
      el.dispatchEvent(ev);
      return ev.defaultPrevented;
    };
    if (ph === "enter") return fire("dragenter") && fire("dragover");
    if (ph === "leave") return fire("dragleave", document.body);
    return fire("dragover") && fire("drop");
  }, [target, files, phase] as const);
}

const isMacLike = (page: Page) => page.evaluate(() => /mac/i.test(navigator.platform));

async function pressMaximize(page: Page) {
  const mod = (await isMacLike(page)) ? "Meta" : "Control";
  await page.keyboard.press(`${mod}+Shift+Backslash`);
}

test.describe("分栏线键盘与双击复位", () => {
  test("divider is focusable; ←/→ move 16px, Home / End go to the ends, double-click restores the default 40%", async ({ page }) => {
    await open(page);
    await hydrate(page, SECTIONS);
    const divider = page.getByRole("separator");
    await expect(divider).toBeVisible();
    await expect(divider).toHaveAttribute("tabindex", "0");
    const before = await chatWidth(page);
    const areaW = await page.locator(".v2-split-area").evaluate((el) => el.clientWidth);
    await divider.focus();
    await expect(divider).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect.poll(() => chatWidth(page)).toBe(before + 16);
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await expect.poll(() => chatWidth(page)).toBe(before - 16);
    await page.keyboard.press("Home");
    await expect.poll(() => chatWidth(page)).toBe(360);
    await expect(divider).toHaveAttribute("aria-valuenow", String(Math.round((360 / areaW) * 100)));
    await expect(divider).toHaveAttribute("aria-valuemin", String(Math.round((360 / areaW) * 100)));
    await page.keyboard.press("End");
    await expect.poll(() => chatWidth(page)).toBe(areaW - 420);
    await expect(divider).toHaveAttribute("aria-valuemax", String(Math.round(((areaW - 420) / areaW) * 100)));
    await divider.dblclick();
    await expect.poll(() => chatWidth(page)).toBe(Math.round(areaW * 0.4));
    await expect(divider).toHaveAttribute("aria-valuenow", "40");
    expect(await hitsItself(divider)).toBe(true);
    // Adjusted width saved locally, surviving F5.
    await page.keyboard.press("Home");
    await reloadSession(page);
    await hydrate(page, SECTIONS);
    await expect.poll(() => chatWidth(page)).toBe(360);
  });
});

test.describe("最大化快捷键 ⌘⇧\\ / Ctrl+Shift+\\", () => {
  test("pressing once maximizes the results area and again restores it, also with focus in the composer", async ({ page }) => {
    await open(page);
    await hydrate(page, SECTIONS);
    await expect(page.getByRole("separator")).toBeVisible();
    await page.locator("textarea").first().focus();
    const sidebar = page.locator("[data-presentation]").first();
    const sidebarBefore = await sidebar.getAttribute("data-presentation");
    await pressMaximize(page);
    await expect(page.locator(".v2-workspace.canvas-maxed")).toBeVisible();
    // No conflict with sidebar’s ⌘\ / Ctrl+\: sidebar state unchanged.
    expect(await sidebar.getAttribute("data-presentation")).toBe(sidebarBefore);
    await expect(page.locator(".v2-ws-edge--chat")).toBeVisible();
    await pressMaximize(page);
    await expect(page.locator(".v2-workspace.canvas-maxed")).toHaveCount(0);
    await expect(page.getByRole("separator")).toBeVisible();
    // Sidebar’s own ⌘\ (without Shift) still toggles sidebar, never maximizes.
    const mod = (await isMacLike(page)) ? "Meta" : "Control";
    await page.keyboard.press(`${mod}+Backslash`);
    await expect.poll(() => sidebar.getAttribute("data-presentation")).not.toBe(sidebarBefore);
    await expect(page.locator(".v2-workspace.canvas-maxed")).toHaveCount(0);
    // Never trigger during IME composition.
    await page.evaluate(() => window.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Process", code: "Backslash", metaKey: true, ctrlKey: true, shiftKey: true, isComposing: true, bubbles: true,
    })));
    await page.waitForTimeout(200);
    await expect(page.locator(".v2-workspace.canvas-maxed")).toHaveCount(0);
    // Button hint includes shortcut.
    const maxBtn = page.locator(".r-actions .v2-icon-btn").first();
    await expect(maxBtn).toHaveAttribute("title", /Shift|⇧/);
  });
});

test.describe("「成果」有新内容的圆点", () => {
  test("phone 412: new results while on the chat tab show a dot and screen-reader note on the switch; cleared once viewed and not back after F5", async ({ page }) => {
    await open(page, 412);
    // Local record exists for this session (zero seen) → all later Results count as new content.
    await page.evaluate((sid) => {
      localStorage.setItem("bowhead.results_seen.v1", JSON.stringify({ [sid]: { seen: [], at: Date.now() } }));
    }, SESSION);
    await reloadSession(page);
    await hydrate(page, SECTIONS);
    const tab = page.getByRole("tab", { name: /成果/ });
    await expect(tab).toHaveAttribute("data-has-new", "1");
    await expect(tab).toHaveAccessibleName(/有新内容/);
    await expect(tab.locator(".v2-ws-new-dot")).toBeVisible();
    // F5: unseen → dot persists.
    await reloadSession(page);
    await hydrate(page, SECTIONS);
    await expect(page.getByRole("tab", { name: /成果/ })).toHaveAttribute("data-has-new", "1");
    // Seen → clear.
    await page.getByRole("tab", { name: /成果/ }).click();
    await page.getByRole("tab", { name: "对话" }).click();
    await expect(page.getByRole("tab", { name: /成果/ })).not.toHaveAttribute("data-has-new", "1");
    await expect(page.getByRole("tab", { name: /成果/ })).not.toHaveAccessibleName(/有新内容/);
    await reloadSession(page);
    await hydrate(page, SECTIONS);
    await expect(page.getByRole("tab", { name: /成果/ })).toBeVisible();
    await page.waitForTimeout(300);
    await expect(page.getByRole("tab", { name: /成果/ })).not.toHaveAttribute("data-has-new", "1");
    // Another section arrives → reappears.
    await hydrate(page, [...SECTIONS, EXTRA]);
    await expect(page.getByRole("tab", { name: /成果/ })).toHaveAttribute("data-has-new", "1");
  });

  test("phone 412, live: a report written during the running turn while on the chat tab shows the dot; cleared once viewed", async ({ page }) => {
    // Mock timeline advances via setTimeout: swallow timers after 1.5 seconds to leave this turn running.
    await page.addInitScript(() => {
      const original = window.setTimeout;
      window.setTimeout = ((fn: TimerHandler, delay?: number, ...args: unknown[]) =>
        typeof delay === "number" && delay > 1500 && delay < 30_000 ? 0 : original(fn, delay, ...args)) as typeof window.setTimeout;
    });
    await page.setViewportSize({ width: 412, height: 900 });
    await mockAuthedApp(page);
    await page.goto("/?scenario=peer");
    await expect(page.locator(".bh-band__bar").first()).toBeVisible({ timeout: 15_000 });
    await page.waitForFunction(() => "__CANVAS_TEST_HYDRATE__" in window);
    await page.evaluate((secs) => (window as unknown as { __CANVAS_TEST_HYDRATE__: (p: unknown) => void }).__CANVAS_TEST_HYDRATE__({
      canvas: { id: "cv_live", sessionId: "s_live", title: "E2E" }, sections: secs,
    }), SECTIONS);
    const tab = page.getByRole("tab", { name: /成果/ });
    await expect(tab).toHaveAttribute("data-has-new", "1");
    await expect(tab).toHaveAccessibleName("成果，有新内容");
    await tab.click();
    await page.getByRole("tab", { name: "对话" }).click();
    await expect(page.getByRole("tab", { name: /成果/ })).not.toHaveAttribute("data-has-new", "1");
    await expect(page.getByRole("tab", { name: /成果/ })).toHaveAccessibleName("成果");
  });

  test("desktop 1440: new results while the results column is closed show a dot and screen-reader note on the restore bar; cleared on reopen", async ({ page }) => {
    await open(page);
    await hydrate(page, SECTIONS);
    await expect(page.getByRole("separator")).toBeVisible();
    await page.locator(".r-actions .v2-icon-btn").nth(1).click();
    const edge = page.locator(".v2-ws-edge--canvas");
    await expect(edge).toBeVisible();
    await expect(edge).not.toHaveAttribute("data-has-new", "1");
    await hydrate(page, [...SECTIONS, EXTRA]);
    await expect(edge).toHaveAttribute("data-has-new", "1");
    await expect(edge).toHaveAccessibleName(/有新内容/);
    expect(await hitsItself(edge.locator(".v2-ws-new-dot"))).toBe(true);
    await edge.click();
    await expect(page.getByRole("separator")).toBeVisible();
    await page.locator(".r-actions .v2-icon-btn").nth(1).click();
    await expect(page.locator(".v2-ws-edge--canvas")).not.toHaveAttribute("data-has-new", "1");
  });
});

test.describe("把文件拖进对话列", () => {
  test("dragging files in shows an overlay inside the chat column (not over results); dropping uploads them as attachments; a wrong type shows the reason", async ({ page }) => {
    await open(page);
    await hydrate(page, SECTIONS);
    let uploads = 0;
    await page.route("**/api/v1/files/upload**", async (route) => {
      uploads += 1;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ file_id: "art_drop_1", filename: "门店清单.csv", size: 8, mime: "text/csv" }) });
    });
    expect(await dragFiles(page, ".v2-ws-left", [{ name: "门店清单.csv", type: "text/csv" }], "enter")).toBe(true);
    const overlay = page.getByTestId("composer-file-drop");
    await expect(overlay).toBeVisible();
    await expect(overlay).toHaveAttribute("data-state", "accept");
    await expect(overlay).toContainText("松开即可添加到这条消息");
    await expect(page.getByTestId("composer-drop-status")).toContainText("松开即可添加");
    // Render within chat column, never cover Results column.
    const [overlayBox, leftBox, rightBox] = await Promise.all([
      overlay.boundingBox(),
      page.locator(".v2-ws-left").boundingBox(),
      page.locator(".v2-split-canvas").boundingBox(),
    ]);
    expect(overlayBox!.x).toBeGreaterThanOrEqual(leftBox!.x);
    expect(overlayBox!.x + overlayBox!.width).toBeLessThanOrEqual(leftBox!.x + leftBox!.width);
    expect(overlayBox!.x + overlayBox!.width).toBeLessThanOrEqual(rightBox!.x);
    // Overlay topmost: pointer-disabled overlay skipped by hit tests; temporarily enable pointers, inspect center and bottom composer positions.
    const topIsOverlay = await overlay.evaluate((el) => {
      const box = el as HTMLElement;
      box.style.pointerEvents = "auto";
      const r = box.getBoundingClientRect();
      const points = [[r.left + r.width / 2, r.top + r.height / 2], [r.left + r.width / 2, r.bottom - 40], [r.left + 60, r.bottom - 40]];
      const ok = points.every(([x, y]) => {
        const hit = document.elementFromPoint(x, y);
        return Boolean(hit && (hit === box || box.contains(hit)));
      });
      box.style.pointerEvents = "";
      return ok;
    });
    expect(topIsOverlay).toBe(true);
    await dragFiles(page, ".v2-ws-left", [], "leave");
    await expect(overlay).toHaveCount(0);

    await dragFiles(page, ".v2-ws-left", [{ name: "录屏.mp4", type: "video/mp4" }], "enter");
    await expect(page.getByTestId("composer-file-drop")).toHaveAttribute("data-state", "reject");
    await expect(page.getByTestId("composer-file-drop")).toContainText("不支持这种文件类型");
    await dragFiles(page, ".v2-ws-left", [], "leave");

    // Browser MIME variants accepted by attachment upload must not be rejected during drag.
    await dragFiles(page, ".v2-ws-left", [{ name: "说明.md", type: "text/x-markdown" }, { name: "明细.tsv", type: "text/tsv" }], "enter");
    await expect(page.getByTestId("composer-file-drop")).toHaveAttribute("data-state", "accept");
    await dragFiles(page, ".v2-ws-left", [], "leave");

    await dragFiles(page, ".v2-ws-left", [{ name: "门店清单.csv", type: "text/csv" }], "enter");
    await dragFiles(page, ".v2-ws-left", [{ name: "门店清单.csv", type: "text/csv" }], "drop");
    await expect(page.getByTestId("composer-file-drop")).toHaveCount(0);
    await expect(page.locator(".v2-composer-wrap")).toContainText("门店清单.csv");
    await expect.poll(() => uploads).toBe(1);
  });
});

test.describe("发送不可用的原因常显", () => {
  test("while an attachment uploads, the reason is written under the composer and linked to Send", async ({ page }) => {
    await open(page);
    await page.route("**/api/v1/files/upload**", () => { /* Suspend: remain uploading. */ });
    await page.locator("textarea").first().fill("按附件里的门店清单分析");
    await page.locator('input[type="file"]').first().setInputFiles({ name: "门店清单.csv", mimeType: "text/csv", buffer: Buffer.from("a,b\n1,2\n") });
    const send = page.getByRole("button", { name: "发送", exact: true });
    await expect(send).toBeDisabled();
    const note = page.getByTestId("composer-send-blocked");
    await expect(note).toBeVisible();
    await expect(note).toHaveText("等附件上传完成后才能发送");
    const noteId = await note.getAttribute("id");
    expect((await send.getAttribute("aria-describedby"))?.split(" ")).toContain(noteId);
    await expect(send).toHaveAccessibleDescription(/等附件上传完成后才能发送/);
  });
});

test.describe("报告 AI 改写：草稿与失败重试", () => {
  test("the section draft survives close and reopen; retry after a failure resends the same request", async ({ page }) => {
    await open(page);
    let calls = 0;
    const bodies: string[] = [];
    await page.route("**/api/v1/canvas/*/sections/_/rewrite", async (route) => {
      calls += 1;
      bodies.push(route.request().postData() ?? "");
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: { message: "模型暂不可用" } }) });
    });
    await hydrate(page, SECTIONS);
    await page.getByRole("button", { name: "编辑此报告" }).click();
    const narr = page.locator("[data-section-id='sec_wb_narr']").first();
    const aiBtn = page.getByRole("button", { name: "AI 改写" });
    const input = page.locator(".rc-ai-bar input");
    await narr.hover();
    await aiBtn.click();
    await input.fill("压到三句话");
    await page.locator(".rc-ai-bar .legacy-rc-ai-close").click();
    await expect(input).toHaveCount(0);
    await narr.hover();
    await aiBtn.click();
    await expect(input).toHaveValue("压到三句话");
    await input.press("Enter");
    const errorBar = page.locator(".rc-result-bar.error");
    await expect(errorBar).toBeVisible();
    await expect.poll(() => calls).toBe(1);
    const retry = errorBar.getByRole("button", { name: "重试" });
    // After failure, edit body before this text (heading end): retry still sends original text, never rewrites displaced fragment.
    await page.locator("[data-section-id='sec_wb_h1']").first().click();
    await page.keyboard.press("End");
    await page.keyboard.type("（修订）");
    await retry.click();
    await expect.poll(() => calls).toBe(2);
    expect(JSON.parse(bodies[1]).selection_text).toBe(JSON.parse(bodies[0]).selection_text);
    expect(JSON.parse(bodies[0]).selection_text).toContain("华东区 GMV 环比增长 8.3%");
    // No later UI assertions: body edit autosaves; mock server lacks report, subsequent WebKit reconciliation clears canvas.
  });
});
