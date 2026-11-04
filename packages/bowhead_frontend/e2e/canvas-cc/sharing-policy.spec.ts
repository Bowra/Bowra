import { expect, test } from "@playwright/test";

const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";

for (const enabled of [true, false]) {
  test(`report sharing preserves management with deployment policy ${enabled}`, async ({ page }) => {
    await page.request.post(`http://127.0.0.1:${apiPort}/api/v1/_test/reset`);
    await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
      dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50,
      interactive_html: true, content_sharing: { enabled, reason: enabled ? null : "deployment_disabled" },
    } }));
    let revoked = false;
    let expanded = false;
    await page.route("**/api/v1/team/directory**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));
    await page.route("**/api/v1/team/groups-directory", (route) => route.fulfill({ json: { items: [] } }));
    await page.route("**/api/v1/shares/report/**", (route) => {
      const request = route.request();
      if (request.method() === "DELETE") {
        revoked = true;
        return route.fulfill({ status: 204 });
      }
      if (request.method() === "PUT" || request.method() === "POST") {
        expanded = true;
        return route.fulfill({ json: { audience: "team_wide", revoked: 0 } });
      }
      return route.fulfill({ json: {
        kind: "report", resource_id: "s_e2e", label: "Sharing fixture", audience: "custom",
        owner_id: "owner", can_manage: true,
        grants: revoked ? [] : [{ id: "grant_fixture", principal_type: "user", principal_id: "recipient",
          principal_name: "Sharing recipient", permission: "read", created_by: "owner", created_at: "2026-11-02T00:00:00Z" }],
      } });
    });
    await page.goto("/");
    const composer = page.locator("textarea").first();
    await expect(composer).toBeVisible();
    await composer.fill("resultsworkspace");
    await composer.press("Enter");
    await expect(page.locator('[data-presentation="results-reading"]')).toHaveCount(1);
    await page.locator(".results-report-share").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Sharing recipient", { exact: true })).toBeVisible();
    const team = dialog.locator('[data-composite-value="team_wide"]');
    if (enabled) {
      await expect(team).toBeEnabled();
      await team.click();
      await expect.poll(() => expanded).toBe(true);
    } else {
      await expect(team).toBeDisabled();
      await expect(dialog.getByRole("status")).toContainText("此部署已关闭分享");
      await expect(dialog.locator(".legacy-share-search-row")).toHaveCount(0);
      await dialog.locator(".legacy-share-row-remove").click();
      await expect.poll(() => revoked).toBe(true);
      expect(expanded).toBe(false);
    }
  });
}
