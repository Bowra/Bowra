import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

for (const locale of ["zh-CN", "en-US"] as const) {
  for (const width of [412, 1440]) {
    test.describe(`${locale} / ${width}`, () => {
      test.use({ locale, viewport: { width, height: 960 } });
      test("saves with confirmation and reloads the durable policy", async ({ page }, info) => {
        await mockAuthedApp(page, { subagentManagement: true });
        const zh = locale === "zh-CN";
        await page.context().addCookies([{ name: "NEXT_LOCALE", value: locale, domain: "localhost", path: "/" }]);
        let policy = { days: 180, version: 0 };
        const writes: unknown[] = [];
        await page.route("**/api/v1/team-settings/reference-retention", async (route) => {
          if (route.request().method() === "PUT") {
            const body = route.request().postDataJSON();
            writes.push(body);
            if (body.expected_version !== policy.version) {
              await route.fulfill({ status: 409, json: { detail: { error: { code: "reference_retention_policy_conflict" } } } });
              return;
            }
            policy = { days: body.days, version: policy.version + 1 };
          }
          await route.fulfill({ json: policy });
        });
        await page.goto("/team/settings/people/");
        // Settings destinations live in the sidebar's settings directory (the mobile drawer on phones).
        if (width === 412) await page.getByTestId("mobile-topbar-hamburger").click();
        await page.getByRole("link", { name: zh ? "引用材料保留期限" : "Reference Retention", exact: true }).click();
        await expect(page).toHaveURL(/\/team\/settings\/reference-retention\/?$/);
        const input = page.getByLabel(zh ? "最多保留天数" : "Maximum retention in days");
        await expect(input).toHaveValue("180");
        await input.fill("7");
        // The save bar saves; shortening the limit asks for confirmation first.
        const save = page.getByRole("button", { name: zh ? "保存" : "Save", exact: true });
        await save.click();
        await expect(page.getByRole("dialog")).toBeVisible();
        expect(writes).toHaveLength(0);
        await page.getByRole("button", { name: zh ? "取消" : "Cancel", exact: true }).click();
        await expect(page.getByRole("dialog")).toHaveCount(0);
        await save.click();
        await page.getByRole("button", { name: zh ? "确认缩短" : "Confirm Shorter Limit", exact: true }).click();
        await expect(page.getByText(zh ? "保留期限已保存。" : "Retention limit saved.", { exact: true })).toBeVisible();
        expect(writes).toEqual([{ days: 7, expected_version: 0 }]);
        await page.reload();
        await expect(input).toHaveValue("7");
        expect(writes).toHaveLength(1);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        await page.screenshot({ path: info.outputPath("retention-settings.png"), fullPage: true });
      });
    });
  }
}

test("a member cannot open the administrative editor or fetch its policy", async ({ page }) => {
  await mockAuthedApp(page);
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: {
    id: "u_member", team_id: "team_smoke", email: "member@example.com", status: "active", tier: "pro",
    team_role: "member", is_team_admin: false, capabilities: {}, needs_onboarding: false,
  } }));
  let reads = 0;
  await page.route("**/api/v1/team-settings/reference-retention", (route) => { reads++; return route.fulfill({ json: { days: 180, version: 0 } }); });
  await page.goto("/team/settings/reference-retention/");
  await expect(page.getByText("只有团队负责人或团队管理员可以访问团队设置。", { exact: true })).toBeVisible();
  expect(reads).toBe(0);
  await expect(page.getByLabel(/最多保留天数|Maximum retention/)).toHaveCount(0);
});
