import { expect, test, type Locator, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

/**
 * Real-browser behavior batch B verification (docs/design/组件统一-技术设计.md “行为批 B：窄屏弹窗与撤销”):
 * 390 touch viewport makes forms bottom sheets (bottom anchoring, handle, title/close, sticky actions); confirmation/rename stay centered cards,
 * drawers are sheets; swipe dismissal, busy close prevention, leave confirmation, focus loop/return, soft-keyboard avoidance (replace visualViewport to drive real layout);
 * Undo Toast undo chain/swipe dismissal (memory page; page.route supplies/intercepts all APIs, no database writes).
 * Fixture in `_dialog-sheet-entry.tsx`.
 */
type Scene = "form" | "busy" | "guarded" | "confirm" | "prompt" | "drawer" | "custom" | "memory" | "toast";

const T = "2026-09-22T08:00:00Z";
const SCOPE = { type: "global", space_id: null, space_name: null, availability: "available", label: "所有空间" };
const memoryItem = (trashed: boolean) => ({
  id: "m_live",
  resource_type: "memory_entry",
  display_type: "preference",
  display_value: "图表默认用柱状图。",
  display_schema: { name: "text", payload: {}, editable_fields: ["value"] },
  scope: SCOPE,
  provenance: {
    created_from: { type: "chat", source_label: "对话", source_available: false, source_open_token: null },
    last_modified: { actor_type: null, actor_id: null, modified_at: null },
  },
  capabilities: { edit: !trashed, soft_delete: !trashed, restore: trashed, permanent_delete: trashed },
  locked: false,
  version: "3",
  created_at: T,
  updated_at: T,
  deleted_at: trashed ? T : null,
  purge_at: trashed ? "2026-10-22T08:00:00Z" : null,
});

interface MemoryRequests {
  deletes: string[];
  restores: string[];
  itemLoads: number;
}

async function routeMemory(page: Page): Promise<MemoryRequests> {
  const requests: MemoryRequests = { deletes: [], restores: [], itemLoads: 0 };
  let trashed = false;
  await page.route("**/api/v1/memory/overview", (route) =>
    route.fulfill({
      json: {
        schema_version: 2,
        about_you: {
          content: "", scope: SCOPE, version: "0", updated_at: null, is_locked: false,
          capabilities: { edit: true, soft_delete: false, restore: false, permanent_delete: false },
        },
        suggestions: [],
        stats: { notes_configured: false, saved_entries: 1, pending_candidates: 0, spaces_represented: 1, trash_count: 0, recoverable_resets: 0 },
        learning: { automatic_learning: "active" },
        capabilities: {
          v2_enabled: true, v2_ready: true, presentation: "v2", schema_version: 2, kind_policy_version: 1,
          actions: { edit: true, permanent_delete: true, recent_resets: true, destructive_operations: true },
          permanent_delete_ready: true, permanent_delete_reason: null, reason: null,
        },
      },
    }),
  );
  await page.route("**/api/v1/memory/items?**", (route) => {
    requests.itemLoads += 1;
    const url = new URL(route.request().url());
    const wantTrash = url.searchParams.get("state") === "trashed";
    const items = wantTrash ? (trashed ? [memoryItem(true)] : []) : trashed ? [] : [memoryItem(false)];
    return route.fulfill({ json: { items, next_cursor: null } });
  });
  await page.route("**/api/v1/memory/resets**", (route) => route.fulfill({ json: { items: [] } }));
  // Write requests: record only, never persist.
  await page.route("**/api/v1/memory/m_live", (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    requests.deletes.push(route.request().headers()["if-match"] ?? "");
    trashed = true;
    return route.fulfill({ json: { deleted: "m_live" } });
  });
  await page.route("**/api/v1/memory/m_live/restore", (route) => {
    requests.restores.push(route.request().headers()["if-match"] ?? "");
    trashed = false;
    return route.fulfill({ json: { id: "m_live", restored: true } });
  });
  return requests;
}

async function open(page: Page, scene: Scene, routes?: () => Promise<void>) {
  // Register fallback first: Playwright matches reverse registration order, so later specific endpoints take precedence.
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: "{}" }),
  );
  await routes?.();
  await installReferenceComponent(page, "", "_dialog-sheet-entry.tsx");
  await page.goto("/__reference_component_test");
  await expect(page.getByRole("heading", { name: "dialog sheet" })).toBeVisible();
  await page.evaluate((value) => window.dialogSheetScene.show(value as never), scene);
}

