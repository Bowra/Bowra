import { PRODUCT_NAME } from "../lib/product-identity";
/**
 * Critical paths of signed-out public pages (sign-in, password reset, invitation acceptance): layout order, field error association, keyboard order,
 * dark-mode following, brand injection and white-labelling, result cards. Spec: the "公开页面" (public pages) section of docs/design/组件统一-技术设计.md.
 *
 * Every non-GET request is intercepted and answered in the browser; no form is ever submitted to a server.
 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

const CUSTOM_BRAND = JSON.parse(
  readFileSync(path.join(__dirname, "fixtures", "public-brand-custom.json"), "utf8"),
) as Record<string, unknown>;

const CAPABILITIES = {
  deployment_mode: "saas",
  account_creation_policy: "open",
  email_verification_policy: "not_required",
  auth_methods: ["password"],
  public_registration: true,
  bootstrap_required: false,
  invite_activation: true,
};

type Fake = { status?: number; body: unknown } | "hang";
type Traffic = { theme: number; me: number; unhandledWrites: string[] };

/** Read endpoints get fixed responses; write endpoints are answered locally (undeclared ones get 400) and never reach a server. */
async function mockPublicApi(
  page: Page,
  {
    sso = false,
    writes = {},
  }: { sso?: boolean; writes?: Record<string, Fake> } = {},
): Promise<Traffic> {
  const traffic: Traffic = { theme: 0, me: 0, unhandledWrites: [] };
  await page.route(/\/api\/v1\/|\/health$/, async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    if (request.method() !== "GET" && request.method() !== "HEAD") {
      const key = Object.keys(writes).find((suffix) => pathname.endsWith(suffix));
      if (!key) {
        traffic.unhandledWrites.push(`${request.method()} ${pathname}`);
        await route.fulfill({ status: 400, json: { detail: { code: "blocked_in_test" } } });
        return;
      }
      const fake = writes[key];
      if (fake === "hang") return;
      await route.fulfill({ status: fake.status ?? 200, json: fake.body });
      return;
    }
    if (pathname.endsWith("/theme/effective")) traffic.theme += 1;
    if (pathname.endsWith("/auth/me")) traffic.me += 1;
    if (pathname.endsWith("/auth/capabilities")) {
      await route.fulfill({ json: CAPABILITIES });
    } else if (pathname.endsWith("/auth/login-capabilities")) {
      await route.fulfill({
        json: sso
          ? { oauth_providers: ["feishu"], enterprise_oidc: false }
          : { oauth_providers: [], enterprise_oidc: false },
      });
    } else if (pathname.endsWith("/bootstrap/status")) {
      await route.fulfill({ json: { required: false, deployment_mode: "saas" } });
    } else if (pathname === "/health") {
      await route.fulfill({ json: { status: "ok", feature_flags: {} } });
    } else if (pathname.startsWith("/api/v1/branding/assets/")) {
      await route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="32"><rect width="96" height="32" rx="6" fill="#0b6b57"/></svg>',
      });
    } else {
      await route.fulfill({ status: 401, json: { detail: "unauthorized" } });
    }
  });
  return traffic;
}

/** On a cold dev server, values typed before hydration are wiped by controlled inputs: confirm the value stuck, otherwise refill. */
async function fillStable(locator: import("@playwright/test").Locator, value: string): Promise<void> {
  await expect(async () => {
    await locator.fill(value);
    await locator.page().waitForTimeout(150);
    await expect(locator).toHaveValue(value, { timeout: 200 });
  }).toPass({ timeout: 15_000 });
}

function tabKey(browserName: string): string {
  // Safari's default Tab skips buttons and links (a system setting); Alt+Tab walks the full keyboard navigation order.
  return browserName === "webkit" ? "Alt+Tab" : "Tab";
}

async function focusedName(page: Page): Promise<string> {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el) return "";
    const labelled = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.textContent : null;
    return (el.getAttribute("aria-label") || labelled || el.textContent || "").trim();
  });
}

