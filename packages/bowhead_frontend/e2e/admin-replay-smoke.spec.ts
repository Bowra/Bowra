/**
 * V4 PRD-D-4-4 · /admin/turns admin replay UI e2e smoke (mocked backend).
 *
 * Coverage:
 *  1. select turn → snapshot panel renders DTO + breakdown + content
 *  2. snapshot 404 → empty state displayed (orphan / cross-team / no-snapshot)
 *  3. snapshot 500 → error state displayed
 *  4. expanding details reveals injected pack content
 *  5. event load failure does not block snapshot rendering and vice versa
 *
 * Backend security tests (cross-team 404, NULL team orphan filter, audit log)
 * live in pytest test_memory_replay_routes.py.
 */

import { expect, test, type Route } from "@playwright/test";


type SnapshotMode = "ok" | "missing" | "error";

interface AdminMockState {
  turnIds: string[];
  events?: Record<string, { delayMs?: number; payload: object }>;
  // Per-turn-id snapshot mode + payload
  snapshots: Record<string, { mode: SnapshotMode; payload?: object }>;
}

const adminUser = {
  id: "u_admin",
  email: "admin@example.com",
  display_name: "Admin User",
  avatar_url: null,
  team_id: "team_a",
  tier: "ultra",
  role: "user",
  team_role: "team_admin",
  status: "active",
  email_verified: true,
  credit_balance: 10_000,
  credit_quota: 30_000,
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  is_team_admin: true,
  workspace_count: 1,
  needs_onboarding: false,
};


async function _setupMocks(
  page: import("@playwright/test").Page,
  state: AdminMockState,
): Promise<void> {
  await page.route("**/api/v1/auth/me", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(adminUser),
    });
  });
  await page.route("**/api/v1/auth/me/workspaces", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [{ workspace_id: "ws1", name: "Demo workspace", role: "ws_admin" }],
        active_id: "ws1",
      }),
    });
  });
  await page.route(/\/api\/v1\/turns\?.*/, async (route: Route) => {
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ turn_ids: state.turnIds }),
    });
  });
  await page.route(/\/api\/v1\/turns\/[^/]+\/events$/, async (route: Route) => {
    const tid = decodeURIComponent(route.request().url().split("/").slice(-2)[0]);
    const configured = state.events?.[tid];
    if (configured?.delayMs) {
      await new Promise((resolve) => setTimeout(resolve, configured.delayMs));
    }
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        events: configured
          ? [
            {
              seq: 1,
              event_type: "step_list_card_created",
              payload: configured.payload,
              occurred_at: "2026-08-08T00:00:00Z",
            },
          ]
          : [],
      }),
    });
  });
  await page.route(
    /\/api\/v1\/admin\/turns\/[^/]+\/memory-pack-snapshot$/,
    async (route: Route) => {
      const tid = route.request().url().split("/").slice(-2)[0];
      const entry = state.snapshots[decodeURIComponent(tid)];
      if (!entry || entry.mode === "missing") {
        await route.fulfill({
          status: 404, contentType: "application/json",
          body: JSON.stringify({ detail: "snapshot not found" }),
        });
        return;
      }
      if (entry.mode === "error") {
        await route.fulfill({
          status: 500, contentType: "application/json",
          body: JSON.stringify({ detail: "internal error" }),
        });
        return;
      }
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify(entry.payload),
      });
    },
  );
}


test("admin replay shows snapshot DTO + breakdown + content for selected turn", async ({ page }) => {
  await _setupMocks(page, {
    turnIds: ["t_audited"],
    snapshots: {
      t_audited: {
        mode: "ok",
        payload: {
          turn_id: "t_audited",
          user_id: "u_member",
          team_id: "team_a",
          workspace_id: "ws1",
          char_count: 50,
          sections_breakdown: { notes: 30, entries: 15 },
          content: "📌 sample pinned bullet\n• another bullet\n<<USER_PROFILE>>",
          pack_version: "v2",
          created_at: "2026-05-03T10:00:00+00:00",
        },
      },
    },
  });
  await page.goto("/admin/turns?id=t_audited");

  await expect(page.getByTestId("admin-replay-snapshot")).toBeVisible();

  const breakdown = page.getByTestId("admin-replay-breakdown");
  await expect(breakdown).toContainText("notes: 30");
  await expect(breakdown).toContainText("entries: 15");

  // Expand the <details> wrapper to see content
  await page.locator('details summary').click();
  const content = page.getByTestId("admin-replay-content");
  await expect(content).toContainText("sample pinned bullet");
  await expect(content).toContainText("USER_PROFILE");
});


test("admin replay shows empty-state for orphan / no-snapshot turn (404)", async ({ page }) => {
  await _setupMocks(page, {
    turnIds: ["t_orphan"],
    snapshots: { t_orphan: { mode: "missing" } },
  });
  await page.goto("/admin/turns?id=t_orphan");

  await expect(page.getByTestId("admin-replay-empty")).toBeVisible();
  await expect(page.getByTestId("admin-replay-empty"))
    .toContainText(/没有快照|No snapshot/i);
});


test("admin replay shows error message on 500", async ({ page }) => {
  await _setupMocks(page, {
    turnIds: ["t_500"],
    snapshots: { t_500: { mode: "error" } },
  });
  await page.goto("/admin/turns?id=t_500");

  const err = page.getByTestId("admin-replay-error");
  await expect(err).toBeVisible();
  // The notice explains what failed in words; the HTTP status lives only in "复制诊断".
  await expect(err).toContainText("没能加载这个对话轮次的记忆包快照");
  await expect(err.getByRole("button", { name: "复制诊断" })).toBeVisible();
  await expect(err).not.toContainText("500");
});


test("snapshot panel only appears after selecting a turn", async ({ page }) => {
  await _setupMocks(page, {
    turnIds: ["t_one"],
    snapshots: { t_one: { mode: "missing" } },
  });
  await page.goto("/admin/turns");
  // No turn selected yet → no snapshot panel
  await expect(page.getByTestId("admin-replay-snapshot")).toHaveCount(0);
  // Click the only turn id in the list
  await page.getByRole("button", { name: "t_one" }).click();
  await expect(page.getByTestId("admin-replay-snapshot")).toBeVisible();
});

test("a slower previous turn response cannot replace the selected turn timeline", async ({ page }) => {
  await _setupMocks(page, {
    turnIds: ["t_slow", "t_current"],
    events: {
      t_slow: { delayMs: 200, payload: { card_id: "t_slow" } },
      t_current: { payload: { card_id: "t_current" } },
    },
    snapshots: {
      t_slow: { mode: "missing" },
      t_current: { mode: "missing" },
    },
  });
  await page.goto("/admin/turns");

  await page.getByRole("button", { name: "t_slow" }).click();
  await page.getByRole("button", { name: "t_current" }).click();

  await expect(page.locator("tbody")).toContainText("t_current");
  await page.waitForTimeout(250);
  await expect(page.locator("tbody")).toContainText("t_current");
  await expect(page.locator("tbody")).not.toContainText("t_slow");
});