/** Wait for entrance animation (sheet sliding up) before measuring. */
async function settle(locator: Locator) {
  await locator.evaluate((node) =>
    Promise.all(
      [node, ...Array.from(node.querySelectorAll("*"))].flatMap((el) => el.getAnimations()).map((a) => a.finished),
    ),
  );
}

/** Probe elementFromPoint at center/corners inset 24px; return points hitting neither element nor descendants. */
async function coveredPoints(locator: Locator): Promise<string[]> {
  return locator.evaluate((node) => {
    const r = node.getBoundingClientRect();
    const points: Array<[number, number]> = [
      [r.left + r.width / 2, r.top + r.height / 2],
      [r.left + 24, r.top + 24],
      [r.right - 24, r.top + 24],
      [r.left + 24, r.bottom - 24],
      [r.right - 24, r.bottom - 24],
    ];
    return points
      .map(([x, y]) => [x, y, document.elementFromPoint(x, y)] as const)
      .filter(([, , hit]) => !hit || !node.contains(hit))
      .map(([x, y, hit]) => `${Math.round(x)},${Math.round(y)}:${hit ? hit.className : "(nothing)"}`);
  });
}

async function dragDown(page: Page, handle: Locator, distance: number) {
  const g = (await handle.boundingBox())!;
  await page.mouse.move(g.x + g.width / 2, g.y + g.height / 2);
  await page.mouse.down();
  await page.mouse.move(g.x + g.width / 2, g.y + g.height / 2 + distance / 2, { steps: 4 });
  await page.mouse.move(g.x + g.width / 2, g.y + g.height / 2 + distance, { steps: 4 });
  await page.mouse.up();
}

/** Touch swipe: dispatch pointerType=touch events (Playwright touchscreen supports taps only). */
async function touchSwipe(target: Locator, dx: number, dy: number) {
  await target.evaluate(
    (node, [dx, dy]) => {
      const r = node.getBoundingClientRect();
      const x = r.left + r.width / 3;
      const y = r.top + r.height / 2;
      const fire = (type: string, px: number, py: number) =>
        node.dispatchEvent(
          new PointerEvent(type, { bubbles: true, cancelable: true, pointerId: 11, pointerType: "touch", isPrimary: true, clientX: px, clientY: py }),
        );
      fire("pointerdown", x, y);
      for (let step = 1; step <= 5; step += 1) fire("pointermove", x + (dx * step) / 5, y + (dy * step) / 5);
      fire("pointerup", x + dx, y + dy);
    },
    [dx, dy] as const,
  );
}