test.describe("public pages: sign-in", () => {
  test("SSO sits above the form and Tab order matches visual order", async ({ page, browserName }) => {
    const traffic = await mockPublicApi(page, { sso: true });
    await page.goto("/login/");

    const feishu = page.getByRole("button", { name: "使用飞书登录" });
    const divider = page.getByText("或使用邮箱登录");
    const email = page.getByLabel("邮箱");
    await expect(feishu).toBeVisible();
    const ys = await Promise.all(
      [feishu, divider, email].map(async (locator) => (await locator.boundingBox())!.y),
    );
    expect(ys).toEqual([...ys].sort((a, b) => a - b));

    await page.locator("body").click({ position: { x: 5, y: 5 } });
    const expected = [
      "使用飞书登录",
      "邮箱",
      "密码",
      "显示密码",
      "忘记密码？",
      "登录",
      "立即注册",
      "界面语言：中文",
    ];
    const seen: string[] = [];
    for (let index = 0; index < expected.length; index += 1) {
      await page.keyboard.press(tabKey(browserName));
      seen.push(await focusedName(page));
    }
    expect(seen).toEqual(expected);
    expect(traffic.theme).toBe(0);
    expect(traffic.me).toBe(0);
  });

  test("empty submit and format errors show under the field, linked via aria-describedby", async ({ page }) => {
    const traffic = await mockPublicApi(page);
    await page.goto("/login/");
    const email = page.getByLabel("邮箱");
    await expect(email).toBeVisible();

    await page.getByRole("button", { name: "登录", exact: true }).click();
    const emailError = page.getByText("请输入邮箱", { exact: true });
    await expect(emailError).toBeVisible();
    await expect(email).toBeFocused();
    await expect(email).toHaveAttribute("aria-invalid", "true");
    const errorId = await emailError.evaluate((node) => node.closest("p")?.id);
    await expect(email).toHaveAttribute("aria-describedby", errorId!);
    await expect(page.getByText("请输入密码", { exact: true })).toBeVisible();

    await email.fill("li.ming@northstar");
    await email.blur();
    await expect(page.getByText(/邮箱格式不对/)).toBeVisible();
    expect(traffic.unhandledWrites).toEqual([]);
  });

  test("bad credentials keep the email, clear and focus the password; the banner hides technical details by default", async ({ page }) => {
    const traffic = await mockPublicApi(page, {
      writes: {
        "/api/v1/auth/login": {
          status: 401,
          body: { detail: { code: "invalid_credentials", message: "invalid credentials" } },
        },
      },
    });
    await page.goto("/login/");
    await fillStable(page.getByLabel("邮箱"), "li.ming@northstar.example");
    await fillStable(page.getByLabel("密码", { exact: true }), "wrong-password1");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    // Next's route announcer is also role="alert": only look at the form banner
    await expect(page.locator(".pub-banner[role=alert]")).toContainText("邮箱或密码不正确");
    await expect(page.getByLabel("密码", { exact: true })).toBeFocused();
    await expect(page.getByLabel("密码", { exact: true })).toHaveValue("");
    await expect(page.getByLabel("邮箱")).toHaveValue("li.ming@northstar.example");
    await expect(page.getByText("查看技术详情")).toHaveCount(0);
    expect(traffic.unhandledWrites).toEqual([]);
  });

  test("the product default brand follows system dark mode without requesting the authenticated theme API", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    const traffic = await mockPublicApi(page);
    await page.goto("/login/");
    await expect(page.getByTestId("public-brand")).toContainText(PRODUCT_NAME);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const background = await page.locator(".pub-page").evaluate((node) => getComputedStyle(node).backgroundColor);
    // Product default dark background is the SaaS Blue scheme (#0F172A) since the default palette change.
    expect(background).toBe("rgb(15, 23, 42)");

    await page.emulateMedia({ colorScheme: "light" });
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    expect(traffic.theme).toBe(0);
    expect(traffic.me).toBe(0);
  });

  test("a custom brand is fully white-labelled; a broken injection falls back to the product default and sign-in still works", async ({ page }) => {
    await mockPublicApi(page, { sso: true });
    await page.addInitScript((brand) => {
      (window as Window & { __BOWHEAD_PUBLIC_BRAND__?: unknown }).__BOWHEAD_PUBLIC_BRAND__ = brand;
    }, CUSTOM_BRAND);
    await page.goto("/login/");
    const brandRow = page.getByTestId("public-brand");
    await expect(brandRow).toContainText("Northstar Data");
    await expect(brandRow.locator("img").first()).toBeVisible();
    await expect(page.locator("main")).not.toContainText(PRODUCT_NAME);
    await expect(page.locator("main")).not.toContainText(/verified intelligence/i);
    // The tab title is only the customer application name, and the post-hydration title controller does not add the tagline back
    await expect(page).toHaveTitle("Northstar Data");
    await page.waitForTimeout(800);
    expect(await page.title()).toBe("Northstar Data");
    await expect(page.getByRole("button", { name: "使用飞书登录" })).toBeEnabled();

    const broken = await page.context().newPage();
    await mockPublicApi(broken, { sso: true });
    await broken.addInitScript(() => {
      (window as Window & { __BOWHEAD_PUBLIC_BRAND__?: unknown }).__BOWHEAD_PUBLIC_BRAND__ = {
        schemaVersion: 2,
        brand: 42,
      };
    });
    await broken.goto("/login/?next=%2Freports%2F");
    await expect(broken.getByTestId("public-brand")).toContainText(PRODUCT_NAME);
    await expect(broken).toHaveTitle(`${PRODUCT_NAME} — Verified Intelligence`);
    await expect(broken.getByRole("button", { name: "使用飞书登录" })).toBeEnabled();
    await expect(broken.getByLabel("邮箱")).toBeEditable();
    await broken.close();
  });

  test("the language button opens a selection menu with a leading check on the current language; Esc closes it and returns focus", async ({ page }) => {
    await mockPublicApi(page);
    await page.goto("/login/");
    const trigger = page.getByRole("button", { name: "界面语言：中文" });
    await trigger.click();
    const current = page.getByRole("menuitemradio", { name: "中文" });
    await expect(current).toHaveAttribute("aria-checked", "true");
    await expect(page.getByRole("menuitemradio", { name: "English" })).toHaveAttribute("aria-checked", "false");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });
});

