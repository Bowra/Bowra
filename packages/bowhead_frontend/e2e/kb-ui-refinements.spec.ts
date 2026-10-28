import { expect, test, type Page } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

const files = Array.from({ length: 25 }, (_, i) => ({
  file_id: `kbf_${i}`, filename: `合同-${String(i).padStart(2, "0")}.md`, mime_type: "text/markdown", size_bytes: 100,
  can_delete: true, scope: "personal", scope_id: "u_smoke", workspace_id: "ws_smoke", team_id: "team_smoke", uploaded_by: "u_smoke", uploaded_by_name: "Smoke",
  uploaded_at: "2026-09-28T00:00:00Z", parse_status: "done", chunks_count: 1, version: 1, extracted_rows_status: "na", artifact_id: `art_${i}`,
}));
const question = "这份合同中约定的交付日期是什么？请同时说明延期条件和对应条款。";
const snapshot = {
  matrix: { matrix_id: "mx_test", name: "合同核对", can_edit: true, seq: 1, structure_seq: 1, poll_ms: 5000 },
  columns: ["empty", "null", "failed", "running"].map((id) => ({ column_id: id, name: id, question, type: "question", prompt_version: 1 })),
  rows: [{ file_id: "kbf_0", file_name: files[0].filename, missing: false, file_version: 1 }],
  cells: Object.fromEntries(["null", "failed", "running"].map((id) => [`kbf_0:${id}`, { status: id === "null" ? "done" : id, variants: [], edited: false, verified: false, stale: false, cached: false, seq: 1, fail_reason: id === "failed" ? "llm_invoke_failed" : null }])),
  runs: [0, 1, 2, 3, 4].map((n) => ({ run_id: `run_${n}`, status: n === 4 ? "failed" : "succeeded", scope: { kind: "column", column_id: "null" }, cells_total: 1, cells_done: n === 4 ? 0 : 1, cells_failed: n === 4 ? 1 : 0, eta_s: 0, skipped_edited: 0, initiated_by: "u_smoke", can_cancel: false })),
  run: null,
};

async function setup(page: Page, health = false, guard = false) {
  await page.addInitScript(() => localStorage.setItem("bowhead.space_id", "ws_smoke"));
  await mockAuthedApp(page, { kbHealthObservation: health, kbUseGuard: guard });
  await page.route("**/api/v1/kb/**", async (route) => {
    const u = new URL(route.request().url());
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (u.pathname.endsWith("/snapshot")) return json(snapshot);
    if (u.pathname.endsWith("/preview")) {
      return json({ detail: "kb_service_unavailable" }, 503);
    }
    if (u.pathname.endsWith("/chunks")) return json({ chunks: [], total: 0 });
    if (/\/files\/kbf_\d+$/.test(u.pathname)) return json(files.find((f) => u.pathname.endsWith(f.file_id)));
    if (u.pathname.endsWith("/files")) {
      const query = u.searchParams.get("q") || "";
      const rows = files.filter((f) => f.filename.includes(query));
      const offset = Number(u.searchParams.get("offset") || 0);
      const limit = Number(u.searchParams.get("limit") || 20);
      return json({ files: rows.slice(offset, offset + limit), total: rows.length, has_more: offset + limit < rows.length });
    }
    return json({ items: [], jobs: {}, stats: {} });
  });
}

test("keyboard file selection, filter menu and URL back/refresh restore the actual view", async ({ page }) => {
  await setup(page);
  await page.goto("/knowledge/?tab=files&scope=personal&q=合同&sort=name&order=asc&page=2");
  const row = page.locator('[data-file-id="kbf_20"]');
  await expect(row).toBeVisible();
  await expect(page.getByRole("searchbox", { name: "搜索知识库文件" })).toHaveValue("合同");
  const select = row.getByRole("checkbox");
  await select.focus(); await page.keyboard.press("Space");
  await expect(select).toHaveAttribute("aria-checked", "true");
  await expect(page.getByTestId("kb-bulk-bar")).toBeVisible();
  const open = row.getByRole("button", { name: files[20].filename, exact: true });
  await open.focus(); await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/file_id=kbf_20/);
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(open).toBeFocused();
  await expect(page).toHaveURL(/page=2/);
  await page.reload();
  await expect(row).toBeVisible();
  const filter = page.getByRole("button", { name: /^状态/ });
  await filter.focus(); await page.keyboard.press("Enter");
  const option = page.getByRole("menuitemcheckbox", { name: "已完成", exact: true });
  await option.focus(); await page.keyboard.press("Space");
  await expect(option).toHaveAttribute("aria-checked", "true");
  await expect(page).toHaveURL(/status=done/);
  await page.keyboard.press("Escape");
  await expect(filter).toBeFocused();
  await page.goBack();
  await expect(row).toBeVisible();
  await expect(page).toHaveURL(/page=2/);
});

