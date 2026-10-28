import { expect, test, type Page } from "@playwright/test";

/**
 * ?session= deep links and page-owned URL-write timing (chromium + webkit).
 *
 * Page maintains ?session= through history.replaceState; writes must remove Next history markers, otherwise Next
 * useSearchParams stays old URL, later navigation back to old value never reruns guard effect. Guard must mark every write's
 * echo handled: no double hydration, no bounce into session user never clicked (08-25 1d8518894).
 * Dev React StrictMode double-mounts; every case covers that too.
 */

const S2 = "华东 Q3 下滑归因";
const S3 = "各渠道 ROI 对比";

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.route("**/health", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        version: "e2e",
        frontend_min: "e2e",
        force_reload: false,
        feature_flags: {},
      }),
    });
  });
  await page.route("**/api/v1/metrics/event", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
});

/** Session-details request (`GET /api/v1/sessions/:id`) = page opening this session from `?session=`. */
function recordSessionOpens(page: Page): string[] {
  const opened: string[] = [];
  page.on("request", (request) => {
    const match = /\/api\/v1\/sessions\/([^/?]+)$/.exec(new URL(request.url()).pathname);
    if (request.method() === "GET" && match) opened.push(match[1]!);
  });
  return opened;
}

const rowMain = (page: Page, title: string) =>
  page.locator(".unified-work-item", { hasText: title }).locator(".unified-work-item-main");

async function deepLink(page: Page, search: string): Promise<void> {
  await page.evaluate((next) => {
    window.history.pushState(null, "", `${window.location.pathname}${next}`);
  }, search);
}

async function markDocument(page: Page): Promise<void> {
  await page.evaluate(() => {
    (window as unknown as { __sameDocument?: boolean }).__sameDocument = true;
  });
}

async function sameDocument(page: Page): Promise<boolean> {
  return page.evaluate(() => (window as unknown as { __sameDocument?: boolean }).__sameDocument === true);
}

test("initial deep link opens the session exactly once", async ({ page }) => {
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect(rowMain(page, S2)).toHaveAttribute("aria-current", "page");
  await page.waitForTimeout(1500);
  expect(opened).toEqual(["s2"]);
});

test("deep link s2, switch to s3 in the sidebar, then a deep link back to s2 opens s2", async ({ page }) => {
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect.poll(() => opened).toEqual(["s2"]);
  await rowMain(page, S3).click();
  await expect(page).toHaveURL(/\?session=s3$/);
  await expect(rowMain(page, S3)).toHaveAttribute("aria-current", "page");
  // Sidebar-switch URL echo never reopens session.
  await page.waitForTimeout(800);
  const afterSwitch = [...opened];
  expect(afterSwitch.filter((sid) => sid === "s3").length).toBeLessThanOrEqual(1);

  await deepLink(page, "?session=s2");
  await expect.poll(() => opened.length).toBe(afterSwitch.length + 1);
  expect(opened.at(-1)).toBe("s2");
  await expect(rowMain(page, S2)).toHaveAttribute("aria-current", "page");
  await expect(page).toHaveURL(/\?session=s2$/);
});

test("a notification pointing at the initial session opens it after a sidebar switch", async ({ page }) => {
  const notification = {
    id: "ntf_result",
    teamId: "team_smoke",
    category: "scheduled",
    type: "scheduled.completed",
    priority: "p2",
    title: "定时任务已完成",
    body: "华东 Q3 下滑归因已生成",
    subjectType: "session",
    subjectId: "s2",
    actorUserId: null,
    actionUrl: "/?session=s2",
    actions: [{ id: "view", label: "查看结果", kind: "link", url: "/?session=s2" }],
    metadata: {},
    state: "open",
    recipientRole: "owner",
    deliveredAt: "2026-09-28T08:00:00Z",
    readAt: null,
    archivedAt: null,
    createdAt: "2026-09-28T08:00:00Z",
    updatedAt: "2026-09-28T08:00:00Z",
  };
  const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/v1/notifications/unread-count", (route) =>
    route.fulfill(json({ actionRequired: 1, updatesUnread: 0, totalBadge: 1 })));
  await page.route(/\/api\/v1\/notifications(?:\?.*)?$/, (route) =>
    route.fulfill(json({ items: [notification], nextCursor: null })));
  await page.route(/\/api\/v1\/notifications\/[^/]+\/read$/, (route) => route.fulfill(json({ ok: true })));

  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect.poll(() => opened).toEqual(["s2"]);
  await rowMain(page, S3).click();
  await expect(rowMain(page, S3)).toHaveAttribute("aria-current", "page");
  await page.waitForTimeout(500);
  const afterSwitch = [...opened];

  const bell = page.getByRole("button", { name: "通知，1 个待处理" });
  await bell.evaluate((button) => (button as HTMLButtonElement).click());
  await page.getByRole("button", { name: /定时任务已完成/ }).click();
  await expect.poll(() => opened.length).toBe(afterSwitch.length + 1);
  expect(opened.at(-1)).toBe("s2");
  await expect(rowMain(page, S2)).toHaveAttribute("aria-current", "page");
});

