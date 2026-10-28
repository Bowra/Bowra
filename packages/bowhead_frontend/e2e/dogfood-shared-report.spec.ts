import { expect, test, type Route } from "@playwright/test";

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function mockSharedReport(page: import("@playwright/test").Page) {
  await page.route("**/health", async (route: Route) => {
    await route.fulfill(json({
      feature_flags: {
        results_workspace: true,
      },
    }));
  });
  await page.route("**/api/v1/auth/me", async (route: Route) => {
    await route.fulfill(json({
      id: "viewer",
      email: "viewer@example.com",
      name: "Viewer",
      tier: "ultra",
      capabilities: { access_requests: true },
      entitlement: { read_only: false },
    }));
  });
  await page.route("**/api/v1/auth/me/workspaces", async (route: Route) => {
    await route.fulfill(json({
      items: [{ workspace_id: "ws_shared", name: "Shared", description: "" }],
      active_id: "ws_shared",
    }));
  });
  await page.route("**/api/v1/user-settings**", async (route: Route) => {
    await route.fulfill(json({ settings: {} }));
  });
  await page.route("**/api/v1/theme/effective", async (route: Route) => {
    await route.fulfill(json({ mode: "light", tokens: {} }));
  });
  await page.route("**/api/v1/team-settings/theme-policy", async (route: Route) => {
    await route.fulfill(json({ value: null }));
  });
  await page.route("**/api/v1/llm/catalog", async (route: Route) => {
    await route.fulfill(json({ providers: [], defaults: {} }));
  });
  await page.route("**/api/v1/sessions", async (route: Route) => {
    await route.fulfill(json({
      groups: [{
        id: "shared",
        label: "Shared",
        items: [{ id: "s_shared", title: "Shared H1 Report", intents: ["report"], time: "09:00", state: "done" }],
      }],
    }));
  });
  await page.route("**/api/v1/sessions/s_shared/seen", async (route: Route) => {
    await route.fulfill(json({ ok: true }));
  });
  await page.route("**/api/v1/sessions/s_shared", async (route: Route) => {
    await route.fulfill(json({ detail: "forbidden" }, 403));
  });
  await page.route("**/api/v1/canvas/by-session/s_shared**", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/artifacts")) {
      await route.fulfill(json({
        items: [{
          id: "art_shared_data",
          filename: "shared-chart.csv",
          mime: "text/csv",
          size: 42,
          source: "python_sandbox",
          created_at: "2026-07-03T00:00:00Z",
          preview_available: true,
          kind: "sandbox_output",
          origin: "compute",
          trust: "trusted",
          created_by_turn: null,
        }],
      }));
      return;
    }
    // The results viewer reads report-scoped artifacts through representations/{details|preview|raw}
    // （routes/canvas.py get_report_artifact_representation → ArtifactRepresentationDTO）。
    // CSV preview has the same shape as build_file_preview: family=spreadsheet, mode=rows.
    const representation = url.pathname.match(/\/artifacts\/art_shared_data\/representations\/(details|preview|raw)$/)?.[1];
    if (representation) {
      const base = { family: "spreadsheet", representation, revision: `rev_shared_${representation}`, text: null };
      const raw = "note\nreport-scoped shared chart data\n";
      await route.fulfill(json(
        representation === "details"
          ? {
              ...base,
              details: {
                id: "art_shared_data",
                filename: "shared-chart.csv",
                mime: "text/csv",
                size: 42,
                source: "python_sandbox",
                created_at: "2026-07-03T00:00:00Z",
                kind: "sandbox_output",
              },
            }
          : representation === "preview"
            ? {
                ...base,
                mode: "rows",
                columns: ["note"],
                rows: [{ note: "report-scoped shared chart data" }],
                total_rows: 1,
                shown_rows: 1,
                truncated: false,
              }
            : {
                ...base,
                mode: "text",
                text: raw,
                encoding: "utf-8",
                truncated: false,
                total_byte_count: raw.length,
                returned_byte_count: raw.length,
              },
      ));
      return;
    }
    if (url.pathname.endsWith("/artifacts/art_shared_data/preview")) {
      await route.fulfill(json({
        family: "text",
        mode: "text",
        text: "report-scoped shared chart data",
      }));
      return;
    }
    await route.fulfill(json({
      canvas: {
        id: "cv_shared",
        sessionId: "s_shared",
        workspaceId: "ws_shared",
        title: "Shared H1 Report",
        status: "done",
        createdAt: "2026-07-03T00:00:00Z",
        updatedAt: "2026-07-03T00:00:00Z",
      },
      sections: [
        { id: "h1_shared", kind: "h1", text: "Shared H1 Report" },
        { id: "n_shared", kind: "narrative", markdown: "Dogfood shared report body is visible." },
        {
          id: "chart_shared",
          kind: "chart",
          spec: {
            kind: "table",
            title: "Shared Data Chart",
            rows: [
              { month: "2026-01", revenue: 1000 },
              { month: "2026-02", revenue: 1200 },
            ],
            columns: [
              { key: "month", header: "Month", type: "date" },
              { key: "revenue", header: "Revenue", type: "numeric" },
            ],
            data_artifact_id: "art_shared_data",
            row_count: 2,
            limit_rows: 30,
          },
        },
        {
          id: "next_shared",
          kind: "next_list",
          items: [{ title: "Continue analysis", detail: "should not be clickable in shared report" }],
        },
      ],
      sourceRefs: [],
    }));
  });
  await page.route("**/api/v1/memory/snapshot", async (route: Route) => {
    await route.fulfill(json({ items: [] }));
  });
  await page.route("**/api/v1/metrics/event", async (route: Route) => {
    await route.fulfill(json({ ok: true }));
  });
}

