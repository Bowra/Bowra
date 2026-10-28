import { expect, test } from "@playwright/test";

const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";

async function resetMock(page: import("@playwright/test").Page) {
  await page.request.post(`http://127.0.0.1:${apiPort}/api/v1/_test/reset`);
}

async function installStandardResultsApi(page: import("@playwright/test").Page) {
  await page.route(
    (url) => url.pathname === "/api/v1/results/capabilities",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          dto_version: 1,
          workspace: true,
          complete_list: true,
          scale_nav: true,
          max_page_size: 50,
        }),
      });
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/sessions\/[^/]+\/results\/query$/.test(url.pathname),
    async (route) => {
      const request = route.request().postDataJSON() as { query?: string };
      const querying = Boolean(request.query?.trim());
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          dto_version: 1,
          capability: { complete_list: true, scale_nav: true },
          snapshot_token: "snapshot",
          items: [{
            key: "artifact:art_stdout",
            object_type: "artifact",
            title: "sandbox-stdout.txt",
            primary_group: "evidence",
            secondary_group: "data",
            viewer_profile: "text",
            status: "ready",
            route: {
              type: "artifact",
              artifact_id: "art_stdout",
              default_mode: "preview",
            },
            capabilities: ["download", "view_details", "view_raw"],
            relation_roles: ["report_detail_table"],
            execution: null,
            display_context: {
              source_role: "sandbox",
              output_role: "report_detail_table",
              execution_label: null,
              occurred_at: "2026-08-25T00:00:00Z",
              duplicate_ordinal: null,
              duplicate_count: null,
            },
            position: 101,
            version: null,
            artifact_metadata: {
              mime: "text/plain",
              size: 12,
              source: "python_sandbox",
              kind: "tool_data",
              origin: "compute",
              trust: "trusted",
              created_at: "2026-08-25T00:00:00Z",
              producer_kind: "python_sandbox",
            },
          }],
          authorized_total: 135,
          matched_total: querying ? 1 : 135,
          group_counts: {
            deliverables: 35,
            evidence: 100,
            data: 70,
            code: 10,
            input: 20,
          },
          loaded_count: 1,
          page_start: 0,
          next_cursor: null,
          previous_cursor: null,
          has_more: false,
          projection_status: {
            report_relations: "ready",
            execution_relations: "unavailable",
            export_relations: "degraded",
          },
          projection_revision: "revision",
          new_results: { count: 0, projection_changed: false },
          resolved_current_key: null,
        }),
      });
    },
  );
}

async function sendMessage(page: import("@playwright/test").Page, message: string) {
  const textarea = page.locator("textarea").first();
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill(message);
  await textarea.press("Enter");
}

test.beforeEach(async ({ page }) => {
  await resetMock(page);
  await installStandardResultsApi(page);
});

test("standard Results opens Quick and browses all without a rollout flag", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/");
  await sendMessage(page, "resultsworkspace");

  await expect(page.locator(".v2-ws-right")).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "成果", exact: true }).click();

  const quick = page.getByRole("dialog", { name: "成果", exact: true });
  await expect(quick).toBeVisible();
  // The default expanded sidebar leaves the Results pane under 640px, so Quick uses its compact
  // layout; a fine pointer still focuses search there.
  await expect(page.locator(".results-quick--compact")).toBeVisible();
  await expect(quick.getByRole("combobox", { name: "搜索全部成果" })).toBeFocused();
  await expect(quick.getByRole("button", { name: "浏览全部成果（135）" })).toBeVisible();

  await quick.getByRole("button", { name: "浏览全部成果（135）" }).click();

  await expect(page).toHaveURL(/results=all/);
  await expect(page.getByRole("heading", { name: "全部成果", exact: true })).toBeVisible();
  await expect(page.getByText("共 135 个成果 · 已加载 1 个")).toBeVisible();

  // The narrow pane folds the explorer filters into a 「筛选」 disclosure.
  await page.locator(".results-explorer-compact-filters summary").click();
  const evidenceType = page.getByRole("radiogroup", { name: "分析依据类型" });
  await expect(evidenceType).toBeVisible();
  await evidenceType.getByRole("radio", { name: "数据", exact: true }).click();
  await expect(evidenceType.getByRole("radio", { name: "数据", exact: true }))
    .toHaveAttribute("aria-checked", "true");
  await expect(page.getByRole("button", { name: /sandbox-stdout\.txt/ })).toBeVisible();
});