test("a deep link that lands just as an earlier hydration commits is not overwritten", async ({ page }) => {
  // As ?session=s2 hydration lands and page writes ?session=s2 in same tick, a deep link has already
  // changed URL to s3. Write deep link synchronously at hydration state commit (also POST /sessions/s2/seen), making timing
  // deterministic. Dev Next syncs one tick later; writing s2 back permanently loses s3. Production batches both updates
  // into render, eventually opens s3, but address bar flashes rewritten s2; thus also assert no post-deep-link write
  // restores s2.
  await page.route("**/api/v1/sessions/s2", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.continue();
  });
  await page.addInitScript(() => {
    const w = window as unknown as { __writesAfterDeepLink?: string[] };
    const nativeReplace = window.history.replaceState.bind(window.history);
    const nativePush = window.history.pushState.bind(window.history);
    // Install beneath Next patch: both page and Next’s own writes ultimately pass here.
    window.history.replaceState = (data, unused, url) => {
      w.__writesAfterDeepLink?.push(String(url));
      nativeReplace(data, unused, url);
    };
    window.history.pushState = (data, unused, url) => {
      w.__writesAfterDeepLink?.push(String(url));
      nativePush(data, unused, url);
    };
    const original = window.fetch.bind(window);
    let fired = false;
    window.fetch = (...args: Parameters<typeof fetch>) => {
      const input = args[0];
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (!fired && /\/api\/v1\/sessions\/s2\/seen$/.test(new URL(url, window.location.href).pathname)) {
        fired = true;
        window.history.pushState(null, "", `${window.location.pathname}?session=s3`);
        w.__writesAfterDeepLink = [];
      }
      return original(...args);
    };
  });
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect.poll(() => opened).toEqual(["s2", "s3"]);
  await expect(rowMain(page, S3)).toHaveAttribute("aria-current", "page");
  await page.waitForTimeout(1000);
  await expect(page).toHaveURL(/\?session=s3$/);
  expect(opened).toEqual(["s2", "s3"]);
  const writes = await page.evaluate(() => (window as unknown as { __writesAfterDeepLink?: string[] }).__writesAfterDeepLink);
  expect(writes?.filter((url) => url.includes("session=s2"))).toEqual([]);
});

test("A then B, New work stays on the clean page (no bounce to a session the user did not pick)", async ({ page }) => {
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect.poll(() => opened).toEqual(["s2"]);
  await rowMain(page, S3).click();
  await expect(rowMain(page, S3)).toHaveAttribute("aria-current", "page");
  const beforeReset = [...opened];

  await page.getByTestId("unified-nav-new_work").click();
  await expect(page).toHaveURL(/\/$/);
  await page.waitForTimeout(3000);
  await expect(page).toHaveURL(/\/$/);
  // Sidebar highlight follows runner.sessionId; mock runner ignores “新工作” reset, so inspect session reopening only.
  expect(opened).toEqual(beforeReset);
});

test("New work, then a deep link to the same session opens it again", async ({ page }) => {
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect.poll(() => opened).toEqual(["s2"]);
  await page.getByTestId("unified-nav-new_work").click();
  await expect(page).toHaveURL(/\/$/);
  await deepLink(page, "?session=s2");
  await expect.poll(() => opened).toEqual(["s2", "s2"]);
  await page.waitForTimeout(1000);
  await expect(page).toHaveURL(/\?session=s2$/);
  expect(opened).toEqual(["s2", "s2"]);
});

