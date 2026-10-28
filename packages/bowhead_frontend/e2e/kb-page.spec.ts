/**
 * /knowledge Files tab e2e — kb-management M1–M5 (mocked backend via page.route).
 *
 * Rewritten for the M1–M5 redesign (KbFilesManager). Covers:
 *  M1 — list renders + scope switch reloads + status filter + pagination
 *  M2 — multi-select → bulk bar + open scope modal from bulk
 *  M4 — parse_failed row offers a retry icon button next to the pill
 *  drawer opens on row click; upload modal opens via Add knowledge button
 *  mobile (≤640px) — row list keeps upload and status on screen
 *
 * Desktop only (chromium project). Mobile M1–M5 is a separate handoff.
 */
import { type Page, type Route, expect, test } from "@playwright/test";
import { probeLayer } from "./_layer-probe";

interface MockFile {
  file_id: string; filename: string; mime_type: string; size_bytes: number;
  scope: "personal" | "space" | "team"; scope_id: string; workspace_id: string; team_id: string;
  uploaded_by: string; uploaded_by_name: string; uploaded_at: string;
  parse_status: string; parse_progress_pct: number | null; parse_layer: null;
  parse_error: string | null; parse_duration_ms: null; chunks_count: number;
  cite_count: number; last_cited_at: null; use_llm_marker: boolean;
  doc_type_id: string | null; auto_classified_doc_type_id: string | null;
  extracted_rows_status: string; summary_json: null; version: number; soft_deleted_at: null;
  artifact_id: string; can_delete: boolean; can_change_scope: boolean;
}

function mk(i: number, scope: MockFile["scope"], status: string, over: Partial<MockFile> = {}): MockFile {
  return {
    file_id: `f_${scope}_${i}`, filename: `${scope}-doc-${i}.pdf`, mime_type: "application/pdf",
    size_bytes: 100_000 + i * 5000, scope, scope_id: scope === "personal" ? "u_alice" : scope === "team" ? "team_test" : "ws_test",
    workspace_id: "ws_test", team_id: "team_test", uploaded_by: "u_alice", uploaded_by_name: "Alice",
    uploaded_at: `2026-05-${String((i % 27) + 1).padStart(2, "0")}T10:00:00Z`, parse_status: status,
    parse_progress_pct: status === "parsing" ? 50 : null, parse_layer: null,
    parse_error: status === "failed" ? "OCR 超时 (error: ocr_timeout)" : null, parse_duration_ms: null,
    chunks_count: status === "done" ? 40 : 0, cite_count: 0, last_cited_at: null, use_llm_marker: false,
    doc_type_id: null, auto_classified_doc_type_id: null, extracted_rows_status: "na",
    summary_json: null, version: 1, soft_deleted_at: null, artifact_id: `art_${i}`,
    can_delete: true, can_change_scope: true, ...over,
  };
}

// 25 space (2 failed) → multi-page; 6 personal; 4 team.
const FILES: MockFile[] = [
  ...Array.from({ length: 25 }, (_, i) => mk(i, "space", i === 2 || i === 9 ? "failed" : i % 5 === 0 ? "parsing" : "done")),
  ...Array.from({ length: 6 }, (_, i) => mk(i, "personal", "done")),
  ...Array.from({ length: 4 }, (_, i) => mk(i, "team", "done")),
];

