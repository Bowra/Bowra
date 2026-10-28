import { type Locator, type Page, type Route, expect, test } from "@playwright/test";

/**
 * file-management-v2 P1 — three file-page tabs' critical paths + accessibility.
 *
 * Old spec covered <tr data-file-id> table/raw confirm(); v2 uses
 * grid rows + in-app overlays, hence whole rewrite. Assertions focus three hard product rules:
 *   1. Assets default, run data excluded (PRD D1).
 *   2. KB files **no delete button**, navigation only (D2); “知识库” column shows status/deep link (FR7).
 *   3. Delete uses in-app overlay + undo toast, not browser confirm.
 */

const ASSET_UPLOAD = {
  file_id: "art_upload",
  filename: "Q1-经营复盘.pptx",
  mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  mime_family: "presentation",
  size: 1_200_000,
  source: "upload",
  origin: "upload",
  kind: "user_upload",
  trust: "trusted",
  in_kb: false,
  kb_promotable: true,
  created_at: "2026-07-18T10:20:00Z",
  last_used_at: "2026-07-20T14:00:00Z",
  deleted_at: null,
  category: "report",
  category_source: "llm",
  usage_count: 3,
  session_id: null,
  session_title: null,
  has_canvas_ref: false,
  retention_hint: null,
  url: "/api/v1/files/art_upload",
};

const ASSET_KB = {
  ...ASSET_UPLOAD,
  file_id: "art_kb",
  filename: "产品价格政策手册.pdf",
  mime: "application/pdf",
  mime_family: "pdf",
  in_kb: true,
  kb: {
    file_id: "kbf_kb",
    scope: "space",
    workspace_id: "ws_test",
    parse_status: "done",
    parse_progress_pct: null,
    parse_stuck: false,
    parse_error_code: null,
  },
  retention_hint: "long_term",
  category: "metric_definition",
  url: "/api/v1/files/art_kb",
};

const ASSET_EXPORT = {
  ...ASSET_UPLOAD,
  file_id: "art_export",
  filename: "华东区销售明细.xlsx",
  mime_family: "spreadsheet",
  kind: "canvas_export",
  in_kb: false,
  has_canvas_ref: true,
  url: "/api/v1/files/art_export",
};

const ASSET_JSON = {
  ...ASSET_UPLOAD,
  file_id: "art_json",
  filename: "analysis-result.json",
  mime: "application/json",
  mime_family: "other",
  kind: "deliverable",
  kb_promotable: false,
  url: "/api/v1/files/art_json",
};

const RUN_ROW = {
  ...ASSET_UPLOAD,
  file_id: "art_rows",
  filename: "rows_20260722_1042.csv",
  mime: "text/csv",
  mime_family: "spreadsheet",
  source: "bi_fetch_rows",
  origin: "query",
  kind: "tool_data",
  in_kb: false,
  kb_promotable: false,
  category: "dataset",
  session_id: "s_q3east",
  session_title: "Q3 华东区销售分析",
  has_canvas_ref: false,
  url: "/api/v1/files/art_rows",
};

const TRASH_ROW = {
  ...ASSET_UPLOAD,
  file_id: "art_trashed",
  filename: "测试数据.csv",
  mime: "text/csv",
  mime_family: "spreadsheet",
  deleted_at: "2026-07-22T09:00:00Z",
  url: "/api/v1/files/art_trashed",
};

/** Purge calls this test: verify request only after confirmation, selected files only. */
let purgeCalls: string[][] = [];
let kbRemoveCalls: Record<string, string>[] = [];

