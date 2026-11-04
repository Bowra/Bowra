import { expect, test, type Page } from "@playwright/test";

import zh from "../messages/zh-CN.json";

/**
 * Desktop top bar hides below 1024; relocated space switcher, notifications and credits must remain accessible:
 * space switcher/notifications on mobile top-bar right; credits in the top bar at 768–1023 and at the session-drawer
 * bottom below 768; ≥1024 retains desktop placement. Test at 900 (tablet) and 390 (phone).
 * Placement table: components/shell/global-controls.tsx.
 */

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function mockShell(page: Page) {
  const lastSpace: string[] = [];
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill(
      json({
        id: "u_shell",
        email: "shell@example.com",
        name: "Shell User",
        tier: "pro",
        credit_balance: 1_200,
        credit_quota: 10_000,
        capabilities: {},
      }),
    ),
  );
  await page.route("**/api/v1/auth/me/workspaces*", (route) =>
    route.fulfill(
      json({
        items: [
          { workspace_id: "ws_east", name: "华东区经营分析空间", role: "member" },
          { workspace_id: "ws_south", name: "华南区", role: "member" },
        ],
        active_id: "ws_east",
      }),
    ),
  );
  await page.route("**/api/v1/auth/me/last-space", (route) => {
    lastSpace.push(String(route.request().postDataJSON()?.workspace_id));
    return route.fulfill({ status: 204, body: "" });
  });
  await page.route("**/api/v1/notifications/unread-count", (route) =>
    route.fulfill(json({ actionRequired: 1, updatesUnread: 0, totalBadge: 1 })),
  );
  await page.route("**/api/v1/notifications?*", (route) =>
    route.fulfill(
      json({
        items: [
          {
            id: "n_1",
            type: "approval.required",
            state: "open",
            priority: "p1",
            // Matches backend persisted approval.required copy; frontend rewrites the title by type in the current language.
            title: "有操作需要审批",
            body: "",
            category: "",
            actionUrl: null,
            readAt: null,
            createdAt: new Date().toISOString(),
          },
        ],
        nextCursor: null,
      }),
    ),
  );
  await page.route("**/api/v1/metrics/event", (route) => route.fulfill(json({ ok: true })));
  return lastSpace;
}

