import { expect, test, type Page, type Route } from "@playwright/test";

import { parseResultsQueryResponse, type ResultItemDTO, type ResultsQueryRequest, type ResultsQueryResponse } from "../../lib/results-api";

// The mobile Results tab announces unseen output as `成果，有新内容`.
const RESULTS_TAB = /^成果(，有新内容)?$/;

const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";

function artifact(id: string, secondary: "data" | "input", position: number): ResultItemDTO {
  return {
    key: `artifact:${id}`, object_type: "artifact", title: `${id}.csv`,
    primary_group: "evidence", secondary_group: secondary, viewer_profile: "table", status: "ready",
    route: { type: "artifact", artifact_id: id, default_mode: "preview" },
    capabilities: ["download", "view_details"], relation_roles: [], execution: null, version: null, position,
    display_context: {
      source_role: secondary === "input" ? "user_upload" : "data_query",
      output_role: null, execution_label: null, occurred_at: null, duplicate_ordinal: null, duplicate_count: null,
    },
    artifact_metadata: {
      mime: "text/csv", size: 10, source: "bi_fetch_rows", kind: "tool_data", origin: "query", trust: "trusted",
      created_at: "2026-09-08T00:00:00Z", producer_kind: null,
    },
  };
}

function fixtures(sessionId: string): ResultItemDTO[] {
  return [{
    ...artifact("report", "data", 1), key: `report:cv_${sessionId}`, object_type: "report",
    title: "E2E 报告", primary_group: "deliverables", secondary_group: null, viewer_profile: "report",
    route: { type: "report" }, capabilities: ["read_report"], artifact_metadata: null,
  }, artifact("analysis-data", "data", 2), artifact("uploaded-input", "input", 3)];
}

async function respond(route: Route, omitReport = false, includeCode = false) {
  const request = route.request().postDataJSON() as ResultsQueryRequest;
  const sessionId = new URL(route.request().url()).pathname.split("/")[4];
  const code: ResultItemDTO = { ...fixtures(sessionId)[0], key: `code:${sessionId}`, object_type: "code", title: "Code and runs",
    primary_group: "evidence", secondary_group: "code", viewer_profile: "code", route: { type: "code" }, capabilities: [] };
  const all = [...fixtures(sessionId), ...(includeCode ? [code] : [])].filter((item) => !omitReport || item.object_type !== "report");
  const items = all.filter((item) => (!request.query || item.title.includes(request.query))
    && (!request.filters || request.filters.primary === "all" || request.filters.primary === item.primary_group)
    && (!request.filters || request.filters.secondary === "all" || request.filters.secondary === item.secondary_group));
  if (request.surface === "locate" && !items.some((item) => item.key === request.anchor_key)) {
    await route.fulfill({ status: 409, json: { detail: { error: { code: "results_anchor_not_in_view" } } } });
    return;
  }
  const payload: ResultsQueryResponse = {
    dto_version: 1, capability: { complete_list: true, scale_nav: true }, snapshot_token: JSON.stringify(request.filters ?? {}),
    items, authorized_total: all.length, matched_total: items.length, loaded_count: items.length,
    group_counts: {
      deliverables: items.filter((item) => item.primary_group === "deliverables").length,
      evidence: items.filter((item) => item.primary_group === "evidence").length,
      data: items.filter((item) => item.secondary_group === "data").length,
      code: items.filter((item) => item.secondary_group === "code").length,
      input: items.filter((item) => item.secondary_group === "input").length,
    },
    page_start: 0, next_cursor: null, previous_cursor: null, has_more: false,
    projection_status: { report_relations: "ready", execution_relations: "unavailable", export_relations: "degraded" },
    projection_revision: "revision", new_results: { count: null, projection_changed: false }, resolved_current_key: null,
  };
  await route.fulfill({ json: parseResultsQueryResponse(payload) });
}

async function openExplorer(page: Page, handler: (route: Route) => Promise<void>, openList = true) {
  await page.request.post(`http://127.0.0.1:${apiPort}/api/v1/_test/reset`);
  await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
    dto_version: 1, workspace: true, complete_list: true, scale_nav: true, max_page_size: 50,
  } }));
  await page.route("**/api/v1/sessions/*/results/query", handler);
  await page.goto("/");
  const composer = page.locator("textarea").first();
  await expect(composer).toBeVisible();
  await composer.fill("resultsworkspace");
  const sent = page.waitForResponse((response) => response.url().endsWith("/api/v1/chat/send"));
  await composer.press("Enter");
  const { turn_id: turnId, session_id: sessionId } = await (await sent).json();
  await expect(page.locator('[data-presentation="results-reading"]')).toHaveCount(1);
  // The mock stores Canvas, but its session-detail fixture otherwise has no turns.
  const detail = await (await page.request.get(`/api/v1/sessions/${sessionId}`)).json();
  await page.route(`**/api/v1/sessions/${sessionId}`, (route) => route.fulfill({ json: {
    ...detail, turns: [{ id: turnId, seq: 1, user_text: "resultsworkspace", intent: "analysis", status: "completed",
      started_at: "2026-09-08T00:00:00Z", ended_at: "2026-09-08T00:00:01Z", final_answer: "报告已生成",
      trace_steps: [], summary: null }],
  } }));
  if (!openList) return page.locator(".results-explorer-pane");
  if (page.viewportSize()!.width < 768) await page.getByRole("tab", { name: RESULTS_TAB }).click();
  await page.getByRole("button", { name: page.viewportSize()!.width <= 640 ? "返回成果列表" : "成果", exact: true }).click();
  await page.getByRole("button", { name: /浏览全部成果/ }).click();
  await expect(page).toHaveURL(/results=all/);
  const pane = page.locator(".results-explorer-pane");
  await expect(pane.locator(".results-explorer-scroll")).toHaveAttribute("aria-busy", "false");
  return pane;
}