test.describe("public pages: no authenticated API requests", () => {
  const pages: Array<[string, string, Record<string, Fake>]> = [
    ["register", "/register/", {}],
    ["forgot-password", "/forgot-password/", {}],
    ["reset-password", "/reset-password/?flow=self_service&token=e2e-token", {}],
    [
      "invite",
      "/invite/?token=e2e-invite",
      {
        "/api/v1/auth/invites/intent": {
          body: { status: "ready", invite_type: "team", invited_email_mask: "li***@northstar.example", team: { name: "北辰零售" }, next_action: "activate_password" },
        },
        "/api/v1/auth/invites/intent/continue": {
          body: { status: "ready", invite_type: "team", invited_email_mask: "li***@northstar.example", team: { name: "北辰零售" }, next_action: "activate_password" },
        },
      },
    ],
  ];
  for (const [name, url, writes] of pages) {
    test(`${name} page requests neither theme/effective nor auth/me`, async ({ page }) => {
      const traffic = await mockPublicApi(page, { writes });
      await page.goto(url);
      await expect(page.locator("h1")).toBeVisible();
      await expect(page.locator(".pub-page")).toBeVisible();
      await page.waitForTimeout(800);
      expect(traffic.theme).toBe(0);
      expect(traffic.me).toBe(0);
    });
  }
});

