import { expect, test, type Page } from "@playwright/test";

/** Ordinary homepage input drafts persisted locally (IndexedDB, same database/table as Composer). */
function persistedComposerDraftCount(page: Page) {
  return page.evaluate(
    () =>
      new Promise<number>((resolve) => {
        const request = indexedDB.open("bowhead-composer-recovery-v1");
        request.onerror = () => resolve(-1);
        request.onsuccess = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains("ordinary_drafts")) {
            db.close();
            resolve(0);
            return;
          }
          const count = db
            .transaction("ordinary_drafts", "readonly")
            .objectStore("ordinary_drafts")
            .count();
          count.onsuccess = () => {
            db.close();
            resolve(count.result);
          };
          count.onerror = () => {
            db.close();
            resolve(-1);
          };
        };
      }),
  );
}

test.describe("Option A unified sidebar", () => {
  // Next development-tools button is fixed bottom-left, covering compact-sidebar avatar and intercepting clicks;
  // it is not product UI (absent in production), so hide it in this spec.
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      const hide = () => {
        const style = document.createElement("style");
        style.textContent = "nextjs-portal { display: none !important; }";
        document.head.append(style);
      };
      if (document.head) hide();
      else document.addEventListener("DOMContentLoaded", hide, { once: true });
    });
  });

  test("uses one IA and overlays without shifting the workspace", async ({
    page,
  }) => {
    // 768-1023: the sidebar is hidden and the top bar's menu button opens it as an overlay.
    await page.setViewportSize({ width: 900, height: 900 });
    await page.goto("/");

    const sidebar = page.getByTestId("unified-sidebar");
    await expect(sidebar).toHaveAttribute("data-presentation", "drawer");
    await expect(page.locator(".v2-sidebar")).toHaveCount(0);
    await expect(page.locator(".v2-session-rail")).toHaveCount(0);

    const main = page.locator("main.v2-main");
    const before = await main.boundingBox();
    const expandSidebar = page.getByTestId("mobile-topbar-hamburger");
    await expandSidebar.click();
    await expect(sidebar).toHaveAttribute("data-presentation", "overlay");
    await expect(
      sidebar.locator(".unified-sidebar-expanded[role='dialog']"),
    ).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Shift+Tab");
    expect(
      await sidebar.evaluate((element) =>
        element.contains(document.activeElement),
      ),
    ).toBe(true);
    const after = await main.boundingBox();
    expect(after?.width).toBe(before?.width);

    await expect(page.getByTestId("unified-nav-new_work")).toBeVisible();
    await expect(page.getByTestId("unified-nav-new_work")).toHaveAttribute(
      "aria-current",
      "page",
    );
    await expect(page.getByTestId("unified-nav-reports")).toBeVisible();
    await expect(page.getByTestId("unified-nav-library")).toBeVisible();
    await expect(page.getByTestId("unified-nav-capabilities")).toBeVisible();

    await sidebar.getByRole("button", { name: /账户|Account/ }).click();
    const userMenu = page.locator('[data-user-menu="true"]');
    await expect(userMenu).toBeVisible();
    const firstMenuItem = userMenu.getByRole("menuitem").first();
    await firstMenuItem.focus();
    // Menu items use roving tabIndex: Shift+Tab exits menu to account button, focus stays in overlay.
    await page.keyboard.press("Shift+Tab");
    await expect(
      sidebar.getByRole("button", { name: /账户|Account/ }),
    ).toBeFocused();
    // Esc with menu open closes only menu (even focus on trigger); overlay stays open.
    await page.keyboard.press("Escape");
    await expect(userMenu).toHaveCount(0);
    await expect(sidebar).toHaveAttribute("data-presentation", "overlay");

    await page.keyboard.press("Escape");
    await expect(sidebar).toHaveAttribute("data-presentation", "drawer");
    await expect(expandSidebar).toBeFocused();
  });

  test("keeps the recent-work scrollbar quiet and close to the outer edge", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1600, height: 620 });
    await page.goto("/");

    const panel = page.locator(".unified-sidebar-expanded");
    const list = panel.locator(".unified-work-list");
    await expect(list).toBeVisible();

    const [panelBox, listBox, scrollbar] = await Promise.all([
      panel.boundingBox(),
      list.boundingBox(),
      list.evaluate((element) => ({
        scrollable: element.scrollHeight > element.clientHeight,
        track: getComputedStyle(element, "::-webkit-scrollbar-track")
          .backgroundColor,
        thumbRadius: getComputedStyle(element, "::-webkit-scrollbar-thumb")
          .borderRadius,
      })),
    ]);

    expect(scrollbar.scrollable).toBe(true);
    expect(panelBox).not.toBeNull();
    expect(listBox).not.toBeNull();
    const outerInset =
      (panelBox?.x ?? 0) +
      (panelBox?.width ?? 0) -
      ((listBox?.x ?? 0) + (listBox?.width ?? 0));
    // The list runs to the panel edge (8px panel padding, offset by the list's negative margin).
    expect(outerInset).toBeGreaterThanOrEqual(0);
    expect(outerInset).toBeLessThanOrEqual(2);
    expect(scrollbar.track).toBe("rgba(0, 0, 0, 0)");
    expect(scrollbar.thumbRadius).toBe("999px");
  });

  test("renders the same four destinations in the mobile drawer", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    // The home page names itself with its own title (the brand appears only in the sidebar header).
    await expect(page.getByTestId("mobile-topbar-title")).toHaveText(
      /你的工作进展|Where your work stands/,
    );
    const hamburger = page.getByTestId("mobile-topbar-hamburger");
    await hamburger.click();

    await expect(page.getByTestId("sessions-drawer")).toBeVisible();
    await expect(page.getByTestId("sessions-drawer-close")).toBeFocused();
    const newWork = page.getByTestId("unified-drawer-nav-new_work");
    const reports = page.getByTestId("unified-drawer-nav-reports");
    await expect(newWork).toBeVisible();
    await expect(newWork).toHaveAttribute("aria-current", "page");
    await expect(reports).toBeVisible();
    const [newWorkBox, reportsBox] = await Promise.all([
      newWork.boundingBox(),
      reports.boundingBox(),
    ]);
    expect(newWorkBox?.height).toBe(reportsBox?.height);
    // Navigation rows use the menu-row height (32) with a fine pointer; a coarse pointer raises them to 44.
    expect(newWorkBox?.height).toBeGreaterThanOrEqual(32);
    await expect(page.getByTestId("unified-drawer-nav-library")).toBeVisible();
    await expect(
      page.getByTestId("unified-drawer-nav-capabilities"),
    ).toBeVisible();

    const drawer = page.getByTestId("sessions-drawer");
    await drawer.getByRole("button", { name: /账户|Account/ }).click();
    const userMenu = page.locator('[data-user-menu="true"]');
    await expect(userMenu).toBeVisible();
    // The version is the last line of the account menu, not a drawer footer.
    await expect(userMenu.locator(".v2-user-menu-version")).toContainText(/^Bowhead /);
    await page.keyboard.press("Escape");
    await expect(userMenu).toHaveCount(0);
    await expect(drawer).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.getByTestId("sessions-drawer")).toHaveCount(0);
    await expect(hamburger).toBeFocused();
  });

  test("closes the overlay before opening personal settings", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 900 });
    await page.goto("/");

    const sidebar = page.getByTestId("unified-sidebar");
    await page.getByTestId("mobile-topbar-hamburger").click();
    await expect(sidebar).toHaveAttribute("data-presentation", "overlay");
    await sidebar.getByRole("button", { name: /账户|Account/ }).click();
    await page
      .getByRole("menuitem", {
        name: /^(设置|Settings)$/,
      })
      .click();

    await expect(page).toHaveURL(/\/settings\/profile\/?$/, {
      timeout: 60_000,
    });
    await expect(sidebar).toHaveAttribute("data-presentation", "drawer");
  });

  test("redirects the old Library overview to Files and keeps group disclosure separate", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto("/library/");

    // Library only expands/collapses its children; its old overview URL opens Files.
    await expect(page).toHaveURL(/\/files\/?$/);
    const library = page.getByTestId("unified-nav-library");
    const libraryToggle = page.getByTestId("unified-nav-library-toggle");
    const sidebarFiles = page
      .getByTestId("unified-sidebar")
      .getByRole("link", { name: /文件|Files/ });
    await expect(library).toHaveAttribute("type", "button");
    await expect(library).not.toHaveAttribute("href");
    await expect(library).toHaveAttribute("aria-expanded", "true");
    await expect(libraryToggle).toHaveAttribute("aria-expanded", "true");
    await expect(sidebarFiles).toBeVisible();

    await library.click();
    await expect(library).toHaveAttribute("aria-expanded", "false");
    await expect(libraryToggle).toHaveAttribute("aria-expanded", "false");
    await expect(sidebarFiles).toHaveCount(0);
    await expect(page).toHaveURL(/\/files\/?$/);
    await library.click();
    await expect(sidebarFiles).toBeVisible();

    await page.goto("/capabilities/");
    await expect(
      page.getByTestId("capability-center"),
    ).toBeVisible();
    const capabilitiesLink = page.getByTestId("unified-nav-capabilities");
    await expect(capabilitiesLink).toHaveAttribute("href", "/capabilities/");
    await expect(capabilitiesLink).toHaveAttribute("aria-current", "page");
    await expect(
      page.getByTestId("unified-nav-capabilities-toggle"),
    ).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("unified-nav-skills")).toHaveAttribute(
      "href",
      "/skills/",
    );
  });

  test("keeps the compact sidebar mounted throughout grouped navigation", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto("/");

    const sidebar = page.getByTestId("unified-sidebar");
    await page
      .getByRole("button", {
        name: /收起侧栏|Collapse sidebar/,
      })
      .click();
    await expect(sidebar).toHaveAttribute("data-presentation", "compact");
    await expect
      .poll(() =>
        page.evaluate(() =>
          window.localStorage.getItem("bowhead.sidebar.mode.u_e2e"),
        ),
      )
      .toBe("compact");
    const originalSidebar = await sidebar.elementHandle();
    expect(originalSidebar).not.toBeNull();

    await page.evaluate(() => {
      const auditWindow = window as typeof window & {
        __sidebarPresentationSamples?: string[];
        __stopSidebarPresentationAudit?: () => void;
      };
      const samples: string[] = [];
      let active = true;
      const sample = () => {
        const current = document.querySelector<HTMLElement>(
          '[data-testid="unified-sidebar"]',
        );
        samples.push(
          current?.dataset.presentation ??
            (current?.classList.contains("is-expanded")
              ? "expanded"
              : "missing"),
        );
        if (active) window.requestAnimationFrame(sample);
      };
      auditWindow.__sidebarPresentationSamples = samples;
      auditWindow.__stopSidebarPresentationAudit = () => {
        active = false;
      };
      sample();
    });

    await page.getByRole("button", { name: /资料库|Library/ }).click();
    await page.getByRole("menuitem", { name: /文件|Files/ }).click();
    await expect(page).toHaveURL(/\/files\/?$/, {
      timeout: 60_000,
    });
    await expect(sidebar).toHaveAttribute("data-presentation", "compact");
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          window.requestAnimationFrame(() =>
            window.requestAnimationFrame(() => resolve()),
          ),
        ),
    );

    const result = await page.evaluate((original) => {
      const auditWindow = window as typeof window & {
        __sidebarPresentationSamples?: string[];
        __stopSidebarPresentationAudit?: () => void;
      };
      auditWindow.__stopSidebarPresentationAudit?.();
      return {
        sameNode:
          original ===
          document.querySelector('[data-testid="unified-sidebar"]'),
        originalStillConnected: original?.isConnected ?? false,
        samples: auditWindow.__sidebarPresentationSamples ?? [],
      };
    }, originalSidebar);

    expect(result.sameNode).toBe(true);
    expect(result.originalStillConnected).toBe(true);
    expect(new Set(result.samples)).toEqual(new Set(["compact"]));

    await page.reload();
    await expect(page.getByTestId("unified-sidebar")).toHaveAttribute(
      "data-presentation",
      "compact",
    );
  });

  test("keeps the tablet overlay above its scrim and routes on a real click", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 900 });
    await page.goto("/");

    await page.getByTestId("mobile-topbar-hamburger").click();

    const reports = page.getByTestId("unified-nav-reports");
    await expect(reports).toBeVisible();
    expect(
      await reports.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        const hit = document.elementFromPoint(
          rect.left + rect.width / 2,
          rect.top + rect.height / 2,
        );
        return hit === element || element.contains(hit);
      }),
    ).toBe(true);

    await reports.click();
    await expect(page).toHaveURL(/\/reports\/?$/, {
      timeout: 60_000,
    });
  });

  test("keeps New work actionable across client-side navigation", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/");

    await page.getByTestId("unified-nav-reports").click();

    await expect(page).toHaveURL(/\/reports\/?$/, {
      timeout: 60_000,
    });
    const newWork = page.getByTestId("unified-nav-new_work");
    await expect(newWork).toHaveAttribute("type", "button");
    await expect(newWork).not.toHaveAttribute("href");
    await expect(newWork).not.toHaveAttribute("aria-current");
    await expect(page.getByTestId("unified-nav-reports")).toHaveAttribute(
      "href",
      "/reports/",
    );
    await expect(
      page.getByText(/Failed to load reports|报告加载失败/),
    ).toHaveCount(0);

    await newWork.click();
    await expect(page).toHaveURL(/\/$/);
    await expect(newWork).toHaveAttribute("aria-current", "page");

    await page.getByTestId("unified-nav-reports").click();
    await expect(page).toHaveURL(/\/reports\/?$/, {
      timeout: 60_000,
    });

    await page.getByRole("button", { name: /账户|Account/ }).click();
    await page
      .getByRole("menuitem", {
        name: /^(设置|Settings)$/,
      })
      .click();

    await expect(page).toHaveURL(/\/settings\/profile\/?$/, {
      timeout: 60_000,
    });
    await expect(
      page.getByRole("dialog", { name: /^(个人设置|设置|Settings|Personal settings)$/i }),
    ).toHaveCount(0);
  });

  test("New work clears the home draft and focuses the empty composer", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/");

    const composer = page.getByRole("textbox", { name: /问点什么|Ask/ });
    await expect(composer).toBeEditable({ timeout: 30_000 });
    await composer.fill("这段草稿不应进入下一项工作");
    // Draft persists locally and restores after refresh; wait for actual persistence, since New work must clear that saved draft,
    // not merely input text (previous remount restored it from here).
    await expect.poll(() => persistedComposerDraftCount(page)).toBe(1);
    await page.reload();
    await expect(composer).toHaveValue("这段草稿不应进入下一项工作", {
      timeout: 30_000,
    });
    await expect(composer).toBeEditable();

    await page.getByTestId("unified-nav-new_work").click();

    await expect(composer).toHaveValue("");
    await expect(composer).toBeEditable();
    await expect(composer).toBeFocused();
    await expect(page).toHaveURL(/\/$/);
    await expect.poll(() => persistedComposerDraftCount(page)).toBe(0);

    await page.reload();
    await expect(composer).toBeEditable({ timeout: 30_000 });
    await expect(composer).toHaveValue("");
  });

  test("keeps reports, files, and memory usable with empty mock data", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto("/reports/");

    await expect(
      page.getByText(/Failed to load reports|报告加载失败/),
    ).toHaveCount(0);
    // The toolbar count is the true total (Header, Language & Paging Spec D8).
    await expect(page.getByTestId("reports-total")).toHaveText(/^(0 reports|共 0 份)$/);

    await page.goto("/files/");

    await expect(page.getByText(/NaN/)).toHaveCount(0);
    await expect(page.getByText(/listFiles: 404/)).toHaveCount(0);
    // file-management-v2 P1: asset-library redesign changes empty-state wording to “还没有文件”.
    await expect(page.getByText(/No files yet|还没有文件/)).toBeVisible();

    // Since 08-28, memory belongs to personal settings; standalone /memory/ retired.
    await page.goto("/settings/memory/");
    await expect(page.getByTestId("memory-v2")).toBeVisible({
      timeout: 30_000,
    });
    await expect(
      page.getByText(/这个页面出了点问题|Something went wrong/),
    ).toHaveCount(0);
  });

  test("searches work across object types and keeps partial results usable", async ({
    page,
  }) => {
    await page.route("**/api/v1/search", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          query: "收入",
          partial: true,
          groups: [
            { kind: "session", unavailable: false, items: [] },
            {
              kind: "report",
              unavailable: false,
              items: [
                {
                  kind: "report",
                  id: "cv_1",
                  title: "收入复盘报告",
                  session_id: "s_report",
                  snippet: null,
                  updated_at: "2026-07-22T00:00:00Z",
                },
              ],
            },
            { kind: "file", unavailable: true, items: [] },
            {
              kind: "knowledge",
              unavailable: false,
              items: [
                {
                  kind: "knowledge",
                  id: "kbf_1",
                  title: "收入确认政策.md",
                  session_id: null,
                  snippet: "收入应在履约义务完成时确认。",
                  updated_at: null,
                },
              ],
            },
          ],
        }),
      });
    });
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto("/");

    const sidebar = page.getByTestId("unified-sidebar");
    const search = sidebar.getByRole("searchbox", {
      name: /搜索会话、报告、文件和知识|Search sessions, reports, files, and knowledge/,
    });
    await search.fill("收入");

    await expect(
      sidebar.getByRole("link", { name: /收入复盘报告/ }),
    ).toHaveAttribute("href", "/?session=s_report&report=1");
    await expect(
      sidebar.getByRole("link", { name: /收入确认政策\.md/ }),
    ).toHaveAttribute("href", "/knowledge/?file_id=kbf_1");
    // A failed source says so in its own place (Header, Language & Paging spec 4b).
    await expect(
      sidebar.getByText(
        /文件搜索暂时不可用，其他结果正常|Files search is temporarily unavailable/,
      ),
    ).toBeVisible();
  });

  test("counts session matches, tags content hits and shows more sessions in place", async ({
    page,
  }) => {
    const hit = (id: string, title: string, extra: Record<string, unknown> = {}) => ({
      kind: "session",
      id,
      title,
      session_id: id,
      snippet: null,
      updated_at: "2026-08-14T00:00:00Z",
      match: "title",
      ...extra,
    });
    const searchBodies: Array<Record<string, unknown>> = [];
    await page.route("**/api/v1/search", async (route) => {
      const body = JSON.parse(route.request().postData() ?? "{}");
      searchBodies.push(body);
      const nextPage = body.cursor === "c1";
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          query: "复购",
          partial: false,
          groups: nextPage
            ? [
                {
                  kind: "session",
                  unavailable: false,
                  total: 3,
                  next_cursor: null,
                  items: [hit("s_c", "复购人群画像")],
                },
              ]
            : [
                {
                  kind: "session",
                  unavailable: false,
                  total: 3,
                  next_cursor: "c1",
                  items: [
                    hit("s_a", "会员复购率下滑原因"),
                    hit("s_b", "2026年6月会员分析", {
                      match: "content",
                      snippet: "看一下老客复购周期",
                    }),
                  ],
                },
                { kind: "report", unavailable: false, items: [] },
                { kind: "file", unavailable: false, items: [] },
                { kind: "knowledge", unavailable: false, items: [] },
              ],
        }),
      });
    });
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto("/");

    const sidebar = page.getByTestId("unified-sidebar");
    await sidebar
      .getByRole("searchbox", {
        name: /搜索会话、报告、文件和知识|Search sessions, reports, files, and knowledge/,
      })
      .fill("复购");

    await expect(sidebar.getByRole("heading", { name: /会话\s*3 个/ })).toBeVisible();
    const contentHit = sidebar.getByRole("link", { name: /2026年6月会员分析/ });
    await expect(contentHit.getByText("内容", { exact: true })).toBeVisible();
    await expect(contentHit.locator("mark")).toHaveText("复购");

    await sidebar.getByRole("button", { name: "显示更多会话（还有 1 个）" }).click();
    await expect(sidebar.getByRole("link", { name: /复购人群画像/ })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /显示更多会话/ })).toHaveCount(0);
    expect(searchBodies.at(-1)).toEqual({
      query: "复购",
      limit: 20,
      kinds: ["session"],
      cursor: "c1",
    });
  });

  test("does not carry a hidden search query into recent sessions", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/");
    // The docked sidebar is expanded by default; collapse it to the icon rail.
    await page.getByRole("button", { name: /收起侧栏|Collapse sidebar/ }).click();

    await page
      .getByRole("button", {
        name: /搜索全部工作|Search All Work/,
      })
      .click();
    const search = page.getByRole("searchbox", {
      name: /搜索会话、报告、文件和知识|Search sessions, reports, files, and knowledge/,
    });
    await search.fill("不存在");
    await expect(page.getByText(/没有匹配|Nothing matches/)).toBeVisible();
    await page.keyboard.press("Escape");

    await page
      .getByRole("button", {
        name: /最近会话|Recent Sessions/,
      })
      .click();
    // Row itself (“⋯” accessible name also includes session title, so name-only matching finds both).
    await expect(
      page
        .getByRole("button", { name: /今年 GMV 速查/ })
        .and(page.locator(".unified-work-item-main")),
    ).toBeVisible();
    await expect(page.getByRole("searchbox")).toHaveCount(0);
  });

  test("opens a recent session with the mock detail contract", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/");
    // The docked sidebar is expanded by default; collapse it to the icon rail.
    await page.getByRole("button", { name: /收起侧栏|Collapse sidebar/ }).click();

    await page
      .getByRole("button", {
        name: /最近会话|Recent Sessions/,
      })
      .click();
    await page
      .getByRole("button", { name: /今年 GMV 速查/ })
      .and(page.locator(".unified-work-item-main"))
      .click();

    await expect(page).toHaveURL(/\?session=s1$/);
    const recent = page.getByRole("button", {
      name: /最近会话|Recent Sessions/,
    });
    await expect(recent).toHaveAttribute("aria-current", "page");
    await expect(recent).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByTestId("unified-nav-new_work")).not.toHaveAttribute(
      "aria-current",
    );
    await expect(
      page.getByText(/会话加载失败|Failed to load session/),
    ).toHaveCount(0);
  });

  test("routes account destinations without remounting the app document", async ({
    page,
  }) => {
    // Since 08-28, Space settings is only for current-space admins; default fixture is an ordinary member.
    await page.route("**/api/v1/auth/me/workspaces", (route) =>
      route.request().method() === "GET"
        ? route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              items: [
                {
                  workspace_id: "ws_test",
                  name: "Test Workspace",
                  description: "Default E2E workspace",
                  role: "admin",
                },
              ],
              active_id: "ws_test",
            }),
          })
        : route.fallback(),
    );
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/");
    await page.evaluate(() => {
      (
        window as typeof window & { __sidebarDocumentMarker?: string }
      ).__sidebarDocumentMarker = "preserved";
    });

    // The account menu now opens Settings only; Space settings are a group in
    // the settings navigation for current-space admins.
    await page.getByRole("button", { name: /账户|Account/ }).click();
    await page
      .getByRole("menuitem", {
        name: /^(设置|Settings)$/,
      })
      .click();
    await expect(page).toHaveURL(/\/settings\/profile\/?$/, {
      timeout: 60_000,
    });
    await page
      .getByRole("navigation", { name: /设置导航|Settings navigation/ })
      .locator('a[href*="/space/settings/members/"]')
      .click();

    await expect(page).toHaveURL(/\/space\/settings\/members\/?$/, {
      timeout: 60_000,
    });
    await expect(page.getByText(/Load failed|加载失败/)).toHaveCount(0);
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            (
              window as typeof window & {
                __sidebarDocumentMarker?: string;
              }
            ).__sidebarDocumentMarker,
        ),
      )
      .toBe("preserved");
  });

  test("reuses cross-object search in the mobile drawer", async ({ page }) => {
    await page.route("**/api/v1/search", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          query: "收入",
          partial: false,
          groups: [
            {
              kind: "session",
              unavailable: false,
              items: [
                {
                  kind: "session",
                  id: "s_older",
                  title: "旧收入分析",
                  session_id: "s_older",
                  snippet: null,
                  updated_at: "2026-06-01T00:00:00Z",
                },
              ],
            },
            { kind: "report", unavailable: false, items: [] },
            { kind: "file", unavailable: false, items: [] },
            { kind: "knowledge", unavailable: false, items: [] },
          ],
        }),
      });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await page.getByTestId("mobile-topbar-hamburger").click();

    const drawer = page.getByTestId("sessions-drawer");
    await drawer
      .getByRole("searchbox", {
        name: /搜索会话、报告、文件和知识|Search sessions, reports, files, and knowledge/,
      })
      .fill("收入");
    await expect(
      drawer.getByRole("link", { name: /^旧收入分析/ }),
    ).toHaveAttribute("href", "/?session=s_older");
  });
});
