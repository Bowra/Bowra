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