test.describe("public pages: password reset", () => {
  test("an invalid link shows only the result card and the next step", async ({ page }) => {
    const traffic = await mockPublicApi(page);
    await page.goto("/reset-password/?token=old-link");
    await expect(page.getByRole("heading", { name: "链接已失效" })).toBeVisible();
    await expect(page.getByRole("link", { name: "重新申请重设链接" })).toHaveAttribute(
      "href",
      /\/forgot-password\/$/,
    );
    await expect(page.locator("input")).toHaveCount(0);
    expect(traffic.unhandledWrites).toEqual([]);
  });

  test("completion shows a result card and a button without auto-redirect", async ({ page }) => {
    const traffic = await mockPublicApi(page, {
      writes: { "/api/v1/auth/reset-password": { body: { ok: true } } },
    });
    await page.goto("/reset-password/?flow=self_service&token=e2e-token");
    await fillStable(page.getByLabel("新密码", { exact: true }), "Northstar2026");
    await fillStable(page.getByLabel("确认密码", { exact: true }), "Northstar2026");
    await expect(page.getByText("包含数字")).toBeVisible();
    await page.getByRole("button", { name: "保存新密码" }).click();

    const heading = page.getByRole("heading", { name: "密码已更新" });
    await expect(heading).toBeVisible();
    await expect(heading).toBeFocused();
    const enter = page.getByRole("link", { name: "进入 Bowhead" });
    await expect(enter).toHaveAttribute("href", /\/$/);
    await page.waitForTimeout(2500);
    await expect(page).toHaveURL(/\/reset-password\/$/);
    expect(traffic.unhandledWrites).toEqual([]);
  });
});

test.describe("public pages: invitation acceptance", () => {
  const preview = {
    status: "ready",
    invite_type: "workspace",
    invited_email_mask: "li***@northstar.example",
    expires_at: "2026-10-05T08:00:00Z",
    team: { name: "北辰零售" },
    workspace: { name: "华东经营分析", role: "member" },
    viewer_context: { state: "anonymous" },
    next_action: "activate_password",
    inviter_display_name: "李华",
  };

  test("a valid invitation names the inviter; on phones the steps are not truncated and there is no horizontal scroll", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const traffic = await mockPublicApi(page, {
      writes: {
        "/api/v1/auth/invites/intent": { body: preview },
        "/api/v1/auth/invites/intent/continue": { body: preview },
      },
    });
    await page.goto("/invite/?token=e2e-invite");

    await expect(page.getByRole("heading", { name: "加入「华东经营分析」" })).toBeVisible();
    await expect(page.getByText("李华 邀请你加入「华东经营分析」")).toBeVisible();
    await expect(page.getByText("第 1 步，共 3 步")).toBeVisible();
    const truncated = await page.locator(".pub-steps__text").evaluate(
      (node) => node.scrollWidth > node.clientWidth + 1,
    );
    expect(truncated).toBe(false);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    await expect(page.getByText("接下来会发生什么")).toHaveCount(0);
    expect(traffic.me).toBe(0);
    expect(traffic.unhandledWrites).toEqual([]);
  });

  test("under a custom brand the product name in invitation copy becomes the team application name", async ({ page }) => {
    await mockPublicApi(page, {
      writes: {
        "/api/v1/auth/invites/intent": { body: preview },
        "/api/v1/auth/invites/intent/continue": { body: preview },
      },
    });
    await page.addInitScript((brand) => {
      (window as Window & { __BOWHEAD_PUBLIC_BRAND__?: unknown }).__BOWHEAD_PUBLIC_BRAND__ = brand;
    }, CUSTOM_BRAND);
    await page.goto("/invite/?token=e2e-invite");
    await expect(page.getByRole("heading", { name: "加入「华东经营分析」" })).toBeVisible();
    await expect(page.getByText(/Northstar Data 空间/)).toBeVisible();
    await expect(page.locator("main")).not.toContainText(PRODUCT_NAME);
  });

  test("an already accepted invitation offers Sign in instead of Back to sign-in", async ({ page }) => {
    await mockPublicApi(page, {
      writes: {
        "/api/v1/auth/invites/intent": {
          status: 409,
          body: { detail: { code: "invite_consumed" } },
        },
      },
    });
    await page.goto("/invite/?token=e2e-invite");
    await expect(page.getByRole("heading", { name: "这条邀请已经接受过" })).toBeVisible();
    await expect(page.getByRole("link", { name: "登录", exact: true })).toHaveAttribute("href", /\/login\/$/);
  });
});