test("shared report opens from URL when session detail is forbidden", async ({ page }) => {
  await mockSharedReport(page);
  await page.addInitScript(() => {
    window.localStorage.setItem("bowhead.feature.results_workspace", "1");
  });
  await page.goto("/?session=s_shared");
  await expect(page.getByRole("heading", { name: "Shared H1 Report", level: 1 })).toBeVisible();
  await expect(page.getByText("Dogfood shared report body is visible.")).toBeVisible();
  await expect(page.getByRole("tablist", { name: "报告视图" })).toHaveCount(0);
  await page.getByRole("button", { name: "成果", exact: true }).click();
  let navigator = page.getByRole("dialog", { name: "成果导航", exact: true });
  await expect(navigator).toBeVisible();
  await expect(navigator.getByText("代码", { exact: true })).toHaveCount(0);
  await navigator.locator(".results-nav-item", { hasText: "shared-chart.csv" }).click();
  await expect(page).toHaveURL(/result=art_shared_data/);
  await expect(page.getByText("report-scoped shared chart data")).toBeVisible();
  await expect(page.getByRole("button", { name: "编辑此报告" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "在对话中引用" })).toHaveCount(0);
  await page.getByRole("button", { name: "更多操作", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: "在 Files 中管理" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "反馈此格式" })).toHaveCount(0);

  await page.getByRole("button", { name: "成果", exact: true }).click();
  navigator = page.getByRole("dialog", { name: "成果导航", exact: true });
  await expect(navigator).toBeVisible();
  await navigator.locator(".results-nav-item", { hasText: "Shared H1 Report" }).click();
  await expect(page.getByText("Continue analysis")).toBeVisible();
  await expect(page.getByRole("button", { name: /Continue analysis/ })).toHaveCount(0);
  const reportToolbar = page.locator(".results-report-object-toolbar");
  await expect(reportToolbar).toBeVisible();
  // The report object offers shared viewers no object-level More Actions; charts' own ⋯ (Open Data, Download PNG/CSV, Inspect) are read-only and excluded.
  await expect(reportToolbar.getByRole("button", { name: "更多操作" })).toHaveCount(0);
});
