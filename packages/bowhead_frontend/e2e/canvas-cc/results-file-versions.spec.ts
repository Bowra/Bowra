import { expect, test, type Page, type Route } from "@playwright/test";

import { parseResultsQueryResponse, type ResultItemDTO, type ResultsQueryRequest } from "../../lib/results-api";

// File versions (docs/sprints/file-versions-2026-11-10/tech-design.md R2): Results lists a
// revised file once, keyed by its first version and showing its latest version. Each chat
// card still opens the version it delivered.

const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";
const versions = ["art_rv_1", "art_rv_2", "art_rv_3"];
const sizes: Record<string, number> = { art_rv_1: 1_000, art_rv_2: 2_000, art_rv_3: 3_000 };

const entry: ResultItemDTO = {
  key: "artifact:art_rv_1", object_type: "artifact", title: "report.md",
  primary_group: "deliverables", secondary_group: null, viewer_profile: "text", status: "ready",
  route: { type: "artifact", artifact_id: "art_rv_3", default_mode: "preview" },
  capabilities: ["download", "view_details", "view_raw", "view_history"], relation_roles: ["main_deliverable"],
  execution: null, version: null, lineage: { number: 3, count: 3 }, position: 1,
  display_context: {
    source_role: "sandbox", output_role: "main_deliverable", execution_label: null,
    occurred_at: "2026-11-13T00:03:00Z", duplicate_ordinal: null, duplicate_count: null,
  },
  artifact_metadata: {
    mime: "text/markdown", size: sizes.art_rv_3, source: "python_sandbox", kind: "deliverable",
    origin: "compute", trust: "trusted", created_at: "2026-11-13T00:03:00Z", producer_kind: "python_sandbox",
  },
};

async function respond(route: Route) {
  const request = route.request().postDataJSON() as ResultsQueryRequest;
  const versionKeys = versions.map((id) => `artifact:${id}`);
  if (request.surface === "locate" && !versionKeys.includes(request.anchor_key ?? "")) {
    await route.fulfill({ status: 409, json: { detail: { error: { code: "results_anchor_not_in_view" } } } });
    return;
  }
  await route.fulfill({ json: parseResultsQueryResponse({
    dto_version: 1, capability: { complete_list: true, scale_nav: true }, snapshot_token: "snapshot",
    items: [entry], authorized_total: 1, matched_total: 1, loaded_count: 1,
    group_counts: { deliverables: 1, evidence: 0, data: 0, code: 0, input: 0 },
    page_start: 0, next_cursor: null, previous_cursor: null, has_more: false,
    projection_status: { report_relations: "ready", execution_relations: "unavailable", export_relations: "degraded" },
    projection_revision: "revision", new_results: { count: null, projection_changed: false },
    resolved_current_key: request.current_key && versionKeys.includes(request.current_key) ? entry.key : null,
  }) });
}

async function representation(route: Route) {
  const [, id, mode] = new URL(route.request().url()).pathname.match(/files\/([^/]+)\/representations\/([^/]+)/) ?? [];
  if (!versions.includes(id)) {
    await route.fulfill({ status: 404, json: { detail: { error: { code: "artifact_scope_unavailable" } } } });
    return;
  }
  const details = {
    id, filename: "report.md", mime: "text/markdown", size: sizes[id], source: "python_sandbox",
    created_at: "2026-11-13T00:00:00Z", kind: "deliverable", producer_kind: "python_sandbox",
  };
  await route.fulfill({ json: mode === "details"
    ? { family: "text", representation: "details", revision: `details-${id}`, details }
    : { family: "text", representation: mode, revision: `${mode}-${id}`, mode: "text", text: `content of ${id}` } });
}

async function openSession(page: Page): Promise<string> {
  await page.request.post(`http://127.0.0.1:${apiPort}/api/v1/_test/reset`);
  await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({ json: {
    dto_version: 1, workspace: true, complete_list: true, scale_nav: true, max_page_size: 50,
  } }));
  await page.route("**/api/v1/sessions/*/results/query", respond);
  await page.route("**/api/v1/files/*/representations/*", representation);
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
      started_at: "2026-11-13T00:00:00Z", ended_at: "2026-11-13T00:00:01Z", final_answer: "报告已生成",
      trace_steps: [], summary: null }],
  } }));
  return sessionId;
}

test("each card of a revised file opens the version it delivered", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const sessionId = await openSession(page);
  const viewer = page.locator(".v2-ws-right");

  for (const id of ["art_rv_2", "art_rv_1", "art_rv_3"]) {
    await page.goto(`/?session=${sessionId}&result=${id}&mode=preview`);
    await expect(viewer.getByText(`content of ${id}`)).toBeVisible();
    await expect(viewer.locator(`a[href*="/api/v1/files/${id}"]`).first()).toBeVisible();
    for (const other of versions.filter((value) => value !== id)) {
      await expect(viewer.locator(`a[href*="/api/v1/files/${other}"]`)).toHaveCount(0);
    }
  }
});

test("phone history, exact-version download and All Results return use the compact toolbar", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 786 });
  await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/sessions/*/results/versions", (route) => route.fulfill({ json: {
    versions: versions.map((id, index) => ({
      artifact_id: id, version_no: index + 1, latest: index === 2, filename: "report.md", mime: "text/markdown",
      size: sizes[id], created_at: "2026-11-13T00:03:00Z",
    })),
  } }));
  const sessionId = await openSession(page);
  await page.goto(`/?session=${sessionId}&result=art_rv_2&mode=preview`);
  const toolbar = page.locator(".result-file-toolbar");
  const title = toolbar.getByRole("button", { name: "选择成果，当前文件为report.md", exact: true });
  const more = toolbar.getByRole("button", { name: "更多操作", exact: true });
  const select = async (name: string) => {
    await more.click();
    await page.getByRole("menuitem", { name, exact: true }).click();
  };

  await expect(page.getByText("content of art_rv_2")).toBeVisible();
  await more.click();
  await expect(page.getByRole("menuitem", { name: "下载", exact: true })).toHaveAttribute("href", "/api/v1/files/art_rv_2");
  await page.keyboard.press("Escape");
  await select("版本历史");
  const history = page.getByTestId("file-versions-drawer");
  const rows = history.getByTestId("file-version-row");
  await expect(rows).toHaveCount(3);
  await expect(history.getByRole("link", { name: "下载第 2 版" })).toHaveAttribute("href", "/api/v1/files/art_rv_2");
  await page.keyboard.press("Escape");
  await expect(more).toBeFocused();

  await title.click();
  await page.getByRole("button", { name: /浏览全部成果/ }).click();
  const explorerItem = page.locator('.results-explorer-pane [data-result-key="artifact:art_rv_1"] button');
  await explorerItem.click();
  await expect(page.getByText("content of art_rv_3")).toBeVisible();
  await select("返回全部成果");
  await expect(explorerItem).toBeVisible();
  await expect(explorerItem).toBeFocused();
  await expect(page).toHaveURL(/results=all/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
});
