import { expect, test, type Locator, type Page } from "@playwright/test";

/**
 * Desktop-sidebar session deletion (docs/design/组件统一-技术设计.md “行为批 A：菜单”, session-deletion section): on hover-capable devices,
 * session-row “⋯” and inline actions appear together on hover / keyboard focus; Delete Session has no confirmation/“…” and offers Undo afterward.
 * The row has no inline Pin / Archive buttons, “⋯” is its only action (its menu holds Pin, Archive and Delete).
 * Check expanded sidebar and compact Recent Sessions popover.
 * Intercept deletion/undo requests via page.route; no database writes.
 */

const TITLE = "华东 Q3 下滑归因";
const SESSION_ID = "s2";

test.use({ viewport: { width: 1440, height: 900 } });

async function setup(page: Page) {
  const calls: string[] = [];
  await page.route(`**/api/v1/sessions/${SESSION_ID}`, async (route) => {
    if (route.request().method() !== "DELETE") return route.continue();
    calls.push("delete");
    await route.fulfill({ status: 204, body: "" });
  });
  await page.route(`**/api/v1/sessions/${SESSION_ID}/undelete`, async (route) => {
    calls.push(`undelete:${route.request().method()}`);
    await route.fulfill({ status: 204, body: "" });
  });
  await page.route("**/api/v1/metrics/event", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }),
  );
  await page.goto("/");
  return calls;
}

const rowOf = (scope: Page | Locator) => scope.locator(".unified-work-item", { hasText: TITLE });
const moreOf = (row: Locator) => row.getByRole("button", { name: `更多会话操作：${TITLE}` });
const opacityOf = (locator: Locator) =>
  locator.evaluate((node) => getComputedStyle(node.closest(".unified-work-more-wrap")!).opacity);

async function deleteAndUndo(page: Page, row: Locator, calls: string[]) {
  const del = page.getByRole("menuitem", { name: "删除会话" });
  await expect(del).toHaveText("删除会话");
  await expect(del).toHaveAttribute("data-danger", "true");
  await del.click();
  await expect(page.getByRole("dialog", { name: /删除/ })).toHaveCount(0);
  await expect.poll(() => calls).toEqual(["delete"]);
  await expect(row).toHaveCount(0);
  await expect(page.getByText(`已删除“${TITLE}”`)).toBeVisible();
  await page.getByRole("button", { name: "撤销" }).click();
  await expect.poll(() => calls).toEqual(["delete", "undelete:POST"]);
  await expect(row).toHaveCount(1);
}

test("expanded sidebar: hovering a row shows “⋯”; Delete Session deletes at once and Undo restores it", async ({ page }) => {
  const calls = await setup(page);
  const row = rowOf(page);
  await expect(row).toBeVisible();
  const more = moreOf(row);
  await page.mouse.move(900, 450);
  await expect.poll(() => opacityOf(more)).toBe("0");
  await row.hover();
  await expect.poll(() => opacityOf(more)).toBe("1");
  // “⋯” sits at far right; title yields space and remains unobscured.
  const moreBox = (await more.boundingBox())!;
  // Mouse (fine pointer): inline actions and “⋯” are 32 (touch remains 44).
  expect(Math.round(moreBox.width)).toBe(32);
  const rowBox = (await row.boundingBox())!;
  expect(Math.abs(moreBox.x + moreBox.width - (rowBox.x + rowBox.width))).toBeLessThanOrEqual(1);
  const titleBox = (await row.locator(".unified-work-title").boundingBox())!;
  // Hover shows only “⋯”; the title yields one button width (32 − 2).
  await expect(row.locator(".unified-work-actions, .unified-work-action")).toHaveCount(0);
  expect(titleBox.x + titleBox.width).toBeLessThanOrEqual(moreBox.x + 3);

  await more.click();
  // Moving pointer into menu ends row hover, but “⋯” stays visible.
  await page.getByRole("menuitem", { name: "删除会话" }).hover();
  await expect.poll(() => opacityOf(more)).toBe("1");
  await deleteAndUndo(page, rowOf(page), calls);
});

test("keyboard: Tab reaches the row menu, Enter opens it and Escape returns focus", async ({ page, browserName }) => {
  // Safari default Tab skips buttons; Option+Tab visits them (matching user keyboard navigation in Safari).
  const tab = browserName === "webkit" ? "Alt+Tab" : "Tab";
  await setup(page);
  const row = rowOf(page);
  await expect(row).toBeVisible();
  await page.mouse.move(900, 450);
  const more = moreOf(row);
  await row.locator(".unified-work-item-main").focus();
  await expect.poll(() => opacityOf(more)).toBe("0");
  let reached = false;
  for (let step = 0; step < 4 && !reached; step += 1) {
    await page.keyboard.press(tab);
    reached = await more.evaluate((node) => node === document.activeElement);
  }
  expect(reached).toBe(true);
  await expect.poll(() => opacityOf(more)).toBe("1");
  await page.keyboard.press("Enter");
  const menu = page.getByRole("menu", { name: `更多会话操作：${TITLE}` });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem").first()).toBeFocused();
  await expect(menu.getByRole("menuitem", { name: "删除会话" })).toBeVisible();
  // Rename opens a dialog; Pin follows it and Delete remains last.
  await expect(menu.getByRole("menuitem").first()).toHaveText("重命名…");
  await expect(menu.getByRole("menuitem").nth(1)).toHaveText(/^(置顶|取消置顶)$/);
  await expect(menu.getByRole("menuitem").last()).toHaveText("删除会话");
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByRole("menuitem").nth(1)).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(more).toBeFocused();
  await expect.poll(() => opacityOf(more)).toBe("1");
});

test("紧凑侧栏的「最近会话」浮层里同样能删除、撤销", async ({ page }) => {
  const calls = await setup(page);
  await page.getByRole("button", { name: "收起侧栏" }).click();
  await expect(page.getByTestId("unified-sidebar")).toHaveAttribute("data-presentation", "compact");
  await page.getByRole("button", { name: "最近会话" }).click();
  const flyout = page.locator("#unified-sidebar-work-recovery");
  await expect(flyout).toBeVisible();
  const row = rowOf(flyout);
  await row.hover();
  const more = moreOf(row);
  await expect.poll(() => opacityOf(more)).toBe("1");
  await more.click();
  await expect(flyout).toBeVisible();
  await deleteAndUndo(page, rowOf(page), calls);
});

test.describe("触屏", () => {
  test.use({ viewport: { width: 1440, height: 900 }, hasTouch: true });

  test("touch: “⋯” stays visible with a 44px touch target", async ({ page }) => {
    await setup(page);
    const row = rowOf(page);
    await expect(row).toBeVisible();
    const more = moreOf(row);
    await expect(more).toBeVisible();
    await expect.poll(() => opacityOf(more)).toBe("1");
    const box = (await more.boundingBox())!;
    expect(Math.round(box.width)).toBe(44);
    expect(Math.round(box.height)).toBe(44);
    // Title never runs under the always-visible “⋯”.
    const titleBox = (await row.locator(".unified-work-title").boundingBox())!;
    expect(titleBox.x + titleBox.width).toBeLessThanOrEqual(box.x + 3);
  });
});
