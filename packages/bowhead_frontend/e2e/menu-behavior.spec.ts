import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import { probeLayer } from "./_layer-probe";

/**
 * Real-browser behavior batch A (docs/design/组件统一-技术设计.md “行为批 A：菜单”):
 * at 390 touch width, long menus are bottom sheets, short ones remain popovers; account Language is a desktop submenu (keyboard open/close, hover),
 * narrow screens drill down inside the sheet; dangerous items use “…” and confirm first (except undoable session deletion); sheets are modal; space-picker hints do not cover highlights; Esc/focus return. Fixture: `_menu-behavior-entry.tsx`;
 * intercept all APIs here, no database writes.
 */
type Scene = "account" | "long-menu" | "short-menu" | "sessions" | "row-menu" | "space-picker";

async function open(page: Page, scene: Scene) {
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: "{}" }),
  );
  await page.route("**/api/v1/auth/me/workspaces**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: Array.from({ length: 15 }, (_, index) => ({
          workspace_id: `ws_${index + 1}`,
          name: index === 0 ? "经营分析" : `空间 ${index + 1}`,
          role: index === 0 ? "admin" : "member",
        })),
        active_id: "ws_1",
      }),
    }),
  );
  await installReferenceComponent(page, "", "_menu-behavior-entry.tsx");
  await page.goto("/__reference_component_test");
  await expect(page.getByRole("heading", { name: "menu behavior" })).toBeVisible();
  await page.evaluate((value) => window.menuBehaviorScene.show(value as never), scene);
}