test("preview errors stay compact and retry without closing the underlying drawer", async ({ page }) => {
  await setup(page);
  await page.goto("/knowledge/?file_id=kbf_0&scope=personal");
  await page.getByTestId("kb-preview-button").click();
  const modal = page.getByTestId("kb-preview-modal");
  await expect(modal.getByTestId("kb-preview-error")).toBeVisible();
  expect((await modal.boundingBox())!.height).toBeLessThan(400);
  await page.route("**/api/v1/kb/files/kbf_0/preview", (route) => route.fulfill({ contentType: "text/plain", body: "已恢复的原文" }));
  await modal.getByRole("button", { name: "重试预览" }).click();
  await expect(modal).toContainText("已恢复的原文");
  await page.keyboard.press("Escape");
  await expect(modal).toHaveCount(0);
  await expect(page.getByTestId("kb-preview-button")).toBeFocused();
});

for (const width of [1440, 320]) {
  test(`source check is on demand and preserves unknown binding at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await setup(page, true);
    let checks = 0;
    await page.route("**/api/v1/kb/files/kbf_0/health", (route) => {
      checks++;
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({
        file_id: "kbf_0", file_version: 1, checked_at: "2026-09-28T00:00:00Z", source_health: "temporarily_unavailable", source_reason: "probe_failed",
        index: { state: "unknown", reason: "legacy_chunks_readable", observed_chunks: 1, count_limited: false },
        binding_state: "unverified", evidence_state: "unknown", eligibility_enforced: false,
      }) });
    });
    await page.goto("/knowledge/?file_id=kbf_0&scope=personal");
    const health = page.getByTestId("kb-file-health");
    await expect(health).toBeVisible();
    expect(checks).toBe(0);
    await health.getByRole("button", { name: "检查来源", exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(health).toContainText("尚未验证索引是否对应当前原件");
    await expect(health).toContainText("暂时无法核对原件");
    expect(checks).toBe(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`health-${width}.png`), fullPage: true, animations: "disabled" });
  });
}

for (const width of [1440, 390, 320]) {
  test(`matrix statuses and retained failure summary at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await setup(page);
    await page.goto("/knowledge/?tab=matrix&view=mx_test");
    const summary = page.getByTestId("kb-matrix-task-summary");
    await expect(summary).toContainText("运行记录");
    await expect(summary).toContainText("1 个任务需要处理");
    await expect(page.getByText("未运行", { exact: true })).toBeVisible();
    const empty = page.getByText("未提取到值", { exact: true });
    await expect(empty).toBeVisible();
    if (width === 1440) {
      const title = page.getByRole("button", { name: "null", exact: true });
      await title.focus();
      await expect(page.locator(".mx-question-text")).toHaveText(question);
      await empty.click();
      await expect(page.getByTestId("kb-matrix-panel")).toContainText(question);
      await expect(page.getByTestId("kb-matrix-panel")).toContainText("请核对原文");
      await page.getByTestId("kb-matrix-panel").locator(".mx-back").click();
    } else {
      await empty.click();
      // The bottom sheet's title bar names the column; the full question stays at the top of the body.
      await expect(page.getByRole("dialog")).toHaveAccessibleName("null");
      await expect(page.getByRole("dialog")).toContainText(question);
      await page.keyboard.press("Escape");
      await expect(page.getByRole("dialog")).toHaveCount(0);
    }
    await summary.click();
    await expect(page.getByRole("button", { name: "查看失败" })).toBeVisible();
    await summary.click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`matrix-${width}.png`), fullPage: true });
  });
}

test("English dark mode keeps task states readable in a 720px effective viewport", async ({ page }, testInfo) => {
  await page.context().addCookies([{ name: "NEXT_LOCALE", value: "en-US", domain: "localhost", path: "/" }]);
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  // A 1440px browser at 200% uses a 720 CSS-pixel layout viewport.
  // Native browser zoom remains a deployment smoke check; CSS zoom does not exercise media queries.
  await page.setViewportSize({ width: 720, height: 500 });
  await setup(page);
  await page.goto("/knowledge/?tab=matrix&view=mx_test");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByTestId("kb-matrix-task-summary")).toContainText("Run history");
  await expect(page.getByText("No value extracted", { exact: true })).toBeVisible();
  await page.getByText("No value extracted", { exact: true }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("matrix-en-dark-scale.png"), fullPage: true });
});

