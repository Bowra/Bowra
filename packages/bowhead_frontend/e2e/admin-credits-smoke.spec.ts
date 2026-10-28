import { expect, test } from "@playwright/test";
import { probeLayer } from "./_layer-probe";

/**
 * V3 admin credits e2e — basic visibility + permission gating + activation action smoke.
 *
 * Anonymous path:
 *   - explicitly mock /auth/me + /auth/refresh = 401; directly visiting an admin route must redirect to /login?next=...
 *   - do not render sidebar credits NAV item (isTeamAdmin=false)
 *
 * activation happy path:
 *   - mock team_admin credit capability + summary attention item
 *   - recommended Grant credits action opens GrantCreditModal
 *   - modal success only refreshes; grant API backend coordination supplies acted
 */

const creditCaps = {
  credits_read: true,
  credit_grant: true,
  credit_user_quota_write: true,
  credit_user_tier_write: true,
  credit_period_reset: true,
  credit_team_quota_write: true,
  credit_attention_dismiss: true,
};

const creditAdmin = {
  id: "u_admin",
  email: "billing@example.com",
  display_name: "Team Admin",
  avatar_url: null,
  team_id: "team_1",
  tier: "ultra",
  role: "user",
  team_role: "team_admin",
  status: "active",
  email_verified: true,
  capabilities: { credit: creditCaps },
  credit_capabilities: creditCaps,
  credit_balance: 10_000,
  credit_quota: 10_000,
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  is_team_admin: true,
  workspace_count: 1,
  needs_onboarding: false,
};

const memberUser = {
  ...creditAdmin,
  id: "u_member",
  email: "member@example.com",
  display_name: "Member User",
  team_role: "member",
  capabilities: {
    credit: {
      credits_read: false,
      credit_grant: false,
      credit_user_quota_write: false,
      credit_user_tier_write: false,
      credit_period_reset: false,
      credit_team_quota_write: false,
      credit_attention_dismiss: false,
    },
  },
  credit_capabilities: {
    credits_read: false,
    credit_grant: false,
    credit_user_quota_write: false,
    credit_user_tier_write: false,
    credit_period_reset: false,
    credit_team_quota_write: false,
    credit_attention_dismiss: false,
  },
  is_team_admin: false,
};

const attentionItem = {
  attention_item_id: "cai_1",
  team_id: "team_1",
  user_id: "u_power",
  workspace_id: null,
  policy_key: "low_balance_power_user",
  state: "open",
  severity: "warning",
  severity_rank: 2,
  primary_segment: "low_balance_power_user",
  segment_flags: ["low_balance_power_user"],
  attention_reason: "Low balance for active user",
  evidence: {
    email: "power@example.com",
    balance_pct: 0.08,
    spend_7d: 240,
  },
  recommended_action_key: "grant_credit",
  action_intent: {
    key: "grant_credit",
    label: "Grant credits",
    reason: "Low balance for active user",
  },
  recommended_action: {
    key: "grant_credit",
    label: "Grant credits",
    allowed: true,
    disabled_reason: null,
    capability_required: "credits:grant",
  },
  available_actions: [],
  email: "power@example.com",
  display_name: "Power User",
  tier: "pro",
  opened_at: "2026-06-07T00:00:00Z",
  expires_at: "2026-07-07T00:00:00Z",
  updated_at: "2026-06-07T00:00:00Z",
  resolved_at: null,
};

const tableAttentionItem = {
  ...attentionItem,
  attention_item_id: "cai_table",
  user_id: "u_table",
  email: "table-power@example.com",
  display_name: "Table Power",
};

const tableAttentionUser = {
  user_id: "u_table",
  email: "table-power@example.com",
  display_name: "Table Power",
  tier: "pro",
  status: "active",
  credit_balance: 80,
  credit_quota: 1_000,
  credit_period_start: null,
  spend_30d: 900,
  last_activity_at: null,
  is_team_admin: false,
  attention_score: 0.66,
  activation_attention: tableAttentionItem,
  primary_segment: "low_balance_power_user",
  segment_flags: ["low_balance_power_user"],
  recommended_action_key: "grant_credit",
  recommended_action: tableAttentionItem.recommended_action,
};