async function setupMocks(page: Page, isTeamAdmin = true): Promise<void> {
  await page.route("**/api/v1/team/kb-ingestion-readiness", (r: Route) => r.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify({ state: "ready" }),
  }));
  await page.route("**/api/v1/auth/me", (r: Route) => r.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      id: "u_alice", email: "a@test.com", display_name: "Alice", avatar_url: null,
      team_id: "team_test", tier: "ultra", role: "member", team_role: isTeamAdmin ? "team_admin" : "member",
      status: "active", email_verified: true, credit_balance: 30000, credit_quota: 30000,
      locale: "zh-CN", timezone: "Asia/Shanghai", is_team_admin: isTeamAdmin, workspace_count: 1, needs_onboarding: false,
    }),
  }));
  await page.route("**/api/v1/auth/me/workspaces**", (r: Route) => r.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ items: [{ workspace_id: "ws_test", name: "测试空间", description: "", role: "admin" }], active_id: "ws_test" }),
  }));
  await page.route("**/api/v1/kb/doc_types**", (r: Route) => r.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ items: [{ doc_type_id: "dt_audit", workspace_id: "ws_test", name: "审计报告", fields: [{}, {}], cardinality: "one" }], stats: {} }),
  }));
  await page.route("**/api/v1/kb/files/_batch/extract_rows_status**", (r: Route) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ jobs: {} }) }));
  await page.route("**/api/v1/kb/files/*/reparse", (r: Route) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...FILES[0], parse_status: "queued", parse_error: null }) }));
  await page.route(/\/api\/v1\/kb\/files\/bulk_(delete|scope|doc_type)$/, async (r: Route) => {
    const body = JSON.parse(r.request().postData() || "{}");
    const ids: string[] = body.file_ids || [];
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ results: ids.map((id) => ({ file_id: id, ok: true, error: null })), succeeded: ids.length, failed: 0 }) });
  });
  // list (paginated/filtered)  — must come AFTER the more specific routes above.
  await page.route(/\/api\/v1\/kb\/files\?/, (r: Route) => {
    const u = new URL(r.request().url());
    const scope = u.searchParams.get("scope") ?? "personal";
    const offset = Number(u.searchParams.get("offset") ?? 0);
    const limit = Number(u.searchParams.get("limit") ?? 20);
    const status = u.searchParams.get("status");
    const q = u.searchParams.get("q");
    let rows = FILES.filter((f) => f.scope === scope);
    if (status) { const set = new Set(status.split(",")); rows = rows.filter((f) => set.has(f.parse_status)); }
    if (q) rows = rows.filter((f) => f.filename.toLowerCase().includes(q.toLowerCase()));
    const total = rows.length;
    const slice = rows.slice(offset, offset + limit);
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ files: slice, total, has_more: offset + slice.length < total }) });
  });
  // single file (drawer) — generic, last.
  await page.route(/\/api\/v1\/kb\/files\/[^/?]+$/, (r: Route) => {
    const id = r.request().url().split("/").pop()!.split("?")[0];
    const f = FILES.find((x) => x.file_id === id) ?? FILES[0];
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(f) });
  });
  await page.route("**/api/v1/kb/files/*/chunks", (r: Route) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ chunks: [], total: 0 }) }));
}

/** The toolbar "范围" filter picks the listed scope (the default is the current space). */
async function selectScope(page: Page, id: "personal" | "space" | "team"): Promise<void> {
  await page.getByTestId("kb-scope-filter").click();
  await page.getByTestId(`kb-scope-filter-${id}`).click();
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("bowhead.space_id", "ws_test"));
});

test("M1: list renders rows, scope counts, and a parse_failed pill", async ({ page }) => {
  await setupMocks(page);
  await page.goto("/knowledge");
  await expect(page.getByTestId("kb-files-table")).toBeVisible();
  // The current space is the default scope: 25 files, first page of 20.
  await expect(page.getByTestId("kb-scope-filter")).toContainText("空间 · 测试空间");
  await expect(page.getByTestId("kb-files-row")).toHaveCount(20);
  await expect(page.locator('button[data-state="parse_failed"]').first()).toBeVisible();
  // The scope filter lists every scope with its count.
  await page.getByTestId("kb-scope-filter").click();
  await expect(page.getByTestId("kb-scope-filter-personal")).toContainText("6");
  await expect(page.getByTestId("kb-scope-filter-space")).toContainText("25");
  await expect(page.getByTestId("kb-scope-filter-team")).toContainText("4");
});

test("M1: scope switch reloads + pagination on 25 space files", async ({ page }) => {
  await setupMocks(page);
  await page.goto("/knowledge");
  await expect(page.getByTestId("kb-files-row")).toHaveCount(20); // page size
  await expect(page.locator(".kb-pagination")).toContainText("25");
  await page.getByRole("button", { name: /^下一页/ }).click();
  await expect(page.getByTestId("kb-files-row")).toHaveCount(5); // remainder
  await selectScope(page, "personal");
  await expect(page.getByTestId("kb-files-row")).toHaveCount(6);
});

test("M1: status filter narrows to failed", async ({ page }) => {
  await setupMocks(page);
  await page.goto("/knowledge");
  await page.getByRole("button", { name: /^状态/ }).click();
  // The row pills also read "失败" (aa537b06b); pick the filter option itself.
  await page.getByRole("menuitemcheckbox", { name: "失败", exact: true }).click();
  await expect(page.getByTestId("kb-files-row")).toHaveCount(2); // 2 failed in space
});