test("Back and Forward around New work stay in the same document and follow the URL", async ({ page }) => {
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect.poll(() => opened).toEqual(["s2"]);
  await deepLink(page, "?session=s3");
  await expect.poll(() => opened).toEqual(["s2", "s3"]);
  await page.getByTestId("unified-nav-new_work").click();
  await expect(page).toHaveURL(/\/$/);
  await page.waitForTimeout(800);
  await markDocument(page);

  await page.goBack();
  await expect(page).toHaveURL(/\?session=s2$/);
  await expect.poll(() => opened).toEqual(["s2", "s3", "s2"]);
  await expect(rowMain(page, S2)).toHaveAttribute("aria-current", "page");

  await page.goForward();
  await expect(page).toHaveURL(/\/$/);
  await page.waitForTimeout(800);
  expect(await sameDocument(page)).toBe(true);

  await page.goBack();
  await expect(page).toHaveURL(/\?session=s2$/);
  await page.waitForTimeout(800);
  expect(await sameDocument(page)).toBe(true);
  await expect(rowMain(page, S2)).toHaveAttribute("aria-current", "page");
});

test("history entries written by the page keep Next's router state", async ({ page }) => {
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect.poll(() => opened).toEqual(["s2"]);
  await rowMain(page, S3).click();
  await expect(page).toHaveURL(/\?session=s3$/);
  await page.waitForTimeout(500);
  const afterSwitch = await page.evaluate(() => ({
    na: Boolean(window.history.state?.__NA),
    tree: Array.isArray(window.history.state?.__PRIVATE_NEXTJS_INTERNALS_TREE),
  }));
  expect(afterSwitch).toEqual({ na: true, tree: true });

  await page.getByTestId("unified-nav-new_work").click();
  await expect(page).toHaveURL(/\/$/);
  await page.waitForTimeout(500);
  const afterReset = await page.evaluate(() => ({
    na: Boolean(window.history.state?.__NA),
    tree: Array.isArray(window.history.state?.__PRIVATE_NEXTJS_INTERNALS_TREE),
  }));
  expect(afterReset).toEqual({ na: true, tree: true });
});

// Previously asserted header `.turn-id` (gone since 2026-08-08 batch F merged into “复制排障信息”); mock
// runner ignores “新工作” reset, header session id cannot distinguish reopening either; inspect actual page reopening instead.
test("deleting the open session clears its URL and report while Undo remains available", async ({ page }) => {
  await installS2WithReport(page);
  await page.route("**/api/v1/sessions/s2", async (route) => {
    if (route.request().method() === "DELETE") await route.fulfill({ status: 204, body: "" });
    else await route.fallback();
  });
  await page.goto("/?session=s2");
  await expect(s2Visible(page)).toBeVisible();
  const row = page.locator(".unified-work-item", { hasText: S2 });
  await row.hover();
  await row.getByRole("button", { name: /^更多会话操作/ }).click();
  await page.getByRole("menuitem", { name: "删除会话" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(row).toHaveCount(0);
  await expect(s2Visible(page)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "撤销" })).toBeVisible();
});

test("current archive resets once and a later deep link can reopen it", async ({
  page,
}) => {
  await installArchiveOfS2(page);
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect(page).toHaveURL(/\?session=s2/);

  const row = page.locator(".unified-work-item", {
    hasText: S2,
  });
  await expect(row.locator(".unified-work-item-main")).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect.poll(() => opened).toEqual(["s2"]);
  await row.hover();
  await row.getByRole("button", { name: /^更多会话操作/ }).click();
  await page.getByRole("menuitem", { name: "归档会话" }).click();

  await expect(page).toHaveURL(/\/$/);
  await expect(row).toHaveCount(0);
  // Reset once only, never bounce into just-archived session.
  await page.waitForTimeout(1000);
  await expect(page).toHaveURL(/\/$/);
  expect(opened).toEqual(["s2"]);

  await page.evaluate(() => {
    window.history.pushState(null, "", "/?session=s2");
  });
  await expect(page).toHaveURL(/\?session=s2$/);
  await expect.poll(() => opened).toEqual(["s2", "s2"]);
});

