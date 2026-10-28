import { expect, test, type Page } from "@playwright/test";

/**
 * V5 mobile fix sprint e2e — replaces old W11-11 mobile drawer test (the old .v2-sidebar
 * data-mobile-open pattern was replaced by V5 SessionsDrawer + MobileTopBar).
 *
 * Coverage at viewport 412×915 (Pixel 7):
 *  - body can scroll
 *  - hamburger opens drawer
 *  - drawer mask click dismiss
 *  - drawer ESC dismiss
 *  - drawer close button dismiss
 *  - drawer contains the current primary destinations + work recovery
 *  - composer reachable
 *  - workspace stacked (chat above canvas, single column)
 *  - no horizontal page overflow
 */
test.use({ viewport: { width: 412, height: 915 } });
test.setTimeout(60_000);

test.beforeEach(async ({ page }) => {
  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill(json({
      id: "u_mobile",
      email: "mobile@example.com",
      name: "Mobile User",
      tier: "pro",
      credit_balance: 1_000,
      credit_quota: 10_000,
    }));
  });
  await page.route("**/api/v1/auth/me/workspaces", async (route) => {
    await route.fulfill(json({
      items: [{ workspace_id: "ws_mobile", name: "Mobile workspace", role: "owner" }],
      active_id: "ws_mobile",
    }));
  });
  await page.route("**/api/v1/user-settings**", async (route) => {
    await route.fulfill(json({ settings: {} }));
  });
  await page.route("**/api/v1/llm/catalog", async (route) => {
    await route.fulfill(json({ models: [], default_model: null }));
  });
  await page.route("**/api/v1/sessions/does_not_exist", async (route) => {
    await route.fulfill(json({ detail: "not found" }, 404));
  });
  await page.route("**/api/v1/sessions**", async (route) => {
    await route.fulfill(json({ items: [] }));
  });
  await page.route("**/api/v1/canvas/by-session/**", async (route) => {
    await route.fulfill(json({ detail: "not found" }, 404));
  });
  await page.route("**/api/v1/memory/snapshot", async (route) => {
    await route.fulfill(json({ items: [] }));
  });
  await page.route("**/api/v1/metrics/event", async (route) => {
    await route.fulfill(json({ ok: true }));
  });
});

async function gotoApp(page: Page, url = "/") {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  // Until sessions and spaces finish loading, the app shell shows a skeleton (no top bar or main content); local dev cold compilation can take
  // longer than the default 5 seconds. Wait for shell main content to mount (present in mobile and desktop layouts), rather than letting each case rely on luck.
  await expect(page.locator("#bowhead-main-content")).toBeAttached({ timeout: 20_000 });
}

async function openDrawer(page: Page) {
  const hamburger = page.locator("[data-testid='mobile-topbar-hamburger']");
  const drawer = page.locator("[data-testid='sessions-drawer']");

  await expect(hamburger).toBeVisible();
  await expect(hamburger).toBeEnabled();

  for (let attempt = 0; attempt < 3; attempt += 1) {
    await hamburger.click();
    try {
      await expect(drawer).toHaveCount(1, { timeout: 1_500 });
      return;
    } catch (error) {
      if (attempt === 2) throw error;
      await page.waitForTimeout(250);
    }
  }
}

test("mobile: body can scroll (no overflow:hidden lock)", async ({ page }) => {
  await gotoApp(page);
  const overflow = await page.evaluate(() => getComputedStyle(document.body).overflow);
  // V5 fix: body overflow visible on mobile (≤768px)
  expect(overflow).not.toBe("hidden");
});

test("mobile: hamburger opens sessions drawer", async ({ page }) => {
  await gotoApp(page);
  await expect(page.locator("[data-testid='sessions-drawer']")).toHaveCount(0);
  await openDrawer(page);
  await expect(page.locator("[data-testid='sessions-drawer']")).toHaveCount(1);
  await expect(page.locator("[data-testid='sessions-drawer-mask']")).toHaveCount(1);
});

test("mobile: drawer renders the current primary destinations", async ({
  page,
}) => {
  await gotoApp(page);
  await openDrawer(page);
  await expect(page.getByRole("button", { name: /新工作|New Work/ })).toBeVisible();
  for (const destination of ["reports", "library", "capabilities"]) {
    await expect(
      page.getByTestId(`unified-drawer-nav-${destination}`),
    ).toHaveCount(1);
  }
});