test.describe("390 触屏", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test("表单弹窗是底部面板：贴底全宽、抓手、标题栏与关闭按钮、操作栏吸底且按钮 44 高，不被别的层压住", async ({ page }) => {
    await open(page, "form");
    await page.getByRole("button", { name: "添加连接" }).click();
    const sheet = page.locator(".ui-dialog-sheet");
    await expect(sheet).toBeVisible();
    await settle(sheet);
    const box = (await sheet.boundingBox())!;
    expect(Math.round(box.x)).toBe(0);
    expect(Math.round(box.width)).toBe(390);
    expect(Math.round(box.y + box.height)).toBe(844);
    expect(box.height).toBeLessThanOrEqual(844 * 0.92 + 1);
    const radius = await sheet.evaluate((node) => getComputedStyle(node).borderTopLeftRadius);
    expect(radius).toBe("16px");
    await expect(page.locator(".ui-dialog-sheet-grab")).toBeVisible();
    const dialog = page.getByRole("dialog", { name: "添加连接" });
    await expect(dialog.getByRole("button", { name: "关闭" })).toBeVisible();
    const footer = dialog.locator(".settings-dialog__footer");
    const f = (await footer.boundingBox())!;
    expect(Math.round(f.y + f.height)).toBe(844);
    for (const name of ["取消", "保存"]) {
      const b = (await footer.getByRole("button", { name }).boundingBox())!;
      expect(Math.round(b.height)).toBeGreaterThanOrEqual(44);
    }
    const cancel = (await footer.getByRole("button", { name: "取消" }).boundingBox())!;
    const save = (await footer.getByRole("button", { name: "保存" }).boundingBox())!;
    expect(Math.abs(cancel.width - save.width)).toBeLessThanOrEqual(1);
    expect(save.x).toBeGreaterThan(cancel.x);
    expect(await coveredPoints(sheet)).toEqual([]);
    // Body scrolls independently: actions remain at bottom regardless of content length.
    await dialog.locator(".settings-dialog__body").evaluate((node) => {
      const filler = document.createElement("div");
      filler.style.height = "1600px";
      node.append(filler);
    });
    const f2 = (await footer.boundingBox())!;
    expect(Math.round(f2.y + f2.height)).toBe(844);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  });

  test("确认框与重命名框仍是居中卡片", async ({ page }) => {
    await open(page, "confirm");
    await page.getByRole("button", { name: "删除报告" }).click();
    const confirm = page.getByRole("dialog", { name: "删除这份报告？" });
    await expect(confirm).toBeVisible();
    await expect(page.locator(".ui-dialog-sheet")).toHaveCount(0);
    await settle(confirm);
    const c = (await confirm.boundingBox())!;
    expect(c.x).toBeGreaterThanOrEqual(15);
    expect(c.x + c.width).toBeLessThanOrEqual(390 - 15);
    expect(Math.abs(c.y + c.height / 2 - 844 / 2)).toBeLessThan(40);
    await expect(confirm.getByRole("button", { name: "取消" })).toBeFocused();
    await page.keyboard.press("Escape");

    await page.evaluate(() => window.dialogSheetScene.show("prompt"));
    await page.getByRole("button", { name: "重命名" }).click();
    await expect(page.getByRole("dialog", { name: "重命名" })).toBeVisible();
    await expect(page.locator(".ui-dialog-sheet")).toHaveCount(0);
  });

  test("抽屉（邀请成员）在手机上是底部面板；自写弹窗的底栏吸底", async ({ page }) => {
    await open(page, "drawer");
    await page.getByRole("button", { name: "邀请成员" }).click();
    const sheet = page.locator(".ui-dialog-sheet");
    await expect(sheet).toBeVisible();
    await settle(sheet);
    const box = (await sheet.boundingBox())!;
    expect(Math.round(box.width)).toBe(390);
    expect(Math.round(box.y + box.height)).toBe(844);
    expect(box.height).toBeLessThan(844 * 0.92);
    expect(await coveredPoints(sheet)).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);

    await page.evaluate(() => window.dialogSheetScene.show("custom"));
    await page.getByRole("button", { name: "绑定文档类型" }).click();
    await expect(sheet).toBeVisible();
    await settle(sheet);
    const foot = page.locator(".kb-modal-foot");
    const panel = page.getByRole("dialog", { name: "绑定文档类型" });
    const footBox = (await foot.boundingBox())!;
    expect(Math.round(footBox.y + footBox.height)).toBe(844);
    await panel.evaluate((node) => node.scrollTo(0, node.scrollHeight / 2));
    const footBox2 = (await foot.boundingBox())!;
    expect(Math.round(footBox2.y + footBox2.height)).toBe(844);
    expect(await coveredPoints(sheet)).toEqual([]);
  });

  test("按住抓手下滑关闭，焦点回到触发按钮；拖一点松手弹回", async ({ page }) => {
    await open(page, "form");
    const trigger = page.getByRole("button", { name: "添加连接" });
    // Open by keyboard: WebKit button clicks do not focus, so click-opened dialogs cannot return focus to button (unrelated to this batch).
    await trigger.focus();
    await page.keyboard.press("Enter");
    const sheet = page.locator(".ui-dialog-sheet");
    await expect(sheet).toBeVisible();
    await settle(sheet);
    const grab = page.locator(".ui-dialog-sheet-grab");
    const g = (await grab.boundingBox())!;
    await page.mouse.move(g.x + g.width / 2, g.y + g.height / 2);
    await page.mouse.down();
    await page.mouse.move(g.x + g.width / 2, g.y + g.height / 2 + 30, { steps: 6 });
    await page.waitForTimeout(300);
    await page.mouse.up();
    await expect(sheet).toBeVisible();
    await expect.poll(() => sheet.evaluate((node) => node.style.transform)).toBe("");
    // Wait for snap-back transition before measuring title-bar position.
    await expect.poll(() => sheet.evaluate((node) => getComputedStyle(node).transform)).toBe("none");

    await dragDown(page, page.locator(".ui-dialog-sheet-head .settings-dialog__title"), 200);
    await expect(sheet).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test("busy 时下滑、Esc、点遮罩都关不掉，关闭按钮不可用", async ({ page }) => {
    await open(page, "busy");
    await page.getByRole("button", { name: "添加连接" }).click();
    const sheet = page.locator(".ui-dialog-sheet");
    await expect(sheet).toBeVisible();
    await settle(sheet);
    await dragDown(page, page.locator(".ui-dialog-sheet-grab"), 240);
    await page.keyboard.press("Escape");
    await page.mouse.click(195, 20);
    await expect(sheet).toBeVisible();
    await expect(page.getByRole("dialog", { name: "添加连接" }).getByRole("button", { name: "关闭" })).toBeDisabled();
    expect(await page.evaluate(() => window.dialogSheetEvents.filter((e) => e === "close-request").length)).toBe(0);
  });

  test("有离开确认时下滑走原确认流程：确认框是盖在面板上的居中卡片，取消后面板仍在", async ({ page }) => {
    await open(page, "guarded");
    await page.getByRole("button", { name: "添加连接" }).click();
    const sheet = page.locator(".ui-dialog-sheet");
    await expect(sheet).toBeVisible();
    await settle(sheet);
    await dragDown(page, page.locator(".ui-dialog-sheet-grab"), 200);
    const confirm = page.getByRole("dialog", { name: "放弃未保存的内容？" });
    await expect(confirm).toBeVisible();
    await expect(sheet).toBeVisible();
    await settle(confirm);
    expect(await coveredPoints(confirm)).toEqual([]);
    await confirm.getByRole("button", { name: "取消" }).click();
    await expect(confirm).toHaveCount(0);
    await expect(sheet).toBeVisible();
    await dragDown(page, page.locator(".ui-dialog-sheet-grab"), 200);
    await page.getByRole("dialog", { name: "放弃未保存的内容？" }).getByRole("button", { name: "放弃" }).click();
    await expect(sheet).toHaveCount(0);
  });

  test("焦点：打开后在面板上（不自动弹键盘），Tab / Shift+Tab 在面板里首尾循环，背景 inert，Esc 关闭回焦", async ({ page }) => {
    await open(page, "form");
    const trigger = page.getByRole("button", { name: "添加连接" });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "添加连接" });
    await expect(dialog).toBeVisible();
    await expect(dialog).toBeFocused();
    expect(await page.locator("#before").evaluate((node) => Boolean(node.closest("[inert]")))).toBe(true);
    expect(await page.locator(".ui-dialog-sheet-grab").evaluate((node) => Boolean(node.closest("[inert]")))).toBe(false);
    expect(await dialog.evaluate((node) => Boolean(node.closest("[inert]")))).toBe(false);
    const close = dialog.getByRole("button", { name: "关闭" });
    const save = dialog.getByRole("button", { name: "保存" });
    await save.focus();
    await page.keyboard.press("Tab");
    await expect(close).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(save).toBeFocused();
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press("Tab");
      expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
    }
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(await page.locator("#before").evaluate((node) => Boolean(node.closest("[inert]")))).toBe(false);
  });

  test("软键盘避让：可见区域变矮时面板抬到键盘上方、操作栏与输入框都在可见区域里", async ({ page }) => {
    // Playwright cannot open real keyboard; controlled visualViewport drives real layout (iOS: unchanged layout viewport, shorter visible area).
    await page.addInitScript(() => {
      const target = new EventTarget() as EventTarget & { height: number; width: number; offsetTop: number; offsetLeft: number; scale: number };
      Object.assign(target, { height: window.innerHeight, width: window.innerWidth, offsetTop: 0, offsetLeft: 0, scale: 1 });
      Object.defineProperty(window, "visualViewport", { configurable: true, get: () => target });
      (window as unknown as { fakeKeyboard: (h: number) => void }).fakeKeyboard = (h: number) => {
        target.height = window.innerHeight - h;
        target.dispatchEvent(new Event("resize"));
      };
    });
    await open(page, "form");
    await page.getByRole("button", { name: "添加连接" }).click();
    const sheet = page.locator(".ui-dialog-sheet");
    await expect(sheet).toBeVisible();
    await settle(sheet);
    const note = page.getByRole("dialog", { name: "添加连接" }).locator("textarea");
    await note.focus();
    await page.evaluate(() => (window as unknown as { fakeKeyboard: (h: number) => void }).fakeKeyboard(340));
    await expect(sheet).toHaveAttribute("data-keyboard", "open");
    const visibleBottom = 844 - 340;
    await expect.poll(async () => Math.round((await sheet.boundingBox())!.y + (await sheet.boundingBox())!.height)).toBe(visibleBottom);
    const box = (await sheet.boundingBox())!;
    expect(box.y).toBeGreaterThanOrEqual(0);
    const footer = (await page.locator(".settings-dialog__footer").boundingBox())!;
    expect(footer.y + footer.height).toBeLessThanOrEqual(visibleBottom + 1);
    const field = (await note.boundingBox())!;
    expect(field.y).toBeGreaterThanOrEqual(box.y);
    expect(field.y + field.height).toBeLessThanOrEqual(footer.y + 1);
    await page.evaluate(() => (window as unknown as { fakeKeyboard: (h: number) => void }).fakeKeyboard(0));
    await expect(sheet).not.toHaveAttribute("data-keyboard", "open");
    await expect.poll(async () => Math.round((await sheet.boundingBox())!.y + (await sheet.boundingBox())!.height)).toBe(844);
  });

  test("撤销链路：记忆移入回收站不确认、直接执行，撤销后调用恢复接口（不带删除前版本）并刷新列表", async ({ page }) => {
    let requests!: MemoryRequests;
    await open(page, "memory", async () => {
      requests = await routeMemory(page);
    });
    await expect(page.getByText("图表默认用柱状图。")).toBeVisible();
    await page.getByRole("button", { name: "「图表默认用柱状图。」的更多操作" }).first().click();
    // Undoable deletion no longer confirms (user decision 2026-09-28): selecting executes.
    await page.getByRole("menuitem", { name: "移入回收站", exact: true }).click();
    await expect.poll(() => requests.deletes.length).toBe(1);
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await expect(page.getByText("图表默认用柱状图。")).toHaveCount(0);
    const undoToast = page.getByTestId("toast-info").filter({ hasText: "已移入回收站" });
    await expect(undoToast).toBeVisible();
    await expect(undoToast).toHaveClass(/ui-toast--undo/);
    // Mobile Toast at top, never covered by bottom sheet or backdrop.
    expect(await coveredPoints(undoToast)).toEqual([]);
    const loads = requests.itemLoads;
    await undoToast.getByRole("button", { name: "撤销" }).click();
    await expect.poll(() => requests.restores).toEqual([""]);
    await expect.poll(() => requests.itemLoads).toBeGreaterThan(loads);
    await expect(page.getByText("图表默认用柱状图。")).toBeVisible();
    await expect(page.getByText("记忆已恢复")).toBeVisible();
  });

  test("Toast 向上或左右轻扫关闭（照常结束撤销窗口）；拖一点弹回", async ({ page }) => {
    await open(page, "toast");
    const show = page.getByRole("button", { name: "显示撤销提示" });
    await show.click();
    let item = page.getByTestId("toast-info");
    await expect(item).toBeVisible();
    await touchSwipe(item, 0, -10);
    await expect(item).toBeVisible();
    await touchSwipe(item, 0, -80);
    await expect(item).toHaveCount(0);
    expect(await page.evaluate(() => window.dialogSheetEvents)).toEqual(["dismiss"]);

    await show.click();
    item = page.getByTestId("toast-info");
    await expect(item).toBeVisible();
    await touchSwipe(item, 140, 0);
    await expect(item).toHaveCount(0);
    expect(await page.evaluate(() => window.dialogSheetEvents)).toEqual(["dismiss", "dismiss"]);
  });
});

