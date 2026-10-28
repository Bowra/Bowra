import { expect, test } from "@playwright/test";

test("zip bundle upload and progress poll use the active workspace header", async ({ page }) => {
  const bundleHeaders: string[] = [];
  const pollHeaders: string[] = [];

  await page.addInitScript(() => {
    window.localStorage.setItem("bowhead.workspace_id", "ws_alice");
  });

  await page.route("**/api/v1/kb/files**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const workspaceHeader = req.headers()["x-workspace-id"] ?? "";

    if (url.pathname === "/api/v1/kb/files/bundle" && req.method() === "POST") {
      bundleHeaders.push(workspaceHeader);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          uploaded: [{
            file_id: "kbf_child_1",
            artifact_id: "art_child_1",
            filename: "inside.md",
            size_bytes: 12,
            parse_status: "parsing",
          }],
          skipped: [],
          total_entries: 1,
          bundle_filename: "bundle.zip",
        }),
      });
      return;
    }

    if (url.pathname === "/api/v1/kb/files" && req.method() === "GET") {
      pollHeaders.push(workspaceHeader);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          files: [{
            file_id: "kbf_child_1",
            artifact_id: "art_child_1",
            filename: "inside.md",
            size_bytes: 12,
            parse_status: "done",
          }],
          total: 1,
          next_cursor: null,
        }),
      });
      return;
    }

    await route.fallback();
  });

  await page.goto("/");
  await expect(page.locator(".v2-composer")).toBeVisible();
  await expect(page.locator('input[type="file"]').first()).toBeEnabled();

  await page.locator('input[type="file"]').first().setInputFiles({
    name: "bundle.zip",
    mimeType: "application/zip",
    buffer: Buffer.from("PK\x03\x04"),
  });

  await expect.poll(() => bundleHeaders[0] ?? "").toBe("ws_alice");
  await expect.poll(() => pollHeaders[0] ?? "", { timeout: 7_000 }).toBe("ws_alice");
});
