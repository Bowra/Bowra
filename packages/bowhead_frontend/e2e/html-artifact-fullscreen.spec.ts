import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Layer contract for report HTML-embed fullscreen (html-artifact.tsx), verified inside real application shell
 * (docs/design/组件统一-技术设计.md “图层”):
 * - Fullscreen covers sidebar, Topbar, and all Workbench areas (elementFromPoint hits backdrop or card);
 * - Card switches in place, iframe node never moves/reloads (same element and contentWindow, no new load);
 * - Focus enters fullscreen, Esc closes and returns to expand button;
 * - Body-mounted menus and Toast layers remain above fullscreen.
 * jsdom lacks stacking/hit testing; enforce these here only.
 */

const SESSION_ID = "s_html_fullscreen";
const ARTIFACT_ID = "art_html_fullscreen";
const TITLE = "Fullscreen fixture";

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function mockReport(page: Page, html = "<!doctype html><html><body><p id=\"fs-content\">Embedded result</p></body></html>", maxBytes?: number): Promise<void> {
  await page.route("**/health", (route: Route) =>
    route.fulfill(json({ ok: true, feature_flags: { results_workspace: true } })),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill(json({
      id: "fs_viewer",
      email: "fs-viewer@example.com",
      name: "Fullscreen Viewer",
      tier: "ultra",
      capabilities: {},
      entitlement: { read_only: false },
    })),
  );
  await page.route("**/api/v1/auth/me/workspaces", (route) =>
    route.fulfill(json({
      items: [{ workspace_id: "ws_fs", name: "Fullscreen workspace", description: "" }],
      active_id: "ws_fs",
    })),
  );
  await page.route("**/api/v1/user-settings**", (route) => route.fulfill(json({ settings: {} })));
  await page.route("**/api/v1/theme/effective", (route) => route.fulfill(json({ mode: "light", tokens: {} })));
  await page.route("**/api/v1/sessions", (route) =>
    route.fulfill(json({
      groups: [{
        id: "today",
        label: "Today",
        items: [{ id: SESSION_ID, title: TITLE, intents: ["report"], time: "09:00", state: "done" }],
      }],
    })),
  );
  await page.route(`**/api/v1/sessions/${SESSION_ID}**`, (route) =>
    route.fulfill(json({ detail: "forbidden" }, 403)),
  );
  await page.route(`**/api/v1/canvas/by-session/${SESSION_ID}**`, async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith(`/artifacts/${ARTIFACT_ID}`)) {
      await route.fulfill({
        status: 200,
        contentType: "text/html",
        headers: maxBytes == null ? {} : { "X-Bowhead-HTML-Max-Bytes": String(maxBytes) },
        body: html,
      });
      return;
    }
    if (pathname.endsWith("/artifacts")) {
      await route.fulfill(json({ items: [] }));
      return;
    }
    await route.fulfill(json({
      canvas: {
        id: "cv_html_fullscreen",
        sessionId: SESSION_ID,
        workspaceId: "ws_fs",
        title: TITLE,
        status: "done",
        createdAt: "2026-09-25T00:00:00Z",
        updatedAt: "2026-09-25T00:00:00Z",
      },
      sections: [{ id: "html_fs", kind: "html_artifact", artifact_id: ARTIFACT_ID, title: TITLE, unverified: true }],
      sourceRefs: [],
    }));
  });
}

interface FrameWatch {
  sameElement: boolean;
  sameWindow: boolean;
  loads: number;
}

declare global {
  interface Window {
    __fsWatch?: { frame: HTMLIFrameElement; win: Window | null; loads: number };
  }
}

async function watchFrame(page: Page): Promise<void> {
  await page.locator(`iframe[title="${TITLE}"]`).evaluate((node) => {
    const frame = node as HTMLIFrameElement;
    const watch = { frame, win: frame.contentWindow, loads: 0 };
    frame.addEventListener("load", () => {
      watch.loads += 1;
    });
    window.__fsWatch = watch;
  });
}