test("after New work, a later deep link to another session still opens it", async ({
  page,
}) => {
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect.poll(() => opened).toEqual(["s2"]);
  await page.getByTestId("unified-nav-new_work").click();
  await expect(page).toHaveURL(/\/$/);
  await page.waitForTimeout(1000);
  expect(opened).toEqual(["s2"]);

  await page.evaluate(() => {
    window.history.pushState(null, "", "/?session=s3");
  });
  await expect.poll(() => opened).toEqual(["s2", "s3"]);
  await expect(page).toHaveURL(/\?session=s3$/);
});

// Common path: sidebar session URL writes leave Next snapshot `/`; reset writing `/` sees unchanged snapshot,
// so ?session= guard effect never reruns; end reset directly when writing clean URL to permit later deep links.
test("from /, open A then B in the sidebar, New work, then a deep link back to A opens it", async ({
  page,
}) => {
  const opened = recordSessionOpens(page);
  await page.goto("/");
  await page
    .locator(".unified-work-item", { hasText: "华东 Q3 下滑归因" })
    .locator(".unified-work-item-main")
    .click();
  await expect(page).toHaveURL(/\?session=s2/);
  const second = page
    .locator(".unified-work-item", { hasText: "各渠道 ROI 对比" })
    .locator(".unified-work-item-main");
  await second.click();
  await expect(page).toHaveURL(/\?session=s3/);
  await expect(second).toHaveAttribute("aria-current", "page");
  const beforeReset = [...opened];

  await page.getByTestId("unified-nav-new_work").click();
  await expect(page).toHaveURL(/\/$/);
  // Never bounce into any just-opened session.
  await page.waitForTimeout(1500);
  await expect(page).toHaveURL(/\/$/);
  expect(opened).toEqual(beforeReset);

  await page.evaluate(() => {
    window.history.pushState(null, "", "/?session=s2");
  });
  await expect.poll(() => opened).toEqual([...beforeReset, "s2"]);
  await expect(page).toHaveURL(/\?session=s2$/);
});

test("New work twice, then a deep link still opens the session", async ({
  page,
}) => {
  const opened = recordSessionOpens(page);
  await page.goto("/?session=s2");
  await expect.poll(() => opened).toEqual(["s2"]);
  await page.getByTestId("unified-nav-new_work").click();
  await expect(page).toHaveURL(/\/$/);
  await page.getByTestId("unified-nav-new_work").click();
  await page.waitForTimeout(1000);
  await expect(page).toHaveURL(/\/$/);
  expect(opened).toEqual(["s2"]);

  await page.evaluate(() => {
    window.history.pushState(null, "", "/?session=s3");
  });
  await expect.poll(() => opened).toEqual(["s2", "s3"]);
  await expect(page).toHaveURL(/\?session=s3$/);
});

/** Minimal s2-archive backend: enable session-archive flag, archive request succeeds. */
async function installArchiveOfS2(page: Page): Promise<void> {
  await page.route("**/health", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ status: "ok", version: "e2e", frontend_min: "e2e", force_reload: false, feature_flags: { session_archive: true } }),
  }));
  await page.route("**/api/v1/sessions/s2/archive", (route) => route.fulfill({ status: 204, body: "" }));
}

/** s2 has one chat turn and editable report, enabling proof page remains s2 and unsaved report edits. */
async function installS2WithReport(page: Page): Promise<{ saves: string[] }> {
  const saves: string[] = [];
  const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/v1/sessions/s2", (route) => route.fulfill(json({
    session: { id: "s2", workspace_id: "ws_test", title: S2, started_at: "2026-09-28T00:00:00Z", last_active_at: "2026-09-28T01:00:00Z", status: "done", cost_usd: 0, tokens_total: 0, intents: ["analysis"] },
    turns: [{ id: "t_s2", seq: 1, user_text: "Build the S2 report", intent: "analysis", status: "done", started_at: "2026-09-28T00:00:00Z", ended_at: "2026-09-28T01:00:00Z", final_answer: "S2 report ready", agent_type: "lead", attachments: [], summary: null }],
  })));
  await page.route("**/api/v1/turns/t_s2/events", (route) => route.fulfill(json({ events: [] })));
  await page.route("**/api/v1/canvas/by-session/s2**", (route) => route.fulfill(json({
    canvas: { id: "cv_s2", sessionId: "s2", workspaceId: "ws_test", title: "S2 Report", status: "done", createdAt: "2026-09-28T00:00:00Z", updatedAt: "2026-09-28T00:00:00Z" },
    sections: [
      { id: "sec_s2_h1", kind: "h1", text: "S2 Report" },
      { id: "sec_s2_body", kind: "narrative", markdown: "S2 report body" },
    ],
    sourceRefs: [],
  })));
  await page.route("**/api/v1/canvas/cv_s2/sections/*", async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    saves.push(route.request().postData() ?? "");
    const patch = (JSON.parse(route.request().postData() ?? "{}") as { patch?: Record<string, unknown> }).patch ?? {};
    return route.fulfill(json({ section: { id: "sec_s2_h1", kind: "h1", text: "S2 Report", ...patch }, version: 2 }));
  });
  return { saves };
}