async function mockFilesPage(
  page: Page,
  {
    isTeamAdmin = false,
    kbDeleteStatus = 204,
  }: { isTeamAdmin?: boolean; kbDeleteStatus?: number } = {},
) {
  purgeCalls = [];
  kbRemoveCalls = [];
  await page.route("**/api/v1/team/kb-ingestion-readiness", (route: Route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify({ state: "ready" }),
  }));
  await page.route("**/api/v1/auth/me", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_test", email: "test@datarc.cn", display_name: "Test User", avatar_url: null,
        team_id: "team_test", tier: "pro", role: "member",
        team_role: isTeamAdmin ? "team_admin" : "member", status: "active",
        email_verified: true, credit_balance: 10_000, credit_quota: 10_000,
        locale: "zh-CN", timezone: "Asia/Shanghai", is_team_admin: isTeamAdmin,
        workspace_count: 1, needs_onboarding: false,
      }),
    });
  });
  await page.route("**/api/v1/auth/me/workspaces", async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        active_id: "ws_test",
        items: [{ workspace_id: "ws_test", name: "Test Space", role: "member" }],
      }),
    });
  });
  // Promotion/removal uses KB routes (authorization chain there).
  await page.route("**/api/v1/kb/files/**", async (route: Route) => {
    if (route.request().method() === "DELETE") {
      kbRemoveCalls.push(route.request().headers());
      await route.fulfill({
        status: kbDeleteStatus,
        contentType: "application/json",
        body: kbDeleteStatus === 204 ? "" : JSON.stringify({ detail: "forbidden" }),
      });
      return;
    }
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ file_id: "kbf_1" }),
    });
  });
  await page.route("**/api/v1/files/**", async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === "GET" && url.pathname === "/api/v1/files/") {
      // Dispatch by kind / include_deleted: these separate three tabs’ data sources.
      const kinds = url.searchParams.getAll("kind");
      const deleted = url.searchParams.get("include_deleted") === "1";
      const q = url.searchParams.get("q");
      let items: { file_id: string }[] = [];
      if (deleted) items = [TRASH_ROW];
      else if (kinds.includes("tool_data")) items = [RUN_ROW];
      else items = [ASSET_UPLOAD, ASSET_KB, ASSET_EXPORT, ASSET_JSON];
      // Deep-link probe sends q=<artifact_id> across tabs; filter by id to simulate only one tab matching.
      if (q) items = items.filter((it) => it.file_id === q || false);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ total: items.length, size_total_bytes: 3072, items }),
      });
      return;
    }

    if (req.method() === "GET" && url.pathname.endsWith("/files/usage")) {
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ assets: 30_204_660, run_data: 168_770_950 }),
      });
      return;
    }
    if (req.method() === "POST" && url.pathname.endsWith("/files/purge")) {
      const body = JSON.parse(req.postData() || "{}") as { artifact_ids: string[] };
      purgeCalls.push(body.artifact_ids);
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ purged: body.artifact_ids.length, kb_blocked: 0 }),
      });
      return;
    }
    if (req.method() === "DELETE") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (req.method() === "GET" && url.pathname.endsWith("/usages")) {
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ usages: [] }),
      });
      return;
    }
    if (req.method() === "GET" && url.pathname.endsWith("/preview")) {
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          family: "spreadsheet", mode: "rows", text: null,
          columns: [], rows: [], total_rows: 0, shown_rows: 0,
        }),
      });
      return;
    }
    await route.continue();
  });
}

/** Row actions live in the row "⋯" menu; open it and return the menu. */
async function openRowMenu(page: Page, row: Locator): Promise<Locator> {
  await row.hover();
  await row.getByRole("button", { name: /的更多操作$/ }).click();
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  return menu;
}

test("资产 tab 默认打开，运行数据不与资产平铺", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  await expect(page.getByTestId("files-tab-assets")).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-file-id="art_upload"]')).toBeVisible();
  // D1 core: query-generated CSV absent from initial view.
  await expect(page.locator('[data-file-id="art_rows"]')).toHaveCount(0);

  await page.getByTestId("files-tab-run").click();
  await expect(page.locator('[data-file-id="art_rows"]')).toBeVisible();
  await expect(page.locator('[data-file-id="art_upload"]')).toHaveCount(0);
});

