import { expect, test } from "@playwright/test";

test.describe("admin access page", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/v1/auth/me", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "u_admin",
          email: "admin@example.com",
          display_name: "Admin",
          avatar_url: null,
          team_id: "team_1",
          tier: "pro",
          role: "team_admin",
          status: "active",
          email_verified: true,
          credit_balance: 100,
          credit_quota: 100,
          locale: "zh-CN",
          timezone: "Asia/Shanghai",
          is_team_admin: true,
          workspace_count: 1,
          needs_onboarding: false,
        }),
      });
    });
    await page.route("**/api/v1/groups", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            id: "grp_new",
            team_id: "team_1",
            workspace_id: "ws_sales",
            name: "Sales",
            description: "",
            status: "active",
            member_count: 0,
            created_by: "u_admin",
            created_at: "2026-06-05T00:00:00Z",
            updated_at: "2026-06-05T00:00:00Z",
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [{
            id: "grp_sales",
            team_id: "team_1",
            workspace_id: "ws_sales",
            name: "Sales",
            description: "",
            status: "active",
            member_count: 1,
            created_by: "u_admin",
            created_at: "2026-06-05T00:00:00Z",
            updated_at: "2026-06-05T00:00:00Z",
          }],
        }),
      });
    });
    await page.route("**/api/v1/groups/grp_sales/members", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [{
            group_id: "grp_sales",
            user_id: "u_sales",
            role: "member",
            email: "sales@example.com",
            display_name: "Sales",
            created_at: "2026-06-05T00:00:00Z",
          }],
        }),
      });
    });
    await page.route("**/api/v1/resource-grants", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    });
    await page.route("**/api/v1/auth/me/workspaces", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    });
    await page.route("**/api/v1/workspaces", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    });
    await page.route("**/api/v1/permissions/explain", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          allowed: true,
          reason: "allowed_by_grant",
          shadow: false,
          grant_id: "grant_1",
          access_context: {
            user_id: "u_admin",
            team_id: "team_1",
            workspace_id: "ws_sales",
            workspace_role: "ws_admin",
            group_ids: ["grp_sales"],
            policy_epoch: 2,
            is_team_admin: true,
          },
        }),
      });
    });
  });

  test("renders groups and explains a permission", async ({ page }) => {
    await page.goto("/admin/access/");
    await expect(
      page.getByRole("heading", { name: /访问控制|Access/, level: 1 }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /Sales ws_sales/ })).toBeVisible();
    await page.getByPlaceholder("space_id").fill("ws_sales");
    await page.getByPlaceholder("resource_id").last().fill("workspace:ws_sales");
    await page.getByRole("button", { name: /解释|Explain/ }).click();
    await expect(page.locator("code", { hasText: "allowed_by_grant" })).toBeVisible();
    await expect(page.getByText(/版本 2|epoch 2/i)).toBeVisible();
  });

  test("submits create group form", async ({ page }) => {
    let posted = false;
    await page.route("**/api/v1/groups", async (route) => {
      if (route.request().method() === "POST") {
        posted = true;
        const body = JSON.parse(route.request().postData() || "{}");
        expect(body.name).toBe("Finance");
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            id: "grp_finance",
            team_id: "team_1",
            workspace_id: "ws_finance",
            name: "Finance",
            description: "",
            status: "active",
            member_count: 0,
            created_by: "u_admin",
            created_at: "2026-06-05T00:00:00Z",
            updated_at: "2026-06-05T00:00:00Z",
          }),
        });
        return;
      }
      await route.fallback();
    });

    await page.goto("/admin/access/");
    await page.getByRole("textbox", { name: /名称|Name/ }).fill("Finance");
    await page.getByRole("textbox", { name: /空间 ID|Space ID/ }).fill("ws_finance");
    await page.getByRole("button", { name: /创建|Create/ }).click();
    await expect.poll(() => posted).toBe(true);
  });

  test("submits generic tool resource grant form", async ({ page }) => {
    let posted = false;
    await page.route("**/api/v1/resource-grants", async (route) => {
      if (route.request().method() === "POST") {
        posted = true;
        const body = JSON.parse(route.request().postData() || "{}");
        expect(body).toMatchObject({
          resource_kind: "tool",
          resource_id: "canvas_add_section",
          principal_type: "user",
          principal_id: "u_sales",
          permission: "use",
          effect: "deny",
        });
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            id: "grant_tool",
            team_id: "team_1",
            resource_kind: "tool",
            resource_id: "canvas_add_section",
            principal_type: "user",
            principal_id: "u_sales",
            permission: "use",
            effect: "deny",
            created_by: "u_admin",
            created_at: "2026-06-05T00:00:00Z",
            updated_at: "2026-06-05T00:00:00Z",
          }),
        });
        return;
      }
      await route.fallback();
    });

    await page.goto("/admin/access/");
    const grants = page.locator(".access-wide", {
      has: page.getByRole("heading", { name: /资源授权|Resource Grants/ }),
    });
    await grants.locator("select").nth(0).selectOption("tool");
    await grants.getByPlaceholder("resource_id").fill("canvas_add_section");
    await grants.locator("select").nth(1).selectOption("user");
    await grants.getByPlaceholder("principal_id").fill("u_sales");
    await grants.locator("select").nth(2).selectOption("use");
    await grants.locator("select").nth(3).selectOption("deny");
    await grants.getByRole("button", { name: /保存|Save/ }).click();
    await expect.poll(() => posted).toBe(true);
  });

  test("keeps the mobile page and primary actions inside the viewport", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/admin/access/");

    for (const control of [
      page.getByRole("button", { name: /刷新|Refresh/ }),
      page.getByRole("button", { name: /创建|Create/ }),
      page.getByRole("button", { name: /添加|Add/ }),
      page.getByRole("button", { name: /保存|Save/ }),
      page.getByRole("button", { name: /解释|Explain/ }),
    ]) {
      await expect(control).toBeVisible();
      const box = await control.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    }

    const dimensions = await page.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    }));
    expect(dimensions.documentWidth).toBeLessThanOrEqual(
      dimensions.viewportWidth,
    );
  });
});