// P2 batch 4: the mobile top bar is below 1024; 768–1023 carries the credits in the top bar.
for (const width of [900, 390]) {
  test(`space, notifications and credits are reachable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    const lastSpace = await mockShell(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });

    const topbar = page.getByTestId("mobile-topbar");
    const picker = topbar.getByRole("button", { name: /切换空间，当前：华东区经营分析空间/ });
    await expect(picker).toBeVisible({ timeout: 30_000 });
    // Mount only one copy per control (no duplicate when desktop top bar hides); page has no horizontal overflow.
    await expect(page.getByRole("button", { name: /切换空间/ })).toHaveCount(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

    // Space switching: open, select another space.
    await picker.click();
    await page.getByRole("menuitemradio", { name: /华南区/ }).click();
    await expect(topbar.getByRole("button", { name: /切换空间，当前：华南区/ })).toBeVisible();
    expect(lastSpace).toContain("ws_south");

    // Notifications: 44px touch target; clicking opens the list.
    const bell = topbar.getByRole("button", { name: /通知/ });
    const box = await bell.boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(44);
    expect(box?.height).toBeGreaterThanOrEqual(44);
    await bell.click();
    const panel = page.locator('[data-notification-center="true"]');
    await expect(panel.getByText(zh.notifications.copy.approval_required_title)).toBeVisible();
    await bell.click();
    await expect(panel).toBeHidden();

    // The credits badge is a button that opens the credits popover (bottom sheet below 640).
    // 768–1023: gauge icon in the top bar; below 768: at the drawer bottom.
    if (width < 768) await page.getByTestId("mobile-topbar-hamburger").click();
    const credits = (width < 768 ? page.getByTestId("sessions-drawer") : topbar).getByRole("button", { name: /^额度：/ });
    await expect(page.getByTestId("user-credit-badge")).toHaveCount(1);
    await expect(credits).toContainText("1,200");
    await credits.click();
    const summary = page.getByRole("dialog", { name: "Pro 套餐" });
    await expect(summary.getByRole("progressbar", { name: "本期已用额度" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(summary).toBeHidden();
    await expect(credits).toBeFocused();
  });
}

test("desktop keeps all three controls in the topbar at 1440px", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockShell(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const head = page.locator(".v2-main-head");
  await expect(head.getByRole("button", { name: /切换空间，当前：华东区经营分析空间/ })).toBeVisible({ timeout: 30_000 });
  await expect(head.getByRole("button", { name: /通知/ })).toBeVisible();
  await expect(head.getByRole("button", { name: /^额度：/ })).toContainText("1,200");
  await expect(page.getByRole("button", { name: /切换空间/ })).toHaveCount(1);
});

for (const locale of ["zh-CN", "en-US"] as const) {
  test.describe(`space self-exit ${locale}`, () => {
    test.use({ locale });
    for (const [width, another] of [[1280, true], [390, false]] as const) {
      test(`space settings submenu exits safely at ${width}px`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 844 });
        await mockShell(page);
        let left = false;
        const admin = width === 1280;
        const calls: string[] = [];
        await page.route("**/api/v1/auth/me/workspaces*", route => route.fulfill(json({
          items: [
            ...(!left ? [{ workspace_id: "ws_east", name: "Sales", role: admin ? "admin" : "viewer" }] : []),
            ...(another ? [{ workspace_id: "ws_south", name: "Finance", role: "member" }] : []),
          ], active_id: left ? (another ? "ws_south" : null) : "ws_east",
        })));
        await page.route("**/api/v1/workspaces/ws_east/leave", route => {
          if (route.request().method() === "POST") { left = true; calls.push("leave"); }
          return route.fulfill(json(route.request().method() === "POST" ? { ok: true } : { can_leave: true }));
        });
        await page.goto("/", { waitUntil: "domcontentloaded" });
        const picker = page.locator(".v2-ws-picker-button");
        await expect(picker).toContainText("Sales", { timeout: 30_000 });
        await picker.click();
        const more = page.getByRole("menuitem", { name: locale === "zh-CN" ? "更多空间设置" : "More Space Settings", exact: true });
        const leaveLabel = locale === "zh-CN" ? "退出当前空间…" : "Leave Current Space…";
        await expect(page.getByRole("menuitem", { name: leaveLabel, exact: true })).toHaveCount(0);
        await more.click();
        const leave = page.getByRole("menuitem", { name: leaveLabel, exact: true });
        await expect(leave).toBeEnabled();
        if (admin) {
          for (const path of ["general", "instructions", "members"]) {
            await expect(page.locator(`[role="menuitem"][href="/space/settings/${path}/"]`)).toBeVisible();
          }
        } else {
          await expect(page.locator('[role="menuitem"][href^="/space/settings/"]')).toHaveCount(0);
          // Shared submenu drills down on phones and offers a way back to the space list.
          const back = page.locator(".ui-menu-drill-back");
          await expect(back).toBeVisible();
          await back.click();
          await expect(more).toBeVisible();
          await more.click();
        }
        await page.screenshot({ path: testInfo.outputPath("space-settings.png") });
        await leave.click();
        const dialog = page.getByRole("dialog", { name: locale === "zh-CN" ? "退出「Sales」？" : "Leave “Sales”?" });
        await expect(dialog).toBeVisible();
        expect(calls).toEqual([]);
        await dialog.getByRole("button", { name: locale === "zh-CN" ? "取消" : "Cancel", exact: true }).click();
        expect(left).toBe(false);
        await picker.click();
        await more.click();
        await leave.click();
        await dialog.getByRole("button", { name: locale === "zh-CN" ? "退出当前空间" : "Leave Current Space", exact: true }).click();
        await expect(dialog).toBeHidden();
        expect(calls).toEqual(["leave"]);
        await expect(page.getByText(locale === "zh-CN" ? "已退出「Sales」" : "Left “Sales”", { exact: true })).toBeVisible();
        await expect(picker).not.toContainText("Sales");
        if (another) await expect(picker).toContainText("Finance");
        else {
          await expect(page.getByRole("heading", { name: locale === "zh-CN" ? "创建或加入一个空间" : "Create or join a space", exact: true })).toBeVisible();
          expect(await page.evaluate(() => localStorage.getItem("bowhead.space_id"))).toBeNull();
        }
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      });
    }
  });
}

test("last space administrator reads the disabled reason without triggering exit", async ({ page }) => {
  await mockShell(page);
  await page.route("**/api/v1/auth/me/workspaces*", route => route.fulfill(json({
    items: [{ workspace_id: "ws_east", name: "华东区经营分析空间", role: "admin" }], active_id: "ws_east",
  })));
  await page.route("**/api/v1/workspaces/ws_east/leave", route => {
    expect(route.request().method()).toBe("GET");
    return route.fulfill(json({ can_leave: false }));
  });
  await page.goto("/");
  await page.getByRole("button", { name: /切换空间，当前：华东区/ }).click();
  const more = page.getByRole("menuitem", { name: "更多空间设置", exact: true });
  await more.focus();
  await page.keyboard.press("ArrowRight");
  const leave = page.getByRole("menuitem", { name: "退出当前空间…", exact: true });
  await expect(leave).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByText("你是该空间的最后一位有效管理员，请先指定另一位管理员，再退出。")).toBeVisible();
  await leave.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: /退出「/ })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(more).toBeVisible();
});
