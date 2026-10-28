import { expect, test, type Page } from "@playwright/test";

const ARCHIVED_SESSION = {
  id: "s2",
  title: "华东 Q3 下滑归因",
  intents: ["causal", "lookup"],
  time: "36m ago",
  state: "done",
  archived: true,
};

test.beforeEach(async ({ page }) => {
  await installArchiveApi(page);
  await page.route("**/health", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        version: "e2e",
        frontend_min: "e2e",
        force_reload: false,
        feature_flags: { session_archive: true },
      }),
    });
  });
  await page.route("**/api/v1/metrics/event", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    });
  });
});

test("expanded: archive, Undo, archived view, and Restore stay consistent", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");

  const row = page.locator(".unified-work-item", {
    hasText: ARCHIVED_SESSION.title,
  });
  await row.hover();
  await row.getByRole("button", { name: /^更多会话操作/ }).click();
  await page.getByRole("menuitem", { name: "归档会话" }).click();

  await expect(row).toHaveCount(0);
  await expect(page.getByText(`已归档“${ARCHIVED_SESSION.title}”`)).toBeVisible();
  const archivedEntry = page.getByRole("button", {
    name: "1 个已归档会话",
  });
  await expect(archivedEntry).toBeVisible();

  await page.getByRole("button", { name: "撤销" }).click();
  await expect(
    page.locator(".unified-work-item", {
      hasText: ARCHIVED_SESSION.title,
    }),
  ).toBeVisible();
  await expect(archivedEntry).toHaveCount(0);

  const restoredRow = page.locator(".unified-work-item", {
    hasText: ARCHIVED_SESSION.title,
  });
  await restoredRow.hover();
  await restoredRow.getByRole("button", { name: /^更多会话操作/ }).click();
  await page.getByRole("menuitem", { name: "归档会话" }).click();
  await page.getByRole("button", { name: "1 个已归档会话" }).click();

  // The archived view heading (h2). Once the archived list loads, its group is also titled
  // "已归档" (h3), so an unscoped heading query only passed while the list was still loading.
  await expect(
    page.getByRole("heading", { name: "已归档", level: 2 }),
  ).toBeVisible();
  const archivedRow = page.locator(".unified-work-item", {
    hasText: ARCHIVED_SESSION.title,
  });
  await expect(archivedRow).toBeVisible();
  await archivedRow.hover();
  // Archived rows offer only Restore in the row "⋯" menu (no Pin).
  await archivedRow.getByRole("button", { name: /^更多会话操作/ }).click();
  await expect(page.getByRole("menuitem", { name: /置顶/ })).toHaveCount(0);
  await page.getByRole("menuitem", { name: "恢复会话" }).click();

  await expect(archivedRow).toHaveCount(0);
  await expect(page.getByText("会话已恢复")).toBeVisible();
});

test("expanded: running work is visibly blocked", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");

  const runningRow = page.locator(".unified-work-item", {
    hasText: "今年 GMV 速查",
  });
  await runningRow.hover();
  await runningRow.getByRole("button", { name: /^更多会话操作/ }).click();
  // The disabled item keeps its name and shows the reason as a second line.
  const archive = page.getByRole("menuitem", { name: "归档会话" });
  await expect(archive).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByRole("menu").getByText("这项工作仍在运行，完成或停止后才能归档。")).toBeVisible();
});

test("archive feedback disappears after the eight-second Undo window", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  const row = page.locator(".unified-work-item", { hasText: ARCHIVED_SESSION.title });
  await row.hover();
  await row.getByRole("button", { name: /^更多会话操作/ }).click();
  await page.getByRole("menuitem", { name: "归档会话" }).click();
  const feedback = page.getByText(`已归档“${ARCHIVED_SESSION.title}”`);
  await expect(feedback).toBeVisible();
  await page.mouse.move(800, 500);
  await expect(feedback).toHaveCount(0, { timeout: 12_000 });
});