for (const width of [412, 1280, 1440]) {
  test(`filter pending → failure → retry commits one coherent view (${width}px)`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 800 });
    let delayed: Route | undefined;
    let failInput = true;
    const pane = await openExplorer(page, async (route) => {
      const request = route.request().postDataJSON() as ResultsQueryRequest;
      if (failInput && request.surface === "explorer" && request.filters?.secondary === "input") delayed = route;
      else await respond(route);
    });
    const compact = pane.locator(".results-explorer-compact-filters summary");
    if (await compact.isVisible()) await compact.click();
    const input = pane.getByRole("radio", { name: "输入文件", exact: true });
    await input.click();
    await expect.poll(() => Boolean(delayed)).toBe(true);
    await expect(pane).toContainText("正在更新为：分析依据 · 输入文件");
    await expect(pane).toContainText("仍显示：全部");
    await expect(input).toHaveAttribute("aria-checked", "false");
    await expect(pane.getByRole("heading", { name: /交付内容/ })).toBeVisible();
    await expect(pane).toContainText("共 3 个成果 · 已加载 3 个");
    await page.screenshot({ path: testInfo.outputPath(`filter-pending-${width}.png`) });

    await delayed!.fulfill({ status: 503, json: { detail: { error: { code: "results_read_model_unavailable" } } } });
    await expect(pane).toContainText("未能更新为：分析依据 · 输入文件");
    await expect(input).toHaveAttribute("aria-checked", "false");
    await expect(pane).toContainText("共 3 个成果 · 已加载 3 个");
    failInput = false;
    await pane.getByRole("button", { name: "重试", exact: true }).click();
    await expect(input).toHaveAttribute("aria-checked", "true");
    await expect(pane).toContainText("1 个匹配 · 已加载 1 个");
    await expect(pane.getByRole("button", { name: /uploaded-input.csv/ })).toBeVisible();
    await expect(pane.getByRole("heading", { name: /交付内容/ })).toHaveCount(0);
    await expect(pane.locator(".results-explorer-query-status")).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`filter-committed-${width}.png`) });

    await page.reload();
    if (width < 768) await page.getByRole("tab", { name: RESULTS_TAB }).click();
    // Wait for the restored list before probing the compact filter toggle, which renders with it.
    await expect(pane.getByRole("button", { name: /uploaded-input.csv/ })).toBeVisible();
    const reloadedCompact = pane.locator(".results-explorer-compact-filters summary");
    if (await reloadedCompact.isVisible()) await reloadedCompact.click();
    await expect(input).toHaveAttribute("aria-checked", "true");
    await expect(pane).toContainText("1 个匹配 · 已加载 1 个");
  });
}

test("unfiltered locate failure offers list positioning recovery without claiming a filter mismatch", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  let omitReport = false;
  let locateCalls = 0;
  const pane = await openExplorer(page, (route) => {
    if (route.request().postDataJSON().surface === "locate") locateCalls += 1;
    return respond(route, omitReport);
  });
  await pane.locator('[data-result-key^="report:"] button').click();
  omitReport = true;
  await page.reload();
  await page.getByRole("button", { name: "放大报告（隐藏对话）" }).click();
  await expect(pane).toBeVisible();
  await expect(pane).toContainText("未能在成果列表中定位这一行");
  await expect(pane.getByRole("button", { name: "清除条件并显示" })).toHaveCount(0);
  await expect(pane.getByRole("button", { name: "重试列表定位", exact: true })).toBeVisible();
  const beforeRetry = locateCalls;
  await pane.getByRole("button", { name: "重试列表定位", exact: true }).click();
  await expect.poll(() => locateCalls).toBe(beforeRetry + 1);
  await expect(pane).toContainText("未能在成果列表中定位这一行");
});

test("report and code links locate concrete object keys after replay", async ({ page }) => {
  const anchors: string[] = [];
  await openExplorer(page, async (route) => {
    const request = route.request().postDataJSON() as ResultsQueryRequest;
    if (request.surface === "locate") {
      anchors.push(request.anchor_key!);
      if (request.anchor_key === "report" || request.anchor_key === "code") {
        await route.fulfill({ status: 409, json: { detail: { code: "results_anchor_not_in_view" } } });
        return;
      }
    }
    await respond(route, false, true);
  }, false);
  for (const kind of ["report", "code"]) {
    anchors.length = 0;
    await page.goto(`/?session=s_e2e&results=all&result=${kind}`);
    const expectedKey = kind === "report" ? "report:cv_s_e2e" : "code:s_e2e";
    await expect.poll(() => anchors.includes(expectedKey)).toBe(true);
    expect(anchors).not.toContain(kind);
    await expect(page.locator(`[data-result-key="${expectedKey}"]`)).toHaveCount(1);
    await expect(page.locator(".results-explorer-page-error")).toHaveCount(0);
  }
});