async function readFrameWatch(page: Page): Promise<FrameWatch> {
  return page.locator(`iframe[title="${TITLE}"]`).evaluate((node) => {
    const watch = window.__fsWatch!;
    return {
      sameElement: node === watch.frame,
      sameWindow: (node as HTMLIFrameElement).contentWindow === watch.win,
      loads: watch.loads,
    };
  });
}

/**
 * Probe viewport coordinates and return hits outside fullscreen (backdrop/card). Temporarily remove inert:
 * inert elements are excluded from hit testing, so inert upper layers are passed through (as in _layer-probe.ts).
 */
async function hitsOutsideFullscreen(page: Page, points: Array<[number, number]>): Promise<string[]> {
  return page.evaluate((pts) => {
    const inertNodes = Array.from(document.querySelectorAll("[inert]"));
    for (const node of inertNodes) node.removeAttribute("inert");
    const misses: string[] = [];
    for (const [x, y] of pts) {
      const hit = document.elementFromPoint(x, y);
      if (!hit?.closest(".rc-embed-overlay, .rc-embed--expanded")) {
        const cls = typeof hit?.className === "string" ? hit.className : "";
        misses.push(`${x},${y} -> ${hit?.tagName.toLowerCase() ?? "(nothing)"}.${cls}`);
      }
    }
    for (const node of inertNodes) node.setAttribute("inert", "");
    return misses;
  }, points);
}

/** From card to body, list ancestors creating stacking contexts or fixed containing blocks (common CSS-spec conditions). */
async function trappingAncestors(page: Page): Promise<string[]> {
  return page.locator(".rc-embed").last().evaluate((card) => {
    const out: string[] = [];
    for (let el = card.parentElement; el && el !== document.documentElement; el = el.parentElement) {
      const s = getComputedStyle(el);
      const parent = el.parentElement ? getComputedStyle(el.parentElement) : null;
      const reasons: string[] = [];
      const hasZ = s.zIndex !== "auto";
      if (s.position === "fixed" || s.position === "sticky") reasons.push(`position:${s.position}`);
      if (hasZ && (s.position !== "static" || (parent && /(flex|grid)$/.test(parent.display)))) reasons.push(`z-index:${s.zIndex}`);
      if (s.isolation === "isolate") reasons.push("isolation");
      if (Number(s.opacity) < 1) reasons.push(`opacity:${s.opacity}`);
      for (const prop of ["transform", "translate", "rotate", "scale", "perspective", "filter", "backdrop-filter", "-webkit-backdrop-filter", "clip-path", "mask-image"]) {
        const value = s.getPropertyValue(prop);
        if (value && value !== "none") reasons.push(`${prop}:${value}`);
      }
      if (s.mixBlendMode !== "normal") reasons.push(`mix-blend-mode:${s.mixBlendMode}`);
      if (/(layout|paint|strict|content)/.test(s.contain)) reasons.push(`contain:${s.contain}`);
      if (/(transform|opacity|filter|perspective|isolation|z-index|position)/.test(s.willChange)) reasons.push(`will-change:${s.willChange}`);
      if (reasons.length) out.push(`${el.tagName.toLowerCase()}.${el.className.trim().split(/\s+/)[0]} (${reasons.join(", ")})`);
    }
    return out;
  });
}

/**
 * Are body-mounted menus/Toast layers above fullscreen? Put center-viewport probes (menu uses `--z-menu`,
 * Toast mounts in actual application Toast region) and check whether elementFromPoint hits them.
 */
async function bodyLayersAboveFullscreen(page: Page): Promise<{ menu: boolean; toast: boolean }> {
  return page.evaluate(() => {
    const cx = window.innerWidth / 2;
    const cy = window.innerHeight / 2;
    const probe = (host: Element, z: string | null) => {
      const el = document.createElement("div");
      el.style.cssText = `position:fixed;left:${cx - 60}px;top:${cy - 30}px;width:120px;height:60px;pointer-events:auto;${z ? `z-index:${z};` : ""}`;
      host.append(el);
      const ok = document.elementFromPoint(cx, cy) === el;
      el.remove();
      return ok;
    };
    const toastRegion = document.querySelector("[data-testid=toast-region-assertive]");
    return {
      menu: probe(document.body, "var(--z-menu)"),
      toast: toastRegion ? probe(toastRegion, null) : false,
    };
  });
}