async function mockUnauthed(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "not authenticated" }),
    });
  });
  await page.route("**/api/v1/auth/me/workspaces", async (route) => {
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "not authenticated" }),
    });
  });
  await page.route("**/api/v1/auth/refresh", async (route) => {
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "refresh token missing" }),
    });
  });
  await page.route("**/api/v1/auth/logout", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    });
  });
  await page.route("**/api/v1/bootstrap/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ required: false, deployment_mode: "saas" }),
    });
  });
}

async function mockAuthedCredits(
  page: import("@playwright/test").Page,
  options: { users?: unknown[]; onActionClicked?: (body: unknown) => void } = {},
) {
  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(creditAdmin),
    });
  });
  await page.route("**/api/v1/auth/me/workspaces", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [{ workspace_id: "ws_1", name: "Demo workspace", role: "ws_admin" }],
        active_id: "ws_1",
      }),
    });
  });
  await page.route("**/api/v1/sessions**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    });
  });
  await page.route("**/api/v1/admin/credits/summary", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        team: { balance: 50_000, quota: 100_000, period_start: null, last_reset_at: null },
        last_30d: { spend: 1_200, turns: 12, tokens_in: 10_000, tokens_out: 5_000 },
        active_users_7d: 3,
        total_users_active: 5,
        view_attention: [],
        view_top_users: [],
        view_top_workspaces: [],
        activation_attention: [attentionItem],
        credit_actor: { team_role: "team_admin", capabilities: creditCaps },
      }),
    });
  });
  await page.route(
    (url) => url.pathname === "/api/v1/admin/credits/users",
    async (route) => {
      await route.fulfill({
	        status: 200,
	        contentType: "application/json",
	        body: JSON.stringify({
	          items: options.users ?? [],
	          total: options.users?.length ?? 0,
	          page: 1,
	          size: 20,
	        }),
	      });
	    },
	  );
  await page.route("**/api/v1/admin/credits/users/u_power/info", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: "u_power",
        email: "power@example.com",
        display_name: "Power User",
        tier: "pro",
        status: "active",
        credit_balance: 80,
        credit_quota: 1_000,
        credit_period_start: null,
        spend_30d: 900,
        last_activity_at: null,
        is_team_admin: false,
        attention_score: 10,
      }),
    });
  });
  await page.route("**/api/v1/admin/credits/attention/*/action-clicked", async (route) => {
    options.onActionClicked?.(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    });
  });
}

async function mockMemberHome(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(memberUser),
    });
  });
  await page.route("**/api/v1/auth/me/workspaces", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [], active_id: null }),
    });
  });
  await page.route("**/api/v1/sessions**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    });
  });
  await page.route("**/api/v1/user-settings", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ settings: {} }),
    });
  });
  await page.route("**/api/v1/llm/catalog", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: [], default_model: null }),
    });
  });
}

test.describe("admin credits page (unauth)", () => {
  test("direct access /admin/credits without auth shows fallback", async ({ page }) => {
    await mockUnauthed(page);
    await page.goto("/admin/credits/");
    await page.waitForURL(/\/login\/?\?next=/);
    expect(new URL(page.url()).searchParams.get("next")).toBe("/admin/credits");
    await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  });

  test("sidebar credits entry hidden for non-admin user", async ({ page }) => {
    await mockMemberHome(page);
    await page.goto("/");
    const sidebar = page.getByTestId("unified-sidebar");
    await expect(sidebar).toBeVisible();
    await sidebar
      .getByRole("button", { name: /账户|Account/ })
      .press("Enter");
    await expect(
      page.getByRole("menuitem", { name: /管理|Administration/ }),
    ).toHaveCount(0);
  });
});

test.describe("admin credits drill-down (unauth)", () => {
  test("direct access /admin/credits/users/X without auth shows fallback", async ({ page }) => {
    await mockUnauthed(page);
    await page.goto("/admin/credits/users/?user_id=u_anyone");
    await page.waitForURL(/\/login\/?\?next=/);
    expect(new URL(page.url()).searchParams.get("next")).toBe(
      "/admin/credits/users?user_id=u_anyone",
    );
    await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  });
});

