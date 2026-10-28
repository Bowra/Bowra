import { expect, test, type Page, type Route } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

const json = (body: unknown, status = 200) => ({
  status, contentType: "application/json", body: JSON.stringify(body),
});

async function asAdmin(page: Page): Promise<void> {
  // Override _app-mocks /auth/me to add is_team_admin (email card is admin-gated).
  await page.route("**/api/v1/auth/me", async (route: Route) => {
    await route.fulfill(json({
      id: "u_admin", email: "admin@example.com", name: "Admin",
      tier: "ultra", credit_balance: 1000, credit_quota: 10000,
      is_team_admin: true,
    }));
  });
  await page.route("**/api/v1/auth/capabilities", async (route: Route) => {
    await route.fulfill(json({
      deployment_mode: "local",
      account_creation_policy: "invite_only",
      email_verification_policy: "not_required",
      auth_methods: ["password"],
      public_registration: false,
      bootstrap_required: false,
      invite_activation: true,
    }));
  });
}

test("Email service — not-configured state shows warning + form", async ({ page }) => {
  await mockAuthedApp(page);
  await asAdmin(page);
  await page.route("**/api/v1/admin/email-config", async (route: Route) => {
    if (route.request().method() === "GET") {
      await route.fulfill(json({ configured: "none", version: 0 }));
    } else if (route.request().method() === "PUT") {
      await route.fulfill(json({ ok: true }));
    } else {
      await route.fulfill(json({ ok: true }));
    }
  });

  await page.goto("/team/settings/email-service/");
  await expect(page.getByRole("heading", { level: 1, name: "邮件服务 (SMTP)" })).toBeVisible({ timeout: 30000 });
  await expect(page.getByText("未配置", { exact: true })).toBeVisible();
  await expect(page.getByText(/邀请新成员、邮箱验证/)).toBeVisible();

  await expect(page.getByPlaceholder("smtp.feishu.cn")).toBeVisible();
  await expect(page.getByLabel("加密方式")).toHaveValue("starttls");
  // Send Test is disabled when unconfigured.
  await expect(page.getByRole("button", { name: "发送测试邮件" })).toBeDisabled();
});

test("Email service — team-configured state shows fingerprint, no plaintext", async ({ page }) => {
  await mockAuthedApp(page);
  await asAdmin(page);
  await page.route("**/api/v1/admin/email-config", async (route: Route) => {
    await route.fulfill(json({
      configured: "team", version: 3,
      config: {
        host: "smtp.exmail.qq.com", port: 465, user: "bot@acme.com",
        from_addr: "no-reply@acme.com", from_name: "Acme", tls: true,
        password_set: true,
      },
    }));
  });

  await page.goto("/team/settings/email-service/");
  await expect(page.getByText("已配置(团队)")).toBeVisible({ timeout: 30000 });
  // Populate host; password stays empty (fingerprint only, never return plaintext).
  await expect(page.locator("#smtp-host")).toHaveValue("smtp.exmail.qq.com");
  await expect(page.getByLabel("加密方式")).toHaveValue("ssl");
  await expect(page.locator("#smtp-pass")).toHaveValue("");
  await expect(page.getByRole("button", { name: "发送测试邮件" })).toBeEnabled();
});