for (const width of [1440, 320]) {
  test(`source recovery retains context and explains different bytes at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await setup(page, true, true);
    await page.route("**/api/v1/kb/files/kbf_0", route => route.fulfill({ json: { ...files[0], can_delete: true } }));
    let checks = 0;
    await page.route("**/api/v1/kb/files/kbf_0/verify", route => {
      checks++;
      return route.fulfill({ json: {
        file_id: "kbf_0", file_version: 1, checked_at: "2026-09-28T00:00:00Z", source_health: "confirmed_absent", source_reason: "source_missing",
        index: { state: "unknown", reason: "legacy_chunks_readable", observed_chunks: 1, count_limited: false },
        binding_state: "unverified", evidence_state: "unknown", eligibility_enforced: true, use_state: "paused", primary_action: "restore",
      } });
    });
    await page.route("**/api/v1/kb/files/kbf_0/restore-source", route => route.fulfill({ json: { outcome: "different_content_requires_new_file" } }));
    await page.goto("/knowledge/?file_id=kbf_0&scope=personal");
    const health = page.getByTestId("kb-file-health");
    await health.getByRole("button", { name: "检查来源", exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(health.getByRole("button", { name: "恢复原件" })).toBeVisible();
    await health.getByLabel("恢复原件").setInputFiles({ name: "different.md", mimeType: "text/markdown", buffer: Buffer.from("Different content") });
    await expect(health).toContainText("所选文件内容与原件不同");
    await expect(page).toHaveURL(/file_id=kbf_0/);
    expect(checks).toBe(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`recovery-${width}.png`), fullPage: true, animations: "disabled" });
  });
}

for (const width of [1440, 320]) {
  test(`current source status replaces misleading ready label at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await setup(page, true, true);
    const paused = { ...files[0], source_health: { state: "paused", reason: "source_missing", checked_at: "2026-09-28T00:00:00Z" } };
    await page.route("**/api/v1/kb/files?**", route => route.fulfill({ json: { files: [paused], total: 1 } }));
    await page.route("**/api/v1/kb/files/kbf_0", route => route.fulfill({ json: paused }));
    await page.goto("/knowledge/?scope=personal");
    const row = page.getByTestId("kb-files-row").first();
    await expect(row.getByTestId("kb-source-status")).toHaveText("已暂停用于新任务");
    await row.click();
    await expect(page.getByTestId("kb-file-drawer").getByTestId("kb-source-status")).toHaveText("已暂停用于新任务");
    await expect(page.getByTestId("kb-file-drawer").getByRole("button", { name: "在对话中使用" })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`source-status-${width}.png`), animations: "disabled" });
  });
}

test("batch rebuilding previews cost and keeps one progress area", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await setup(page, true, true);
  let submissions = 0;
  await page.route("**/api/v1/kb/maintenance/rebuilds", route => {
    if (route.request().method() === "GET") return route.fulfill({ json: { job_id: submissions ? "kbj_test" : null } });
    const body = route.request().postDataJSON();
    if (body.apply) { expect(body.accept_model_cost).toBe(true); submissions++; }
    return route.fulfill({ json: { files: 1, size_bytes: 100, ...(body.apply ? { job_id: "kbj_test", state: "running" } : {}) } });
  });
  await page.route("**/api/v1/kb/maintenance/rebuilds/kbj_test", route => route.fulfill({ json: { job_id: "kbj_test", state: "completed", total: 1, processed: 1, failed: 0, changed: 0 } }));
  await page.goto("/knowledge/?scope=personal");
  await page.getByTestId("kb-files-row").first().getByRole("checkbox").click();
  const panel = page.getByTestId("kb-rebuild-batch");
  await panel.getByRole("button", { name: "批量重新解析…" }).click();
  await expect(panel).toContainText("重新解析可能产生模型用量");
  expect(submissions).toBe(0);
  await panel.getByRole("button", { name: "确认并开始" }).click();
  await expect(panel.getByRole("status")).toContainText("处理结束 · 1/1");
  expect(submissions).toBe(1);
  await expect(panel.getByRole("status")).toHaveCount(1);
  await page.reload();
  await expect(page.getByTestId("kb-rebuild-batch").getByRole("status")).toContainText("处理结束 · 1/1");
  expect(submissions).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("batch-rebuild.png"), animations: "disabled" });
});

test("space switch clears the previous matrix before denied response", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/auth/me/workspaces", (route) => route.fulfill({ json: {
    items: [
      { id: "ws_smoke", workspace_id: "ws_smoke", name: "Smoke workspace", role: "owner" },
      { id: "ws_other", workspace_id: "ws_other", name: "Other workspace", role: "owner" },
    ], active_id: "ws_smoke",
  } }));
  let releaseDenied!: () => void;
  const deniedGate = new Promise<void>((resolve) => { releaseDenied = resolve; });
  await page.route("**/api/v1/kb/matrices/mx_test/snapshot**", async (route) => {
    if (route.request().headers()["x-space-id"] === "ws_other") {
      await deniedGate;
      return route.fulfill({ status: 404, json: { detail: { error: { code: "matrix_not_found" } } } });
    }
    return route.fulfill({ json: snapshot });
  });
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/knowledge/?tab=matrix&view=mx_test");
  await expect(page.locator(".mx-title")).toHaveText("合同核对");
  await page.getByRole("button", { name: "切换空间，当前：Smoke workspace", exact: true }).click();
  await page.getByRole("menuitemradio", { name: /Other workspace/ }).click();
  try {
    await expect(page.locator(".mx-title")).not.toHaveText("合同核对");
    await expect(page.getByText(files[0].filename, { exact: true })).toHaveCount(0);
  } finally {
    releaseDenied();
  }
  await expect(page.getByTestId("kb-matrix-unavailable")).toBeVisible();
  expect(errors).toEqual([]);
  await page.getByRole("button", { name: "切换空间，当前：Other workspace", exact: true }).click();
  await page.getByRole("menuitemradio", { name: /Smoke workspace/ }).click();
  await expect(page.locator(".mx-title")).toHaveText("合同核对");
  expect(errors).toEqual([]);
});
