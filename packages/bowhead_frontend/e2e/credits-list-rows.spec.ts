import { expect, test, type Locator, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

/**
 * Real-browser page acceptance B (docs/design/组件统一-技术设计.md “逐页验收 B”, phone list rows):
 * viewport < 640 shows credit users as list rows. No information loss even at 320: long emails ellipsize in titles (full email in hover hints and whole-row
 * button names); plan/Admin/Disabled markers sit on row two, unshrunk/unclipped; row-three numbers use at most two lines without ellipsis;
 * removed inactive users cannot activate the whole row, with reason below; balances below 20% turn red (same table rule).
 * Fixture: `_credits-list-rows-entry.tsx`; page.route supplies users.
 */
const NOW = new Date().toISOString();
const LONG_EMAIL = "long.display.name@customer.example";
const user = (id: string, extra: Record<string, unknown> = {}) => ({
  user_id: id,
  email: `${id}@example.com`,
  display_name: null,
  tier: "pro",
  status: "active",
  credit_balance: 1000,
  credit_quota: 1000,
  credit_period_start: NOW,
  spend_30d: 10,
  last_activity_at: NOW,
  is_team_admin: false,
  attention_score: 0,
  activation_attention: null,
  primary_segment: null,
  segment_flags: [],
  recommended_action: null,
  ...extra,
});
const USERS = [
  user("usr_admin", {
    email: LONG_EMAIL,
    is_team_admin: true,
    display_name: "一个非常非常长的显示名用来测试溢出情况",
    tier: "ultra",
    credit_balance: 123456789,
    credit_quota: 987654321,
    spend_30d: 12345678,
  }),
  user("usr_removed_long", { email: `removed.${LONG_EMAIL}`, status: "removed", display_name: "已离职同事" }),
  user("usr_gone", { status: "removed", last_activity_at: null }),
  user("usr_low", { credit_balance: 199, credit_quota: 1000 }),
  user("usr_edge", { credit_balance: 200, credit_quota: 1000 }),
];

async function open(page: Page, width: number) {
  await page.setViewportSize({ width, height: 844 });
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 404, contentType: "application/json", body: "{}" }));
  await page.route(/\/api\/v1\/admin\/credits\/users(\?.*)?$/, (route) =>
    route.fulfill({ json: { items: USERS, total: USERS.length, page: 1, size: 50 } }),
  );
  await installReferenceComponent(page, "", "_credits-list-rows-entry.tsx");
  await page.goto("/__reference_component_test");
  await expect(page.getByRole("heading", { name: "credits list rows" })).toBeVisible();
  await expect(page.locator(".credits-user-rows .ui-list-row")).toHaveCount(USERS.length);
}

const row = (page: Page, index: number) => page.locator(".credits-user-rows .ui-list-row").nth(index);

/** Element lies fully inside ancestor's visible box (all four edges) and has no internal clipping. */
async function fullyVisible(el: Locator, clipper: Locator) {
  const [box, clip] = await Promise.all([el.boundingBox(), clipper.boundingBox()]);
  expect(box, "element box").not.toBeNull();
  expect(clip, "clipper box").not.toBeNull();
  expect(box!.width).toBeGreaterThan(0);
  expect(box!.x).toBeGreaterThanOrEqual(clip!.x - 0.5);
  expect(box!.x + box!.width).toBeLessThanOrEqual(clip!.x + clip!.width + 0.5);
  expect(await el.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
}

for (const width of [320, 375, 390]) {
  test.describe(`额度用户列表行 ${width}px`, () => {
    test("长邮箱在标题里省略，标记与显示名在第二行，标记不被裁", async ({ page }) => {
      await open(page, width);
      for (const index of [0, 1]) {
        const r = row(page, index);
        const title = r.locator(".ui-list-row-title");
        const sub = r.locator(".ui-list-row-sub");
        // Title contains email only: ellipsize if needed; full email remains in hover hint and whole-row button name.
        expect(await title.evaluate((el) => getComputedStyle(el).textOverflow)).toBe("ellipsis");
        const email = index === 0 ? LONG_EMAIL : `removed.${LONG_EMAIL}`;
        await expect(title.locator("[title]")).toHaveAttribute("title", email);
        await expect(r.getByRole("button", { name: email })).toHaveCount(1);
        // Markers fully visible (inside second row's visible box, unshrunk).
        const tag = sub.locator(".legacy-credits-tag");
        await expect(tag).toHaveCount(1);
        await fullyVisible(tag, sub);
        await expect(tag).toHaveText(index === 0 ? "管理员" : "已停用");
        // Plan badge also sits on row two (not at row end), fully visible.
        const tier = sub.locator(".legacy-credits-tier");
        await fullyVisible(tier, sub);
        await expect(tier).toHaveText(index === 0 ? "ULTRA" : "PRO");
        // Ellipsize an overlong display name without pushing markers out.
        const name = sub.locator(".credits-user-listrow-name");
        expect(await name.evaluate((el) => getComputedStyle(el).textOverflow)).toBe("ellipsis");
      }
    });

    test("第三行最多两行，数字不被省略", async ({ page }) => {
      await open(page, width);
      const meta = row(page, 0).locator(".ui-list-row-meta");
      const { lines, clipped } = await meta.evaluate((el) => {
        const lh = parseFloat(getComputedStyle(el).lineHeight);
        return {
          lines: Math.round(el.getBoundingClientRect().height / lh),
          clipped: el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1,
        };
      });
      expect(lines).toBeLessThanOrEqual(2);
      expect(clipped).toBe(false);
      await expect(meta).toContainText("123,456,789");
      await expect(meta).toContainText("987,654,321");
      await expect(meta).toContainText("12,345,678");
    });

    test("已移出且无活动：点整行不跳转，行下方写出原因；有活动的已移出用户仍可进明细", async ({ page }) => {
      await open(page, width);
      const gone = row(page, 2);
      await expect(gone.locator(".ui-list-row-open")).toHaveCount(0);
      await expect(gone.locator(".credits-user-listrow-reason")).toHaveText("该成员已移出团队且没有用量记录，没有可查看的用量明细");
      await gone.locator(".ui-list-row-title").click();
      expect(await page.evaluate(() => window.creditsListRowsEvents)).toEqual([]);

      await row(page, 1).getByRole("button", { name: `removed.${LONG_EMAIL}` }).click();
      expect(await page.evaluate(() => window.creditsListRowsEvents)).toEqual([
        "push:/admin/credits/users/?user_id=usr_removed_long",
      ]);
    });

    test("余额低于 20% 标红，恰好 20% 不标（与表格同一规则）", async ({ page }) => {
      await open(page, width);
      await expect(row(page, 3).locator(".ui-list-row-meta .legacy-credits-balance-low")).toHaveText("199");
      await expect(row(page, 4).locator(".ui-list-row-meta .legacy-credits-balance-low")).toHaveCount(0);
    });
  });
}