test.describe("admin credits activation actions", () => {
  test("recommended grant action opens modal and refreshes after success", async ({ page }) => {
    let actionClickedBody: Record<string, unknown> | null = null;
    await mockAuthedCredits(page, {
      onActionClicked: (body) => { actionClickedBody = body as Record<string, unknown>; },
    });

    let grantCalled = false;
    let outcomeStateCalled = false;
    await page.route("**/api/v1/admin/credits/users/u_power/grant", async (route) => {
      grantCalled = true;
      expect(route.request().method()).toBe("POST");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, new_balance: 1_080 }),
      });
    });
    await page.route("**/api/v1/admin/credits/attention/cai_1/state", async (route) => {
      outcomeStateCalled = true;
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "manual outcome state should not be called" }),
      });
    });

    await page.goto("/admin/credits/");
    await expect(page.getByRole("heading", { name: /额度|Credits/ })).toBeVisible();
    await expect(page.getByText("power@example.com")).toBeVisible();

    await page.getByRole("button", { name: /增加额度|Grant credits/ }).click();
    await expect.poll(() => actionClickedBody?.action_key).toBe("grant_credit");
    expect(actionClickedBody).toMatchObject({
      source_surface: "summary",
      allowed: true,
      disabled_reason: null,
    });
    await expect(page.getByRole("dialog", { name: /发放额度|Grant credit/ })).toBeVisible();
    await expect(page.getByText(/当前余额: 80|current balance: 80/i)).toBeVisible();

    await page.getByRole("button", { name: /^发放$|^Grant$/ }).click();
    await expect.poll(() => grantCalled).toBe(true);
    await page.waitForTimeout(100);
    expect(outcomeStateCalled).toBe(false);
    await expect(page.getByRole("dialog", { name: /发放额度|Grant credit/ })).toHaveCount(0);
  });

  test("dismiss action requires a reason preset", async ({ page }) => {
    await mockAuthedCredits(page);

    let dismissedCalled = false;
    await page.route("**/api/v1/admin/credits/attention/cai_1/state", async (route) => {
      dismissedCalled = true;
      expect(route.request().method()).toBe("POST");
      expect(route.request().postDataJSON()).toMatchObject({
        state: "dismissed",
        reason: "handled_offline",
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, item: { ...attentionItem, state: "dismissed" } }),
      });
    });

    await page.goto("/admin/credits/");
    await expect(page.getByText("power@example.com")).toBeVisible();

    await page.getByRole("button", { name: /Dismiss|忽略/ }).click();
    const dialog = page.getByRole("dialog", { name: /Dismiss attention item|忽略关注项/ });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel(/Reason|原因/).selectOption("handled_offline");
    await dialog.getByRole("button", { name: /Dismiss|忽略/ }).click();

    await expect.poll(() => dismissedCalled).toBe(true);
    await expect(dialog).toHaveCount(0);
  });

  test("users table renders activation attention cell", async ({ page }) => {
    await mockAuthedCredits(page, { users: [tableAttentionUser] });

    await page.goto("/admin/credits/");
    await expect(page.getByRole("heading", { name: /额度|Credits/ })).toBeVisible();

    const row = page.locator("tr", { hasText: "table-power@example.com" });
    await expect(row).toBeVisible();
    await expect(row.getByText("预警")).toBeVisible();
    await expect(row.getByText("活跃用户余额偏低").first()).toBeVisible();
    await expect(row.getByRole("button", { name: /增加额度|Grant credits/ })).toBeVisible();
  });

  test("row ⋯ menu on the last row is fully visible (not clipped by the table)", async ({ page }) => {
    // Layer audit (design document “图层”): the original inline overlay lived in an overflow-x:auto table container (making overflow-y auto too),
    // clipping the last row's menu so it could only be seen by scrolling within the table.
    const plainUser = tableAttentionUser;
    await mockAuthedCredits(page, {
      users: [plainUser, { ...plainUser, user_id: "u_last", email: "last@example.com", display_name: "Last" }],
    });
    await page.goto("/admin/credits/");
    const row = page.locator("tr", { hasText: "last@example.com" });
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "更多", exact: true }).click();
    const menu = page.getByRole("menu");
    await expect(menu).toBeVisible();
    const probe = await probeLayer(menu);
    expect(probe.covered).toEqual([]);
    expect(probe.clipped).toBe(0);
  });
});