const SHOT_DIR = process.env.HTML_FULLSCREEN_SHOT_DIR;

for (const sample of [
  { mib: 6, table: false, maxBytes: undefined },
  { mib: 25, table: false, maxBytes: undefined },
  { mib: 25, table: true, maxBytes: undefined },
  { mib: 50, table: false, maxBytes: 50 * 1024 * 1024 },
]) {
  test(`previews complete ${sample.mib} MiB HTML with ${sample.table ? "table rows" : "embedded data"}`, async ({ page }, testInfo) => {
    const prefix = '<!doctype html><html><head><style>table{table-layout:fixed;width:100%}td{overflow:hidden;white-space:nowrap}</style></head><body><p id="fs-content">Large report</p>';
    const suffix = '<p id="report-end">Complete report</p></body></html>';
    const size = sample.mib * 1024 * 1024;
    let content: string;
    if (sample.table) {
      const rows = 2048;
      const cellBytes = Math.floor((size - prefix.length - suffix.length - 64 - rows * 45) / (rows * 4));
      const row = `<tr>${`<td>${"x".repeat(cellBytes)}</td>`.repeat(4)}</tr>`;
      content = `<table><tbody>${row.repeat(rows)}</tbody></table>`;
    } else {
      content = "";
    }
    const padding = size - prefix.length - content.length - suffix.length - "<pre hidden></pre>".length;
    const html = prefix + content + `<pre hidden>${"x".repeat(padding)}</pre>` + suffix;
    expect(Buffer.byteLength(html)).toBe(size);
    await mockReport(page, html, sample.maxBytes);
    const started = Date.now();
    await page.goto(`/?session=${SESSION_ID}&report=1`);
    const frame = page.frameLocator(`iframe[title="${TITLE}"]`);
    await expect(frame.locator("#report-end")).toHaveText("Complete report", { timeout: 30_000 });
    await expect(page.locator(`iframe[title="${TITLE}"]`)).toHaveAttribute("sandbox", "");
    if (sample.table) await expect(frame.locator("tbody tr")).toHaveCount(2048);
    console.log(`HTML preview ${testInfo.project.name} ${sample.mib} MiB ${sample.table ? "table" : "data"}: ${Date.now() - started} ms`);
  });
}

test("explains the active Team HTML limit and retains original download access", async ({ page }) => {
  await mockReport(page, "<p>Over the configured limit</p>", 16);
  await page.goto(`/?session=${SESSION_ID}&report=1`);
  await expect(page.locator(".rc-embed-error")).toContainText("16 B");
  await expect(page.locator(".rc-embed-error")).toContainText("HTML");
  await expect(page.locator("iframe")).toHaveCount(0);
  await expect(page.locator(`a[href*="/artifacts/${ARTIFACT_ID}"]`)).toBeVisible();
});