test("M2: select rows → bulk bar → open scope modal", async ({ page }) => {
  await setupMocks(page);
  await page.goto("/knowledge");
  await page.locator(".kb-th .kb-cb").click(); // select all on page
  await expect(page.getByTestId("kb-bulk-bar")).toBeVisible();
  await page.locator('.kb-bulkbar button:has-text("更改可见范围")').click();
  await expect(page.locator(".kb-modal").getByRole("heading", { name: /更改可见范围/ })).toBeVisible();
  await expect(page.locator(".kb-scope-card")).toHaveCount(3);
});

test("M2: bulk delete → confirm → optimistic remove + undo toast", async ({ page }) => {
  await setupMocks(page);
  await page.goto("/knowledge");
  await expect(page.getByTestId("kb-files-row")).toHaveCount(20);
  await page.locator(".kb-th .kb-cb").click(); // select page
  await page.locator('.kb-bulkbar button:has-text("删除")').click();
  await expect(page.locator(".kb-modal").getByRole("heading", { name: /删除 .* 个文件/ })).toBeVisible();
  await page.locator('.kb-modal button:has-text("删除")').click();
  // optimistic removal + undo toast (deferred-delete window)
  await expect(page.getByTestId("kb-files-row")).toHaveCount(0);
  const undoToast = page.getByTestId("toast-success").filter({ hasText: "已删除" });
  await expect(undoToast).toBeVisible();
  await undoToast.getByRole("button", { name: "撤销" }).click();
  await expect(page.getByTestId("kb-files-row")).toHaveCount(20); // restored
});

test("M4: a parse_failed row offers a retry button next to the pill", async ({ page }) => {
  await setupMocks(page);
  await page.goto("/knowledge");
  const retry = page.getByTestId("kb-pill-retry").first();
  await expect(retry).toBeVisible();
  await expect(retry).toHaveAccessibleName(/^重试：.+/);
  await retry.click(); // reparse — must not crash
  await expect(page.getByTestId("kb-files-table")).toBeVisible();
});

test("drawer opens on row click; upload modal opens via Add knowledge", async ({ page }) => {
  await setupMocks(page);
  await page.goto("/knowledge");
  await page.getByTestId("kb-files-row").first().click();
  await expect(page.getByTestId("kb-file-drawer")).toBeVisible();
  await page.getByTestId("kb-file-drawer").getByRole("button", { name: "关闭文件详情" }).click();
  await expect(page.getByTestId("kb-file-drawer")).not.toBeVisible();
  await page.getByRole("button", { name: "添加知识", exact: true }).click();
  await expect(page.getByTestId("kb-upload-modal")).toBeVisible();
});

test("mobile: keeps primary controls and row status inside the viewport", async ({
  page,
}) => {
  await setupMocks(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/knowledge");

  const upload = page.getByRole("button", { name: "添加知识" }).last();
  await expect(upload).toBeVisible();
  const uploadBox = await upload.boundingBox();
  expect(uploadBox).not.toBeNull();
  expect(uploadBox!.x).toBeGreaterThanOrEqual(0);
  expect(uploadBox!.x + uploadBox!.width).toBeLessThanOrEqual(390);

  // Narrow screens use a row list instead of a horizontally scrolling table, keeping status onscreen (PRD FR2.7).
  await expect(page.getByTestId("kb-files-list")).toBeVisible();
  await expect(page.getByTestId("kb-files-table")).toHaveCount(0);
  const status = page.getByTestId("kb-files-row").first().locator(".st");
  await expect(status).toBeVisible();
  const statusBox = await status.boundingBox();
  expect(statusBox).not.toBeNull();
  expect(statusBox!.x + statusBox!.width).toBeLessThanOrEqual(390);
  const documentWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(documentWidth).toBeLessThanOrEqual(390);
});

test("row menu on the last row is fully visible (not clipped by the table)", async ({ page }) => {
  // Layer audit (design document “图层”): .kb-tablewrap overflow:hidden previously clipped the inline overlay down to a single edge.
  await setupMocks(page);
  await page.setViewportSize({ width: 1280, height: 1400 });
  await page.goto("/knowledge");
  await expect(page.getByTestId("kb-files-row")).toHaveCount(20);
  const last = page.getByTestId("kb-files-row").last();
  await last.hover();
  await last.getByRole("button", { name: "更多", exact: true }).click();
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  await page.waitForTimeout(200);
  const probe = await probeLayer(menu);
  expect(probe.covered).toEqual([]);
  expect(probe.clipped).toBe(0);
});