test.describe("桌面 1440", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the form dialog stays a centered dialog (the host takes no box) and focuses the first field", async ({ page }) => {
    await open(page, "form");
    await page.getByRole("button", { name: "添加连接" }).click();
    const dialog = page.getByRole("dialog", { name: "添加连接" });
    await expect(dialog).toBeVisible();
    await expect(page.locator(".ui-dialog-sheet")).toHaveCount(0);
    await expect(page.locator(".ui-dialog-sheet-host")).toHaveCount(1);
    await settle(dialog);
    const box = (await dialog.boundingBox())!;
    expect(Math.abs(box.x + box.width / 2 - 720)).toBeLessThan(2);
    await expect(dialog.getByRole("textbox", { name: "连接名称" })).toBeFocused();
  });

  test("撤销 Toast 桌面在右上角、不带状态标记；鼠标拖动不关闭", async ({ page }) => {
    await open(page, "toast");
    await page.getByRole("button", { name: "显示撤销提示" }).click();
    const item = page.getByTestId("toast-info");
    await expect(item).toBeVisible();
    await expect(item.locator(".legacy-toast-dot")).toBeHidden();
    const b = (await item.boundingBox())!;
    await page.mouse.move(b.x + 40, b.y + b.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + 40, b.y - 100, { steps: 6 });
    await page.mouse.up();
    await expect(item).toBeVisible();
  });
});