test("桌面：全屏盖住侧栏、顶栏与工作台，iframe 不重载，Esc 回焦到展开按钮", async ({ page, browserName }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockReport(page);
  await page.goto(`/?session=${SESSION_ID}&report=1`);
  const iframe = page.locator(`iframe[title="${TITLE}"]`);
  await expect(page.frameLocator(`iframe[title="${TITLE}"]`).locator("#fs-content")).toHaveText("Embedded result");
  await watchFrame(page);
  const before = await trappingAncestors(page);
  console.log(`[${browserName}] 收起时困住卡片的祖先:\n  ${before.join("\n  ")}`);

  const toggle = page.locator(".rc-embed-head").getByRole("button", { name: "展开", exact: true });
  await toggle.click();
  const dialog = page.getByRole("dialog", { name: TITLE });
  await expect(dialog).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-rc-embed-fullscreen", "");
  // Fullscreen leaves only desktop shell .v2-app-root (fixed, z 0 in root stacking context; cannot and need not remove it).
  expect(await trappingAncestors(page)).toEqual(["div.v2-app-root (position:fixed)"]);

  // Sidebar (brand, navigation, session list, account), Topbar, four Workbench corners, and blank report area.
  const misses = await hitsOutsideFullscreen(page, [
    [20, 20], [140, 250], [100, 450], [140, 870],
    [720, 20], [1400, 20],
    [300, 100], [1420, 100], [300, 880], [1420, 880], [720, 880],
  ]);
  expect(misses).toEqual([]);
  // Focus enters fullscreen layer.
  expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  // Card is viewport-centered (no ancestor confines fixed containing block to report area).
  const box = (await dialog.boundingBox())!;
  expect(Math.abs(box.x + box.width / 2 - 720)).toBeLessThan(2);
  expect(Math.abs(box.y + box.height / 2 - 450)).toBeLessThan(2);
  if (SHOT_DIR) await page.screenshot({ path: `${SHOT_DIR}/desktop-open-${browserName}.png` });

  // Same iframe element and contentWindow; opening causes no new load.
  expect(await readFrameWatch(page)).toEqual({ sameElement: true, sameWindow: true, loads: 0 });
  expect(await bodyLayersAboveFullscreen(page)).toEqual({ menu: true, toast: true });

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("html")).not.toHaveAttribute("data-rc-embed-fullscreen", "");
  await expect(toggle).toBeFocused();
  if (SHOT_DIR) await page.screenshot({ path: `${SHOT_DIR}/desktop-closed-${browserName}.png` });

  // Same iframe element and contentWindow after closing; no new load.
  await page.waitForTimeout(200);
  expect(await readFrameWatch(page)).toEqual({ sameElement: true, sameWindow: true, loads: 0 });
  await expect(iframe).toBeVisible();
});

test("桌面：全屏里 Tab 不出层，点遮罩关闭", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockReport(page);
  await page.goto(`/?session=${SESSION_ID}&report=1`);
  await expect(page.frameLocator(`iframe[title="${TITLE}"]`).locator("#fs-content")).toHaveText("Embedded result");
  const toggle = page.locator(".rc-embed-head").getByRole("button", { name: "展开", exact: true });
  await toggle.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: TITLE });
  await expect(dialog).toBeVisible();
  for (let i = 0; i < 8; i += 1) {
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  }
  await page.mouse.click(30, 450);
  await expect(dialog).toHaveCount(0);
});

test("手机：全屏盖住顶栏与报告区", async ({ page, browserName }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockReport(page);
  await page.goto(`/?session=${SESSION_ID}&report=1`);
  await page.getByRole("button", { name: /打开安全预览|Open safe preview/ }).click();
  const dialog = page.getByRole("dialog", { name: TITLE });
  await expect(dialog).toBeVisible();
  await expect(page.frameLocator(`iframe[title="${TITLE}"]`).locator("#fs-content")).toHaveText("Embedded result");
  const misses = await hitsOutsideFullscreen(page, [[10, 10], [195, 20], [380, 20], [10, 830], [380, 830], [195, 422]]);
  expect(misses).toEqual([]);
  expect(await bodyLayersAboveFullscreen(page)).toEqual({ menu: true, toast: true });
  if (SHOT_DIR) await page.screenshot({ path: `${SHOT_DIR}/mobile-open-${browserName}.png` });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

for (const width of [1280, 390]) {
  test(`offline report HTML retains controls across Full view at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await mockReport(page, '<!doctype html><button id="count" onclick="this.textContent=Number(this.textContent)+1">0</button>');
    await page.route("**/api/v1/results/capabilities", (route) => route.fulfill(json({
      dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50, interactive_html: true,
    })));
    await page.goto(`/?session=${SESSION_ID}&report=1`);
    const inner = page.frameLocator(`iframe[title="${TITLE}"]`).frameLocator('iframe[title="HTML content"]');
    await expect(inner.locator("#count")).toHaveText("0");
    await inner.locator("#count").click();
    const toggle = page.locator(".rc-embed-head").getByRole("button", { name: "展开", exact: true });
    await toggle.click();
    await expect(inner.locator("#count")).toHaveText("1");
    await inner.locator("#count").press("Escape");
    await expect(page.getByRole("dialog", { name: TITLE })).toHaveCount(0);
    await expect(inner.locator("#count")).toHaveText("1");
    await expect(toggle).toBeFocused();
  });
}