test("expanded: row semantics stay singular and actions remain centered", async ({
  page,
  browserName,
}) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/?session=s2");

  const row = page.locator(".unified-work-item", {
    hasText: ARCHIVED_SESSION.title,
  });
  await expect(row).toBeVisible();
  // A read session has no unread marker (unread sessions show a bold title instead of a dot).
  await expect(row.getByRole("img", { name: "未读" })).toHaveCount(0);
  await expect(row.locator(".unified-work-unread")).toHaveCount(0);

  // The row "⋯" menu trigger is the only row action.
  const actions = row.locator(".unified-work-more-wrap");
  const rowMain = row.locator(".unified-work-item-main");
  const title = row.locator(".unified-work-title");
  await expect(actions).toHaveCSS("opacity", "0");
  const restingTitleWidth = await title.evaluate(
    (element) => element.getBoundingClientRect().width,
  );
  await rowMain.focus();
  await expect(actions).toHaveCSS("opacity", "0");
  await expect
    .poll(() =>
      title.evaluate((element) => element.getBoundingClientRect().width),
    )
    .toBe(restingTitleWidth);
  await page.keyboard.press(browserName === "webkit" ? "Alt+Tab" : "Tab");
  await expect(actions.locator("button").first()).toBeFocused();
  await expect(actions).toHaveCSS("opacity", "1");
  const focusedActionTitleWidth = await title.evaluate(
    (element) => element.getBoundingClientRect().width,
  );
  // The title yields room so it never runs under the visible "⋯" trigger.
  expect(focusedActionTitleWidth).toBeLessThan(restingTitleWidth);
  const titleRight = await title.evaluate((element) => element.getBoundingClientRect().right);
  const triggerLeft = await actions
    .locator("button")
    .first()
    .evaluate((element) => element.getBoundingClientRect().left);
  // The reserve is the button width minus 2px by design (the glyph sits centred in the button).
  expect(titleRight).toBeLessThanOrEqual(triggerLeft + 2);
  await page.evaluate(() => {
    (document.activeElement as HTMLElement | null)?.blur();
  });
  await expect(actions).toHaveCSS("opacity", "0");
  await expect
    .poll(() =>
      title.evaluate((element) => element.getBoundingClientRect().width),
    )
    .toBe(restingTitleWidth);
  await row.hover();
  await expect(actions).toHaveCSS("opacity", "1");
  await expect
    .poll(() =>
      title.evaluate((element) => element.getBoundingClientRect().width),
    )
    .toBe(focusedActionTitleWidth);
  const rowBox = await row.boundingBox();
  const actionsBox = await actions.boundingBox();

  expect(rowBox).not.toBeNull();
  expect(actionsBox).not.toBeNull();
  expect(
    Math.abs(
      (rowBox?.y ?? 0) +
        (rowBox?.height ?? 0) / 2 -
        ((actionsBox?.y ?? 0) + (actionsBox?.height ?? 0) / 2),
    ),
  ).toBeLessThanOrEqual(1);
});

test.describe("touch presentation", () => {
  test.use({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
  });

  test("drawer menu Escape returns focus without closing the drawer", async ({
    page,
  }) => {
    await page.goto("/");
    await page.getByTestId("mobile-topbar-hamburger").click();

    const drawer = page.getByTestId("sessions-drawer");
    const row = drawer.locator(".unified-work-item", {
      hasText: ARCHIVED_SESSION.title,
    });
    const more = row.getByRole("button", { name: /^更多会话操作/ });
    await more.click();
    await expect(
      page.getByRole("menuitem", { name: "归档会话" }),
    ).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(drawer).toBeVisible();
    await expect(more).toBeFocused();
  });
});

async function installArchiveApi(page: Page): Promise<void> {
  let archived = false;
  await page.route("**/api/v1/sessions**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/sessions/s2/archive") {
      if (request.method() === "POST") archived = true;
      if (request.method() === "DELETE") archived = false;
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (url.pathname !== "/api/v1/sessions") {
      await route.continue();
      return;
    }
    const archivedView = url.searchParams.get("view") === "archived";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        groups: archivedView && archived
          ? [{ id: "archived", label: "ARCHIVED", items: [ARCHIVED_SESSION] }]
          : [],
        archived_count: archived ? 1 : 0,
      }),
    });
  });
}
