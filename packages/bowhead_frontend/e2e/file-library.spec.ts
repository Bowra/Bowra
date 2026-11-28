import { test, expect } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

test.use({ locale: "en-US" });
test.setTimeout(150_000);
test.beforeEach(({ page }, testInfo) => {
  page.on("pageerror", (error) => {
    void testInfo.attach("page-error", { body: error.message, contentType: "text/plain" });
  });
});

for (const width of [1440, 412]) {
  test.describe("Files at " + width, () => {
    test.use({ viewport: { width, height: 900 }, locale: "en-US" });
    test("opens delivered content and loads storage only on entry", async ({ page }) => {
      await mockAuthedApp(page, { fileLibrary: true, subagentManagement: true });
      const item = {
        file_id: "art_libbrowse001",
        lineage_id: "art_libbrowse001",
        kind: "deliverable",
        source: "sandbox_output",
        mime: "text/plain",
        mime_family: "text",
        filename: "Findings.txt",
        trust: "trusted",
        views: ["deliverables", "all"],
        size: 18,
        created_at: "2026-10-02T11:00:00Z",
        delivered_at: "2026-10-03T11:00:00Z",
        version_count: 1,
      };
      let summaries = 0,
        previews = 0;
      await page.route("**/api/v1/files/library?**", (route) =>
        route.fulfill({
          json: {
            items: new URL(route.request().url()).searchParams.has("q") ? [] : [item],
            next_cursor: null,
            total: 1,
          },
        }),
      );
      await page.route("**/api/v1/files/library/summary", (route) => {
        summaries++;
        return route.fulfill({ json: { counts: { all: 1, deliverables: 1 }, bytes: { assets: 18 } } });
      });
      await page.route("**/api/v1/files/art_libbrowse001/preview?**", (route) => {
        previews++;
        return route.fulfill({ json: { family: "text", text: "Quarterly findings", mode: "text" } });
      });
      const openedAt = Date.now();
      await page.goto("/files/", { waitUntil: "domcontentloaded" });
      await expect(page.getByRole("tab", { name: "Deliverables" })).toHaveAttribute("aria-selected", "true", {
        timeout: 45_000,
      });
      const row = page.getByRole("button", { name: "Findings.txt" });
      await expect(row).toBeVisible();
      if (width === 412) {
        expect(previews).toBe(0);
        await row.click();
      }
      await expect(page.getByText("Quarterly findings", { exact: true })).toBeVisible();
      expect(previews).toBe(1);
      test.info().annotations.push({ type: "content-visible-ms", description: String(Date.now() - openedAt) });
      expect(summaries).toBe(0);
      if (width === 412) await page.locator(".library-reader .dr-head button").click();
      await page.getByRole("searchbox", { name: "Search filename..." }).fill("no such delivery");
      await expect(page.getByRole("button", { name: "Clear filters" })).toBeVisible();
      await page.getByRole("button", { name: "Clear filters" }).click();
      await page.getByRole("tab", { name: "Manage storage" }).click();
      await expect.poll(() => summaries).toBe(1);
    });
  });
}
test("reviews policy before applying its exact inventory", async ({ page }) => {
  await mockAuthedApp(page, { fileLibrary: true, subagentManagement: true });
  const writes: unknown[] = [];
  await page.route("**/api/v1/team-settings/file-retention", (route) => {
    if (route.request().method() === "PUT") writes.push(route.request().postDataJSON());
    return route.fulfill({
      json: { days: writes.length ? 90 : null, effective_days: writes.length ? 90 : 30, version: writes.length },
    });
  });
  await page.route("**/api/v1/team-settings/file-retention/review", (route) =>
    route.fulfill({
      json: { review_id: "12345678901234567890123456789012", impact: { eligible: { count: 2, bytes: 1024 } } },
    }),
  );
  await page.goto("/team/settings/file-retention/");
  await page.getByRole("combobox").selectOption("90");
  await page.getByRole("button", { name: "Preview impact" }).click();
  await expect(page.getByText("Eligible for cleanup", { exact: true })).toBeVisible();
  expect(writes).toEqual([]);
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Apply policy" }).click();
  await expect(page.getByText("Policy saved.", { exact: true })).toBeVisible();
  expect(writes).toEqual([{ review_id: "12345678901234567890123456789012", include_existing: true }]);
});

test("keeps an unavailable exact link out of the viewer", async ({ page }) => {
  await mockAuthedApp(page, { fileLibrary: true });
  let previews = 0;
  await page.route("**/api/v1/files/library?**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));
  await page.route("**/api/v1/files/library/items/art_unavailable", (route) =>
    route.fulfill({ status: 404, json: { detail: "File unavailable" } }),
  );
  await page.route("**/api/v1/files/*/preview?**", (route) => {
    previews++;
    return route.abort();
  });
  await page.goto("/files/?focus=art_unavailable");
  await expect(page.getByRole("alert").filter({ hasText: "This file is unavailable" })).toBeVisible();
  await expect(page.locator(".library-reader")).toHaveCount(0);
  expect(previews).toBe(0);
});