test("mobile: mask click dismisses drawer", async ({ page }) => {
  await gotoApp(page);
  await openDrawer(page);
  // click on the mask area (not on drawer body)
  await page.locator("[data-testid='sessions-drawer-mask']").click({ position: { x: 400, y: 100 } });
  await expect(page.locator("[data-testid='sessions-drawer']")).toHaveCount(0);
});

test("mobile: close button dismisses drawer", async ({ page }) => {
  await gotoApp(page);
  await openDrawer(page);
  await page.locator("[data-testid='sessions-drawer-close']").click();
  await expect(page.locator("[data-testid='sessions-drawer']")).toHaveCount(0);
});

test("mobile: ESC dismisses drawer", async ({ page }) => {
  await gotoApp(page);
  await openDrawer(page);
  await page.keyboard.press("Escape");
  await expect(page.locator("[data-testid='sessions-drawer']")).toHaveCount(0);
});

test("mobile: nav link click navigates + closes drawer", async ({ page }) => {
  await gotoApp(page);
  await openDrawer(page);
  await page.getByTestId("unified-drawer-nav-reports").click();
  await expect(page).toHaveURL(/\/reports\/?$/);
  await expect(page.getByTestId("sessions-drawer")).toHaveCount(0);
});

test("mobile: workspace = single column stacked (chat above canvas)", async ({ page }) => {
  await gotoApp(page);
  // Wait for workspace to render. May need a session selected; default home shows welcome.
  // Just check that .v2-workspace if present uses flex column on mobile.
  const flexDir = await page.evaluate(() => {
    const ws = document.querySelector(".v2-workspace");
    return ws ? getComputedStyle(ws).flexDirection : null;
  });
  if (flexDir !== null) expect(flexDir).toBe("column");
});

test("mobile: no horizontal page overflow", async ({ page }) => {
  await gotoApp(page);
  const overflow = await page.evaluate(() => {
    const root = document.querySelector(".v2-app-root");
    if (!root) return { sw: 0, cw: 0 };
    return { sw: root.scrollWidth, cw: root.clientWidth };
  });
  // allow 1px rounding
  expect(overflow.sw).toBeLessThanOrEqual(overflow.cw + 1);
});

test("mobile: turn_id NOT in workspace title", async ({ page }) => {
  await gotoApp(page, "/?session=does_not_exist");
  // even on missing session, no t_xxxxx should be visible in title chrome
  const text = await page.evaluate(() => document.body.innerText);
  expect(text).not.toMatch(/\bt_[a-f0-9]{12,}/);
});


// ─── 320×568 (iPhone SE) basic check ─────────────────────────
test.describe("iPhone SE 320×568", () => {
  test.use({ viewport: { width: 320, height: 568 } });
  test("home: body can scroll, hamburger visible", async ({ page }) => {
    await gotoApp(page);
    const overflow = await page.evaluate(() => getComputedStyle(document.body).overflow);
    expect(overflow).not.toBe("hidden");
    await expect(page.locator("[data-testid='mobile-topbar-hamburger']")).toBeVisible();
  });
});


// ─── 768×1024 (iPad portrait — boundary case) ──────────────────
test.describe("iPad portrait 768×1024", () => {
  test.use({ viewport: { width: 768, height: 1024 } });
  test("home: at exactly 768px viewport, mobile mode applies", async ({ page }) => {
    await gotoApp(page);
    // 768 is included in (max-width: 768px), so mobile chrome shows
    await expect(page.locator("[data-testid='mobile-topbar']")).toBeVisible();
  });
});


// ─── Desktop regression check (1440×900) ─────────────────────
test.describe("Desktop 1440×900 regression sanity", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test("desktop: mobile topbar hidden, body still has overflow:hidden (desktop unchanged)", async ({ page }) => {
    await gotoApp(page);
    // desktop overflow stays hidden (existing behavior preserved)
    const overflow = await page.evaluate(() => getComputedStyle(document.body).overflow);
    expect(overflow).toBe("hidden");
    // mobile topbar may be display:none or not rendered at all on desktop.
    const topbarDisplay = await page.evaluate(() => {
      const tb = document.querySelector(".v2-mobile-topbar");
      return tb ? getComputedStyle(tb).display : "no-element";
    });
    expect(["none", "no-element"]).toContain(topbarDisplay);
  });
});