test("知识库文件在此页只能跳转，没有删除入口", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  const kbRow = page.locator('[data-file-id="art_kb"]');
  const plainRow = page.locator('[data-file-id="art_upload"]');
  await kbRow.hover();
  // D2: KB files no delete button; “知识库” column shows status/deep link to this KB file.
  await expect(kbRow.getByTestId("kb-col")).toContainText("已加入 · 可用");
  await expect(kbRow.getByRole("link", { name: "在知识库中查看" })).toHaveAttribute(
    "href",
    "/knowledge/?scope=space&space=ws_test&file_id=kbf_kb",
  );
  const kbMenu = await openRowMenu(page, kbRow);
  await expect(kbMenu.getByRole("menuitem", { name: /^删除/ })).toHaveCount(0);
  await page.keyboard.press("Escape");
  // Control: ordinary files still deletable.
  const plainMenu = await openRowMenu(page, plainRow);
  await expect(plainMenu.getByRole("menuitem", { name: "删除", exact: true })).toBeVisible();
});

test("delete runs at once with an in-app undo toast, never a browser confirm", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  let nativeDialog = false;
  page.on("dialog", (d) => { nativeDialog = true; void d.dismiss(); });

  const row = page.locator('[data-file-id="art_upload"]');
  const menu = await openRowMenu(page, row);
  await menu.getByRole("menuitem", { name: "删除", exact: true }).click();

  // An undoable delete asks no question; the undo toast is the safety net.
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "撤销" })).toBeVisible();
  expect(nativeDialog, "no native browser confirm").toBe(false);
});

test("row actions have readable names and the drawer can be closed", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  const row = page.locator('[data-file-id="art_upload"]');
  const menu = await openRowMenu(page, row);
  await expect(menu.getByRole("menuitem", { name: "下载", exact: true })).toHaveAttribute(
    "href",
    /\/api\/v1\/files\/art_upload$/,
  );
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);

  await row.click();
  const close = page.getByRole("button", { name: "关闭", exact: true });
  await expect(close).toBeVisible();
  await close.click();
  await expect(close).toHaveCount(0);
});

test("回收站 tab 提供恢复而非删除", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");
  await page.getByTestId("files-tab-trash").click();

  const row = page.locator('[data-file-id="art_trashed"]');
  await expect(row).toBeVisible();
  const menu = await openRowMenu(page, row);
  await expect(menu.getByRole("menuitem", { name: "恢复", exact: true })).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: /^删除/ })).toHaveCount(0);
});

test("深链 ?focus= 指向运行数据时自动切 tab 并高亮", async ({ page }) => {
  await mockFilesPage(page);
  // Actual canvas source-panel link shape: /files/?focus=<artifact_id>.
  await page.goto("/files/?focus=art_rows");

  // Target in “运行数据” tab: must autoswitch, otherwise appears click did nothing.
  await expect(page.getByTestId("files-tab-run")).toHaveAttribute("aria-selected", "true");
  const row = page.locator('[data-file-id="art_rows"]');
  await expect(row).toBeVisible();
  await expect(row).toHaveClass(/flash/);
});

test("运行数据行显示来源会话链接", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");
  await page.getByTestId("files-tab-run").click();

  const link = page.locator('[data-file-id="art_rows"] a.srclink');
  await expect(link).toHaveText("Q3 华东区销售分析");
  await expect(link).toHaveAttribute("href", /session=s_q3east/);
});

test("删除有报告引用的文件 → 警告弹层而非普通确认", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  const row = page.locator('[data-file-id="art_export"]');
  const menu = await openRowMenu(page, row);
  // The referenced file explains the consequence first, so its danger item ends with "…".
  await menu.getByRole("menuitem", { name: "删除…" }).click();

  const modal = page.getByRole("dialog");
  // Informed consent, not blocking (KB-file rule): explain consequences, still allow deletion.
  await expect(modal.getByText(/历史报告引用/)).toBeVisible();
  await expect(modal.getByRole("button", { name: "仍然删除" })).toBeVisible();
});

