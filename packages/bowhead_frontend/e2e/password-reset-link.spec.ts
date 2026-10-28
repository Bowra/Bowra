import { expect, test, type Page, type Request } from "@playwright/test";

type AuthRequest = {
  method: string;
  pathname: string;
  body: unknown;
};

function captureAuthRequests(page: Page): AuthRequest[] {
  const requests: AuthRequest[] = [];
  page.on("request", (request: Request) => {
    const url = new URL(request.url());
    if (!url.pathname.includes("/api/v1/auth/")) return;
    requests.push({
      method: request.method(),
      pathname: url.pathname,
      body: request.postDataJSON(),
    });
  });
  return requests;
}

async function fillNewPassword(page: Page): Promise<void> {
  const passwordInputs = page.locator('input[type="password"]');
  await expect(passwordInputs).toHaveCount(2);
  await passwordInputs.nth(0).fill("NewPassw0rd!");
  await passwordInputs.nth(1).fill("NewPassw0rd!");
}

test("self-service link survives native history cleanup and uses JWT reset", async ({
  page,
}) => {
  const requests = captureAuthRequests(page);
  await page.route("**/api/v1/auth/reset-password", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    });
  });

  await page.goto(
    "/reset-password?flow=self_service&token=e2e-self-service-token",
  );

  await expect(page).toHaveURL(/\/reset-password\/$/);
  await expect(page.getByRole("button", { name: "保存新密码" })).toBeEnabled();
  expect(requests).toEqual([]);

  await fillNewPassword(page);
  await page.getByRole("button", { name: "保存新密码" }).click();

  await expect(page.getByRole("heading", { name: "密码已更新" })).toBeVisible();
  await expect(page.getByRole("link", { name: "进入 Bowhead" })).toBeVisible();
  expect(requests).toEqual([
    {
      method: "POST",
      pathname: "/api/v1/auth/reset-password",
      body: {
        token: "e2e-self-service-token",
        new_password: "NewPassw0rd!",
      },
    },
  ]);
});

test("admin link exchanges once and completes through the recovery cookie protocol", async ({
  page,
}) => {
  const requests = captureAuthRequests(page);
  await page.route(
    "**/api/v1/auth/credential-recovery/intent",
    async (route) => {
      await route.fulfill({
        contentType: "application/json",
        headers: {
          "set-cookie":
            "bowhead_recovery_intent=e2e-intent; HttpOnly; Path=/; SameSite=Strict",
        },
        body: JSON.stringify({
          ok: true,
          status: "ready",
        }),
      });
    },
  );
  await page.route(
    "**/api/v1/auth/credential-recovery/complete",
    async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ ok: true, login_required: true }),
      });
    },
  );

  await page.goto(
    "/reset-password?flow=admin&token=e2e-admin-token-long-enough",
  );

  await expect(page).toHaveURL(/\/reset-password\/$/);
  await expect(page.getByRole("button", { name: "保存新密码" })).toBeEnabled();
  expect(
    requests.filter(
      (request) =>
        request.pathname === "/api/v1/auth/credential-recovery/intent",
    ),
  ).toHaveLength(1);

  await fillNewPassword(page);
  await page.getByRole("button", { name: "保存新密码" }).click();

  await expect(page.getByRole("heading", { name: "密码已更新" })).toBeVisible();
  await expect(page.getByRole("link", { name: "去登录" })).toBeVisible();
  expect(
    requests.filter(
      (request) =>
        request.pathname === "/api/v1/auth/credential-recovery/complete",
    ),
  ).toHaveLength(1);
  expect(
    requests.filter((request) => request.pathname.endsWith("/reset-password")),
  ).toHaveLength(0);
});

test("old links fail closed without any authentication request", async ({
  page,
}) => {
  const requests = captureAuthRequests(page);

  await page.goto("/reset-password?token=e2e-old-link-token");

  await expect(page).toHaveURL(/\/reset-password\/$/);
  await expect(page.getByRole("heading", { name: "链接已失效" })).toBeVisible();
  await expect(page.getByRole("link", { name: "重新申请重设链接" })).toBeVisible();
  await expect(page.getByRole("button", { name: "保存新密码" })).toHaveCount(0);
  expect(requests).toEqual([]);
});