/** Sample center/corners inset 24px with elementFromPoint; return points hitting neither the element nor descendants. */
async function coveredPoints(locator: import("@playwright/test").Locator): Promise<string[]> {
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

/** Wait for entry animations (fade/scale, sheet slide-up) before measuring positions. */
async function settle(locator: import("@playwright/test").Locator) {
  await locator.evaluate((node) =>
    Promise.all(
      [node, ...Array.from(node.querySelectorAll("*"))].flatMap((el) => el.getAnimations()).map((a) => a.finished),
    ),
  );
}

test.describe("390 触屏", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test("长菜单是底部面板（贴底、遮罩、抓手、行高 44），不被别的层压住；Esc 关闭并回到触发按钮", async ({ page }) => {
    await open(page, "long-menu");
    const trigger = page.getByRole("button", { name: "长菜单" });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const menu = page.getByRole("menu", { name: "长菜单" });
    await expect(menu).toHaveClass(/is-sheet/);
    const sheet = page.locator(".ui-menu-sheet");
    await expect(sheet).toBeVisible();
    await expect(page.locator(".ui-menu-sheet-scrim")).toBeVisible();
    await expect(page.locator(".ui-menu-sheet-grab")).toBeVisible();
    await settle(sheet);
    const box = (await sheet.boundingBox())!;
    expect(Math.round(box.x)).toBe(8);
    expect(Math.round(box.width)).toBe(390 - 16);
    expect(Math.round(box.y + box.height)).toBeGreaterThanOrEqual(844 - 8 - 1);
    expect(Math.round(box.y + box.height)).toBeLessThanOrEqual(844 - 8 + 1);
    const itemBox = (await menu.getByRole("menuitem").first().boundingBox())!;
    expect(Math.round(itemBox.height)).toBeGreaterThanOrEqual(44);
    // Sheet corners have 20px radius; specified sampling hits the backdrop, so sample five points inset 24px instead.
    expect(await coveredPoints(sheet)).toEqual([]);

    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
    await expect(page.locator(".ui-menu-sheet-scrim")).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test("点遮罩关闭；按住抓手下滑关闭", async ({ page }) => {
    await open(page, "long-menu");
    const trigger = page.getByRole("button", { name: "长菜单" });
    await trigger.click();
    await expect(page.locator(".ui-menu-sheet")).toBeVisible();
    await page.mouse.click(195, 60);
    await expect(page.locator(".ui-menu-sheet")).toHaveCount(0);

    await trigger.click();
    const grab = page.locator(".ui-menu-sheet-grab");
    await expect(grab).toBeVisible();
    await settle(page.locator(".ui-menu-sheet"));
    const g = (await grab.boundingBox())!;
    await page.mouse.move(g.x + g.width / 2, g.y + g.height / 2);
    await page.mouse.down();
    await page.mouse.move(g.x + g.width / 2, g.y + g.height / 2 + 60, { steps: 4 });
    await page.mouse.move(g.x + g.width / 2, g.y + g.height / 2 + 160, { steps: 4 });
    await page.mouse.up();
    await expect(page.locator(".ui-menu-sheet")).toHaveCount(0);
  });

  test("短菜单（≤ 6 项）仍是浮层，贴着触发按钮，触屏行高 44", async ({ page }) => {
    await open(page, "short-menu");
    const trigger = page.getByRole("button", { name: "短菜单" });
    await trigger.click();
    const menu = page.getByRole("menu", { name: "短菜单" });
    await expect(menu).toBeVisible();
    await expect(menu).not.toHaveClass(/is-sheet/);
    await expect(page.locator(".ui-menu-sheet, .ui-menu-sheet-scrim")).toHaveCount(0);
    await settle(menu);
    const t = (await trigger.boundingBox())!;
    const m = (await menu.boundingBox())!;
    expect(m.y).toBeGreaterThan(t.y + t.height - 1);
    expect(m.y).toBeLessThan(t.y + t.height + 20);
    const itemBox = (await menu.getByRole("menuitem").first().boundingBox())!;
    expect(Math.round(itemBox.height)).toBe(44);
  });

  test("账户菜单是底部面板；「语言」在同一面板内下钻，「‹ 账户」返回，Esc 逐级返回", async ({ page }) => {
    await open(page, "account");
    const trigger = page.getByRole("button", { name: "账户" });
    await trigger.click();
    const account = page.locator('[data-user-menu="true"]');
    await expect(account).toHaveClass(/is-sheet/);
    const language = page.getByRole("menuitem", { name: /^语言/ });
    await expect(language).toHaveAttribute("aria-haspopup", "menu");
    await language.click();
    const sub = page.getByRole("menu", { name: "语言" });
    await expect(sub).toBeVisible();
    await expect(page.locator(".ui-menu-sheet")).toHaveAttribute("data-drilled", "true");
    await expect(account).toBeHidden();
    await expect(page.locator(".ui-menu-sheet")).toHaveCount(1);
    const probe = await probeLayer(sub);
    expect(probe.covered).toEqual([]);
    await expect(sub.getByRole("menuitemradio", { name: /^自动/ })).toHaveAttribute("aria-checked", "true");
    const back = sub.locator(".ui-menu-drill-back");
    await expect(back).toHaveText("账户");
    await back.click();
    await expect(sub).toHaveCount(0);
    await expect(account).toBeVisible();
    await expect(language).toBeFocused();

    await language.press("ArrowRight");
    await expect(sub).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sub).toHaveCount(0);
    await expect(account).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(account).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test("危险项带「…」，选中先弹确认框（焦点在「取消」），取消不删，Esc 关闭", async ({ page }) => {
    await open(page, "row-menu");
    const trigger = page.getByRole("button", { name: "季度复盘 的更多操作" });
    await trigger.click();
    const del = page.getByRole("menuitem", { name: "删除…" });
    await expect(del).toHaveClass(/is-danger/);
    await del.click();
    const dialog = page.getByRole("dialog", { name: "删除这份文件？" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "删除" })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "取消" })).toBeFocused();
    expect(await coveredPoints(dialog)).toEqual([]);
    await dialog.getByRole("button", { name: "取消" }).click();
    await expect(dialog).toHaveCount(0);
    expect(await page.evaluate(() => window.deletedSessions)).toEqual([]);

    await trigger.click();
    await page.getByRole("menuitem", { name: "删除…" }).click();
    await expect(dialog).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(await page.evaluate(() => window.deletedSessions)).toEqual([]);
  });

  test("删除会话不确认、不带「…」：直接删除（能撤销的删除不再确认）", async ({ page }) => {
    await open(page, "sessions");
    await page.getByRole("button", { name: /^更多会话操作/ }).first().click();
    const del = page.getByRole("menuitem", { name: "删除会话" });
    await expect(del).toHaveAttribute("data-danger", "true");
    await del.click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => window.deletedSessions)).toEqual(["s_1"]);
  });

  test("底部面板是模态：Tab 与 Shift+Tab 留在面板里，背景 inert 点不动也拿不到焦点；关闭后回焦", async ({ page }) => {
    await open(page, "long-menu");
    const trigger = page.getByRole("button", { name: "长菜单" });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const sheet = page.locator(".ui-menu-sheet");
    await expect(page.getByRole("menu", { name: "长菜单" })).toHaveClass(/is-sheet/);
    for (const key of ["Tab", "Tab", "Shift+Tab", "Tab"]) {
      await page.keyboard.press(key);
      expect(await sheet.evaluate((node) => node.contains(document.activeElement))).toBe(true);
    }
    expect(await page.locator("#before").evaluate((node) => Boolean(node.closest("[inert]")))).toBe(true);
    expect(await sheet.evaluate((node) => Boolean(node.closest("[inert]")))).toBe(false);
    expect(await page.locator(".ui-menu-sheet-scrim").evaluate((node) => Boolean(node.closest("[inert]")))).toBe(false);
    await page.getByRole("button", { name: "before" }).focus();
    expect(await sheet.evaluate((node) => node.contains(document.activeElement))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(await page.locator("#before").evaluate((node) => Boolean(node.closest("[inert]")))).toBe(false);
  });

  test("底部面板打开时页面不能滚动；在已加锁的页面里开关面板，页面的滚动锁仍在", async ({ page }) => {
    await open(page, "long-menu");
    await page.getByRole("button", { name: "长菜单" }).click();
    await expect(page.locator(".ui-menu-sheet")).toBeVisible();
    expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe("hidden");
    await page.keyboard.press("Escape");
    await expect(page.locator(".ui-menu-sheet")).toHaveCount(0);
    await page.evaluate(() => document.body.style.setProperty("overflow", "hidden", "important"));
    await page.getByRole("button", { name: "长菜单" }).click();
    await expect(page.locator(".ui-menu-sheet")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator(".ui-menu-sheet")).toHaveCount(0);
    expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe("hidden");
  });

  test("运行中的会话：禁用项的原因第二行可见，键盘可以停在上面", async ({ page }) => {
    await open(page, "sessions");
    // Open by keyboard here to check focus can stop on a disabled item.
    const trigger = page.getByRole("button", { name: /^更多会话操作/ }).nth(1);
    await trigger.focus();
    await page.keyboard.press("Enter");
    const archive = page.getByRole("menuitem", { name: "归档会话" });
    await expect(archive).toBeVisible();
    await expect(page.getByText("这项工作仍在运行，完成或停止后才能归档。")).toBeVisible();
    await page.keyboard.press("ArrowDown");
    await expect(archive).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(archive).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });
});