test("「在新会话中使用」写入一次性 sessionStorage 并跳转对话页", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  // Intercept navigation to read written payload. Same-origin stub, not abort; abort makes document
  // about:blank, immediately preventing sessionStorage access.
  let navigatedTo: string | null = null;
  await page.route("**/", async (route) => {
    if (route.request().isNavigationRequest()) {
      navigatedTo = new URL(route.request().url()).pathname;
      await route.fulfill({ status: 200, contentType: "text/html", body: "<html><body>stub</body></html>" });
      return;
    }
    await route.continue();
  });

  const row = page.locator('[data-file-id="art_upload"]');
  const menu = await openRowMenu(page, row);
  await menu.getByRole("menuitem", { name: "在新会话中使用" }).click();

  await expect.poll(() => navigatedTo).toBe("/");
  // Payload requires all four fields composer needs for done attachment chip (no byte reupload).
  const raw = await page.evaluate(() => sessionStorage.getItem("bowhead:composer-attach"));
  expect(raw).not.toBeNull();
  expect(JSON.parse(raw as string)).toEqual([{
    fileId: "art_upload",
    filename: "Q1-经营复盘.pptx",
    size: 1_200_000,
    mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  }]);
  // ⚠ Consumption not asserted here, but **never again claim chat-workbench unit tests cover it**:
  // that claim caused 0.87.0 incident: unit tests mounted ChatWorkbench directly, while production navigation from Files
  // lands on empty homepage without component mounted, key never consumed. Consumer moved up into
  // `app/(app)/page.tsx`; `lib/composer-handoff-consumer.test.ts` guards location itself.
});

test("加入知识库：选范围 → 确认 → 提示里给出查看处理进度", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  const row = page.locator('[data-file-id="art_upload"]');
  await row.hover();
  await row.getByTestId("kb-add").click();

  const modal = page.getByRole("dialog");
  await expect(modal.getByText(/Q1-经营复盘\.pptx · 加入后，对话会检索并引用这份资料/)).toBeVisible();
  // All three scopes present, personal default; space names itself, team locked for nonadmins.
  await expect(modal.getByTestId("kb-scope-personal")).toHaveClass(/on/);
  await expect(modal.getByTestId("kb-scope-space")).toContainText("空间 · Test Space");
  await expect(modal.getByTestId("kb-scope-team")).toBeDisabled();
  await expect(modal.getByTestId("kb-scope-team")).toContainText("仅团队管理员");

  await modal.getByTestId("kb-add-confirm").click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByText("已加入知识库 · Q1-经营复盘.pptx")).toBeVisible();
  await expect(page.getByRole("link", { name: "查看处理进度" })).toHaveAttribute(
    "href",
    "/knowledge/?scope=personal&file_id=kbf_1",
  );
});

test("知识库徽章带自动加入标记，且提供移出入口", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  const kbRow = page.locator('[data-file-id="art_kb"]');
  // retention_hint=long_term → autojoin (dashed outline + ✨), distinguishable from manual join.
  await expect(kbRow.locator(".cbadge.kb.auto")).toBeVisible();
  await kbRow.locator(".cbadge.kb").hover();
  await expect(kbRow.getByRole("button", { name: "移出知识库" })).toBeVisible();
});

test("从 Files 移出空间知识条目会发送当前空间上下文", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  const kbRow = page.locator('[data-file-id="art_kb"]');
  await kbRow.locator(".cbadge.kb").hover();
  await kbRow.getByRole("button", { name: "移出知识库" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "移出知识库", exact: true })
    .click();

  await expect.poll(() => kbRemoveCalls).toHaveLength(1);
  expect(kbRemoveCalls[0]["x-space-id"]).toBe("ws_test");
  expect(kbRemoveCalls[0]["x-workspace-id"]).toBe("ws_test");
  await expect(page.getByText(/已移出知识库/)).toBeVisible();
});

test("移出知识库失败显示用户文案而非内部函数名", async ({ page }) => {
  await mockFilesPage(page, { kbDeleteStatus: 403 });
  await page.goto("/files/");

  const kbRow = page.locator('[data-file-id="art_kb"]');
  await kbRow.locator(".cbadge.kb").hover();
  await kbRow.getByRole("button", { name: "移出知识库" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "移出知识库", exact: true })
    .click();

  await expect(
    page.getByText("你没有权限将文件移出这个知识库范围"),
  ).toBeVisible();
  await expect(page.getByText(/removeFileFromKb:/)).toHaveCount(0);
});