// Report proves page remains s2: mock chat runner ignores “新工作” reset, chat text cannot serve as criterion.
const s2Visible = (page: Page) => page.getByText("S2 report body");

/** History [?session=s2, /], currently s2: enter s2 → deep link s3 → New work (replace s3 entry with /) → Back. */
async function backToS2WithCleanEntryAhead(page: Page, opened: string[]): Promise<void> {
  await page.goto("/?session=s2");
  await expect(s2Visible(page)).toBeVisible();
  await deepLink(page, "?session=s3");
  await expect.poll(() => opened.at(-1)).toBe("s3");
  await page.getByTestId("unified-nav-new_work").click();
  await expect(page).toHaveURL(/\/$/);
  await page.waitForTimeout(500);
  await page.goBack();
  await expect(page).toHaveURL(/\?session=s2$/);
  await expect(s2Visible(page)).toBeVisible();
}

async function editReportTitle(page: Page): Promise<void> {
  await page.getByRole("button", { name: "编辑此报告" }).click();
  await page.locator('[contenteditable="true"]').first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" edited");
}

test.describe("Forward / Back to the clean home URL", () => {
  test("Forward to / returns to New work in the same document", async ({ page }) => {
    await installS2WithReport(page);
    const opened = recordSessionOpens(page);
    await backToS2WithCleanEntryAhead(page, opened);
    await markDocument(page);
    const before = [...opened];

    await page.goForward();
    await expect(page).toHaveURL(/\/$/);
    await expect(s2Visible(page)).toHaveCount(0);
    await page.waitForTimeout(1000);
    await expect(page).toHaveURL(/\/$/);
    expect(opened).toEqual(before);
    expect(await sameDocument(page)).toBe(true);
  });

  test("a push to / from elsewhere returns to New work and stays there", async ({ page }) => {
    // After New work, Results URL writes must not restore ?session= from uncleared old session id, which would deep-link back s2.
    // Timing depends on Results initialization, ~half chance per attempt; repeat three times.
    test.setTimeout(90_000);
    await installS2WithReport(page);
    // This turn’s events arrive late: session background catch-up still running when leaving.
    await page.route("**/api/v1/turns/t_s2/events", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 800));
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ events: [] }) });
    });
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const opened = recordSessionOpens(page);
      await page.goto("/?session=s2");
      // Leave immediately after session opens: Results still initializing URL state for this session.
      await expect(page.getByText("Build the S2 report")).toBeVisible();
      await deepLink(page, "");
      await expect(page).toHaveURL(/\/$/);
      await expect(s2Visible(page)).toHaveCount(0);
      await page.waitForTimeout(1500);
      await expect(page).toHaveURL(/\/$/);
      expect(opened).toEqual(["s2"]);
      page.removeAllListeners("request");
    }
  });

  test("round trips Back / Forward stay consistent", async ({ page }) => {
    await installS2WithReport(page);
    const opened = recordSessionOpens(page);
    await backToS2WithCleanEntryAhead(page, opened);
    for (let round = 0; round < 2; round += 1) {
      await page.goForward();
      await expect(page).toHaveURL(/\/$/);
      await expect(s2Visible(page)).toHaveCount(0);
      const beforeBack = opened.length;
      await page.goBack();
      await expect(page).toHaveURL(/\?session=s2$/);
      await expect(s2Visible(page)).toBeVisible();
      await expect.poll(() => opened.length).toBe(beforeBack + 1);
    }
    await page.waitForTimeout(800);
    await expect(page).toHaveURL(/\?session=s2$/);
    await expect(s2Visible(page)).toBeVisible();
  });

  test("Forward to / while the session is still opening drops it (no bounce back to it)", async ({ page }) => {
    await installS2WithReport(page);
    const opened = recordSessionOpens(page);
    await backToS2WithCleanEntryAhead(page, opened);
    await page.goForward();
    await expect(s2Visible(page)).toHaveCount(0);
    // Back to s2 again, then forward to / before opening finishes.
    await page.route("**/api/v1/sessions/s2", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1200));
      await route.fallback();
    });
    const beforeBack = opened.length;
    await page.goBack();
    await expect.poll(() => opened.length).toBe(beforeBack + 1);
    await page.goForward();
    await expect(page).toHaveURL(/\/$/);
    await page.waitForTimeout(2500);
    await expect(page).toHaveURL(/\/$/);
    await expect(s2Visible(page)).toHaveCount(0);
  });

  test("unsaved report edits: cancel keeps the session and puts its URL back", async ({ page }) => {
    await installS2WithReport(page);
    const opened = recordSessionOpens(page);
    await backToS2WithCleanEntryAhead(page, opened);
    await editReportTitle(page);
    const historyLength = await page.evaluate(() => window.history.length);

    await page.goForward();
    await page.getByRole("button", { name: "留在当前报告" }).click();
    await expect(page).toHaveURL(/\?session=s2$/);
    await expect(s2Visible(page)).toBeVisible();
    await expect(page.getByText("S2 Report edited")).toBeVisible();
    // Still editing this report.
    await expect(page.getByRole("button", { name: "完成" })).toBeVisible();
    expect(await page.evaluate(() => window.history.length)).toBe(historyLength);
    await page.waitForTimeout(800);
    await expect(page).toHaveURL(/\?session=s2$/);
    await expect(page.getByText("S2 Report edited")).toBeVisible();
  });

  test("unsaved report edits: Back while the dialog is open, then confirm, keeps the session record", async ({ page }) => {
    const { saves } = await installS2WithReport(page);
    const opened = recordSessionOpens(page);
    await backToS2WithCleanEntryAhead(page, opened);
    await editReportTitle(page);

    await page.goForward();
    await expect(page.getByRole("button", { name: "保存并切换" })).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(/\?session=s2$/);
    await page.getByRole("button", { name: "保存并切换" }).click();
    // Edits save normally, but address bar already s2: no reset, never overwrite entry with /.
    await expect.poll(() => saves.some((body) => body.includes("S2 Report edited"))).toBe(true);
    await page.waitForTimeout(1000);
    await expect(page).toHaveURL(/\?session=s2$/);
    await expect(s2Visible(page)).toBeVisible();
    await page.goForward();
    await expect(page).toHaveURL(/\/$/);
  });

  test("unsaved report edits: another URL change while the dialog is open does not stack a second dialog", async ({ page }) => {
    await installS2WithReport(page);
    const opened = recordSessionOpens(page);
    await backToS2WithCleanEntryAhead(page, opened);
    await editReportTitle(page);

    await page.goForward();
    await expect(page.getByRole("button", { name: "保存并切换" })).toBeVisible();
    // Address still clean homepage, searchParams changed: guard effect reruns.
    await page.evaluate(() => window.history.replaceState(null, "", `${window.location.pathname}?utm=1`));
    await page.waitForTimeout(800);
    await page.getByRole("button", { name: "留在当前报告" }).click();
    await page.waitForTimeout(1000);
    await expect(page.getByRole("button", { name: "保存并切换" })).toHaveCount(0);
    await expect(page).toHaveURL(/session=s2/);
    await expect(s2Visible(page)).toBeVisible();
  });

  test("unsaved report edits: confirm saves them, then returns to New work", async ({ page }) => {
    const { saves } = await installS2WithReport(page);
    const opened = recordSessionOpens(page);
    await backToS2WithCleanEntryAhead(page, opened);
    await editReportTitle(page);

    await page.goForward();
    await page.getByRole("button", { name: "保存并切换" }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(s2Visible(page)).toHaveCount(0);
    expect(saves.some((body) => body.includes("S2 Report edited"))).toBe(true);
    await page.waitForTimeout(800);
    await expect(page).toHaveURL(/\/$/);
  });
});
