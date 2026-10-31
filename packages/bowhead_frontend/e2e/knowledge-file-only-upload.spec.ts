import { expect, test } from "@playwright/test";
import { routeSubmissionCapability } from "./_composer-recovery-fixtures";

// Browser/UI contract against controlled HTTP fixtures. Actual OPC validation
// and byte-identical download are exercised by Gateway integration tests.
for (const width of [412, 1440]) {
  for (const transport of ["multipart", "legacy-fallback", "presign"]) {
    test(`Chat ZIP stays one file without knowledge ingestion across reload (${width}, ${transport})`, async ({ page }) => {
      const large = transport !== "multipart";
      await page.setViewportSize({ width, height: 900 });
      await routeSubmissionCapability(page, () => false);
      const filename = "documents.zip";
      const mime = "application/zip";
      let uploads = 0;
      let presigns = 0;
      let confirms = 0;
      let bundles = 0;
      const result = {
        file_id: "art_file_only_zip", filename, mime, size: large ? 11 * 1024 * 1024 : 100,
        url: "/api/v1/files/art_file_only_zip",
      };
      await page.route("**/api/v1/kb/**", async (route) => {
        if (route.request().method() === "POST") bundles++;
        await route.fallback();
      });
      await page.route("**/api/v1/files/presign", async (route) => {
        presigns++;
        if (transport === "legacy-fallback") {
          await route.fulfill({ status: 409, json: { detail: { error: { code: "file_multipart_required" } } } });
        } else {
          await route.fulfill({ status: 200, json: {
            file_id: result.file_id, artifact_id: result.file_id,
            upload: { url: new URL(`/api/v1/files/local-receiver/${result.file_id}`, page.url()).href, fields: {}, method: "POST" },
            expires_at: new Date(Date.now() + 3600000).toISOString(),
            confirm_url: `/api/v1/files/presign/${result.file_id}/confirm`,
          } });
        }
      });
      await page.route("**/api/v1/files/local-receiver/*", async (route) => {
        uploads++;
        await route.fulfill({ status: 200, json: {} });
      });
      await page.route("**/api/v1/files/presign/*/confirm", async (route) => {
        confirms++;
        await route.fulfill({ status: 200, json: { ...result, status: "ready", idempotent_replay: false } });
      });
      await page.route("**/api/v1/files/upload*", async (route) => {
        uploads++;
        await route.fulfill({ status: 200, json: result });
      });
      await page.goto("/");
      const composer = page.locator(".v2-composer");
      await expect(composer).toBeVisible();
      await expect(composer.locator('input[type="file"]')).toBeEnabled();
      await composer.locator('input[type="file"]').setInputFiles({
        name: filename, mimeType: "application/zip", buffer: Buffer.alloc(large ? 11 * 1024 * 1024 : 100),
      });
      await expect(composer.locator(".v2-chip__name")).toHaveText(filename);
      await expect(composer.locator(".v2-send-btn")).toBeEnabled();
      expect(uploads).toBe(1);
      expect(presigns).toBe(large ? 1 : 0);
      expect(confirms).toBe(transport === "presign" ? 1 : 0);
      expect(bundles).toBe(0);

      // Wait for the real composer IndexedDB write, not an arbitrary sleep.
      await expect.poll(() => page.evaluate(async () => {
        const databases = await indexedDB.databases();
        for (const entry of databases) {
          if (!entry.name?.startsWith("bowhead-composer")) continue;
          const db = await new Promise<IDBDatabase>((resolve, reject) => {
            const request = indexedDB.open(entry.name!);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
          });
          try {
            if (!db.objectStoreNames.contains("ordinary_drafts")) continue;
            const rows = await new Promise<unknown[]>((resolve, reject) => {
              const request = db.transaction("ordinary_drafts").objectStore("ordinary_drafts").getAll();
              request.onsuccess = () => resolve(request.result);
              request.onerror = () => reject(request.error);
            });
            if (JSON.stringify(rows).includes("art_file_only_zip")) return true;
          } finally { db.close(); }
        }
        return false;
      })).toBe(true);

      await page.reload();
      await expect(composer.locator(".v2-chip__name")).toHaveText(filename);
      await expect(composer.locator(".v2-send-btn")).toBeEnabled();
      expect(uploads).toBe(1); // F5 restores the reference, never reuploads bytes.
      expect(confirms).toBe(transport === "presign" ? 1 : 0);
      expect(bundles).toBe(0);
    });
  }
}