test("运行数据 tab 没有加入知识库入口（中间数据不配当可信来源）", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");
  await page.getByTestId("files-tab-run").click();

  const row = page.locator('[data-file-id="art_rows"]');
  await row.hover();
  await expect(row.getByTestId("kb-add")).toHaveCount(0);
  await expect(row.getByTestId("kb-suggest")).toHaveCount(0);
});

test("JSON 成品不显示任何加入知识库入口", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  const row = page.locator('[data-file-id="art_json"]');
  await row.hover();
  await expect(row.getByTestId("kb-add")).toHaveCount(0);
  await expect(row.getByTestId("kb-suggest")).toHaveCount(0);

  await row.click();
  const drawer = page.getByRole("complementary");
  await expect(
    drawer.getByRole("button", { name: "加入知识库", exact: true }),
  ).toHaveCount(0);
});

test("知识库范围弹层关闭后重置为个人范围", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");

  const first = page.locator('[data-file-id="art_upload"]');
  await first.hover();
  await first.getByTestId("kb-add").click();
  let modal = page.getByRole("dialog");
  await modal.getByTestId("kb-scope-space").click();
  await expect(modal.getByTestId("kb-scope-space")).toHaveClass(/on/);
  await modal.getByRole("button", { name: "取消", exact: true }).click();

  const second = page.locator('[data-file-id="art_export"]');
  await second.hover();
  await second.getByTestId("kb-add").click();
  modal = page.getByRole("dialog");
  await expect(modal.getByTestId("kb-scope-personal")).toHaveClass(/on/);
  await expect(modal.getByTestId("kb-scope-space")).not.toHaveClass(/on/);
});

test("团队管理员可选择团队知识库范围", async ({ page }) => {
  await mockFilesPage(page, { isTeamAdmin: true });
  await page.goto("/files/");

  const row = page.locator('[data-file-id="art_upload"]');
  await row.hover();
  await row.getByTestId("kb-add").click();
  await expect(
    page.getByRole("dialog").getByTestId("kb-scope-team"),
  ).toBeEnabled();
});


test("回收站「永久删除」：二次确认 + 说明不可恢复，确认后才真发请求", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");
  await page.getByTestId("files-tab-trash").click();

  const row = page.locator('[data-file-id="art_trashed"]');
  await expect(row).toBeVisible();
  const menu = await openRowMenu(page, row);
  await menu.getByRole("menuitem", { name: "永久删除…" }).click();

  // This path has **no 30-day window**, wording must be explicit; no requests before confirmation.
  await expect(page.locator(".modal")).toContainText("不可恢复");
  expect(purgeCalls).toEqual([]);

  await page.getByTestId("purge-confirm").click();
  await expect.poll(() => purgeCalls).toEqual([["art_trashed"]]);
});

test("永久删除只在回收站出现——资产/运行数据 tab 不给这个入口", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");
  const purge = /^永久删除/;
  // Assets tab: ordinary deletion only (recoverable in trash).
  let menu = await openRowMenu(page, page.locator('[data-file-id="art_upload"]'));
  await expect(menu.getByRole("menuitem", { name: purge })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.getByTestId("files-tab-run").click();
  menu = await openRowMenu(page, page.locator('[data-file-id="art_rows"]'));
  await expect(menu.getByRole("menuitem", { name: purge })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.getByTestId("files-tab-trash").click();
  menu = await openRowMenu(page, page.locator('[data-file-id="art_trashed"]'));
  await expect(menu.getByRole("menuitem", { name: purge })).toBeVisible();
});

test("页头常驻资产/运行数据用量拆分——一个总数说明不了该清哪一堆", async ({ page }) => {
  await mockFilesPage(page);
  await page.goto("/files/");
  const usage = page.getByTestId("files-usage");
  await expect(usage).toBeVisible();
  // Both numbers must coexist, never reduced to one on tab change.
  await expect(usage).toContainText("28.8 MB");
  await expect(usage).toContainText("161.0 MB");
  await page.getByTestId("files-tab-run").click();
  await expect(page.getByTestId("files-usage")).toContainText("28.8 MB");
});