test.describe("桌面 1440", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("账户菜单：→ 打开「语言」子菜单（与父项顶对齐、水平重叠 4px），← 返回父项，Esc 关闭并回到触发按钮", async ({ page }) => {
    await open(page, "account");
    const trigger = page.getByRole("button", { name: "账户" });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const account = page.locator('[data-user-menu="true"]');
    await expect(account).toBeVisible();
    await expect(account).not.toHaveClass(/is-sheet/);
    const language = page.getByRole("menuitem", { name: /^语言/ });
    await language.focus();
    await page.keyboard.press("ArrowRight");
    const sub = page.getByRole("menu", { name: "语言" });
    await expect(sub).toBeVisible();
    await expect(sub.getByRole("menuitemradio", { name: /^自动/ })).toBeFocused();
    await expect(language).toHaveAttribute("aria-expanded", "true");

    await settle(sub);
    await settle(account);
    const parentItem = (await language.boundingBox())!;
    const parentMenu = (await account.boundingBox())!;
    const subBox = (await sub.boundingBox())!;
    expect(Math.abs(subBox.y - parentItem.y)).toBeLessThanOrEqual(1);
    const rightSide = subBox.x > parentMenu.x;
    if (rightSide) expect(Math.abs(parentMenu.x + parentMenu.width - subBox.x - 4)).toBeLessThanOrEqual(1);
    else expect(Math.abs(subBox.x + subBox.width - parentMenu.x - 4)).toBeLessThanOrEqual(1);
    const probe = await probeLayer(sub);
    expect(probe.covered).toEqual([]);

    await page.keyboard.press("ArrowDown");
    await expect(sub.getByRole("menuitemradio", { name: "中文", exact: true })).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(sub.getByRole("menuitemradio", { name: "English" })).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(sub).toHaveCount(0);
    await expect(language).toBeFocused();
    await expect(account).toBeVisible();

    await page.keyboard.press("ArrowRight");
    await expect(sub).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sub).toHaveCount(0);
    await expect(language).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(account).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test("账户菜单：悬停「语言」约 150ms 后打开子菜单，指针斜穿进子菜单不关", async ({ page }) => {
    await open(page, "account");
    await page.getByRole("button", { name: "账户" }).click();
    const language = page.getByRole("menuitem", { name: /^语言/ });
    const box = (await language.boundingBox())!;
    await page.mouse.move(box.x + 20, box.y + box.height / 2);
    const sub = page.getByRole("menu", { name: "语言" });
    await expect(sub).toBeVisible();
    await settle(sub);
    const target = sub.getByRole("menuitemradio", { name: "English" });
    const t = (await target.boundingBox())!;
    await page.mouse.move(t.x + t.width / 2, t.y + t.height / 2, { steps: 12 });
    await expect(sub).toBeVisible();
    await expect(target).toBeVisible();
  });

  test("空间切换器：15 个空间连按 ↓，每一项都完整可见，不被底部的键盘提示条盖住", async ({ page }) => {
    await open(page, "space-picker");
    const chip = page.getByRole("button", { name: /经营分析/ });
    await expect(chip).toBeEnabled();
    await chip.focus();
    await page.keyboard.press("Enter");
    const hints = page.locator(".ui-menu-hints");
    await expect(hints).toBeVisible();
    // Keyboard opening focuses search; ↓ enters the list, then moves down to the last item (stop before wrapping).
    await expect(page.getByRole("searchbox")).toBeFocused();
    const items = page.getByRole("menuitemradio");
    await expect(items).toHaveCount(15);
    const seen = new Set<string>();
    for (let step = 0; step < 15; step += 1) {
      await page.keyboard.press("ArrowDown");
      const state = await page.evaluate(() => {
        const node = document.activeElement as HTMLElement;
        if (node?.getAttribute("role") !== "menuitemradio") return null;
        const menu = node.closest(".ui-menu")!;
        const bar = menu.querySelector(".ui-menu-hints")!.getBoundingClientRect();
        const rect = node.getBoundingClientRect();
        const view = menu.getBoundingClientRect();
        return {
          name: node.textContent ?? "",
          underBar: Math.max(0, Math.round(rect.bottom - bar.top)),
          aboveTop: Math.max(0, Math.round(view.top - rect.top)),
        };
      });
      expect(state, `第 ${step + 1} 次 ↓`).not.toBeNull();
      expect({ underBar: state!.underBar, aboveTop: state!.aboveTop }, state!.name).toEqual({ underBar: 0, aboveTop: 0 });
      seen.add(state!.name);
      if (state!.name.includes("空间 15")) break;
    }
    expect([...seen].some((name) => name.includes("空间 15"))).toBe(true);
  });
});
