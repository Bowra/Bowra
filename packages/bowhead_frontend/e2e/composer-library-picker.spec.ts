import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

const started = {
  status: "started",
  session_id: "s_library",
  turn_id: "t_library",
  stream_url: "/api/v1/chat/stream/t_library",
  execution_generation: 1,
};

type UploadObservedWindow = Window & { importedLibraryBytes?: string };

async function openComposer(page: Page) {
  await page.addInitScript(() => {
    (globalThis as unknown as { process: { env: Record<string, string> } }).process = { env: {} };
    class QuietEventSource {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;
      readyState = 1;
      addEventListener() {}
      removeEventListener() {}
      close() {
        this.readyState = 2;
      }
    }
    window.EventSource = QuietEventSource as unknown as typeof EventSource;
  });
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      json: {
        id: "u_v2_reference",
        team_id: "team_ref",
        workspaces: [],
        capabilities: { composer: { reference_input: false } },
      },
    }),
  );
  await installReferenceComponent(page, "/bowhead", "_composer-reference-v2-entry.tsx");
  await page.goto("/bowhead/__reference_component_test?auth=1&auth_off=1&locale=en");
  await expect(page.getByRole("textbox")).toBeVisible();
}

async function openPicker(page: Page) {
  await page.locator(".composer-v2__tool-button").click();
  await page.getByRole("menuitem", { name: "Choose from Library" }).click();
  return page.getByRole("dialog", { name: "Choose from Library" });
}

const original = {
  file_id: "kbf_hq_expenses",
  artifact_id: "art_hq_expenses",
  filename: "HQ expenses.csv",
  mime_type: "text/csv",
  size_bytes: 8,
  uploaded_at: "2026-11-26T09:00:00Z",
  scope: "team",
  uploaded_by: "u_hq",
  parse_status: "done",
};

for (const width of [1120, 390]) {
  test(`adds an HQ document to the current draft and remembers it at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.addInitScript(() => {
      const fetch = window.fetch;
      window.fetch = (input, init) => {
        const file = init?.body instanceof FormData ? init.body.get("file") : null;
        if (file instanceof File) {
          void file.text().then((text) => {
            (window as UploadObservedWindow).importedLibraryBytes = text;
          });
        }
        return fetch(input, init);
      };
    });
    await openComposer(page);
    const scopes: string[] = [];
    await page.route("**/api/v1/kb/files?**", (route) => {
      const { searchParams } = new URL(route.request().url());
      const scope = searchParams.get("scope")!;
      scopes.push(scope);
      expect(route.request().headers()["x-space-id"]).toBe("ws_ref");
      const visible =
        (scope === "all" && searchParams.get("file_ids") === original.file_id) ||
        (scope === "team" && (!searchParams.get("q") || searchParams.get("q") === "expenses"));
      return route.fulfill({ json: { files: visible ? [original] : [], total: visible ? 1 : 0 } });
    });
    await page.route("**/api/v1/files/?**", (route) =>
      route.fulfill({ json: { items: [], total: 0, size_total_bytes: 0 } }),
    );
    await page.route("**/api/v1/kb/files/kbf_hq_expenses/download", (route) => {
      expect(route.request().headers()["x-space-id"]).toBe("ws_ref");
      expect(new URL(route.request().url()).pathname).toBe("/bowhead/api/v1/kb/files/kbf_hq_expenses/download");
      return route.fulfill({ contentType: "text/csv", body: "meal,10\n" });
    });
    let uploadCount = 0;
    await page.route("**/api/v1/files/upload", (route) => {
      uploadCount++;
      expect(route.request().headers()["x-space-id"]).toBe("ws_ref");
      expect(route.request().postData()).toContain('filename="HQ expenses.csv"');
      return route.fulfill({
        json: { file_id: "art_selected_copy", filename: original.filename, size: 8, mime: "text/csv" },
      });
    });
    const composer = page.getByRole("textbox");
    await composer.fill("Summarize this.");
    const picker = await openPicker(page);
    await expect(picker.getByRole("tab", { name: "Recent" })).toHaveAttribute("aria-selected", "true");
    await picker.getByRole("tab", { name: "Current Space" }).click();
    await expect(picker.getByText("No matching files in this source.")).toBeVisible();
    await picker.getByRole("tab", { name: "My files" }).click();
    await expect(picker.getByText("No matching files in this source.")).toBeVisible();
    await picker.getByRole("tab", { name: "Team materials" }).click();
    await expect(picker.getByRole("button", { name: /^HQ expenses.csv/ })).toBeVisible();
    await picker.getByRole("searchbox").fill("missing");
    await expect(picker.getByText("No matching files in this source.")).toBeVisible();
    await picker.getByRole("searchbox").fill("expenses");
    const file = picker.getByRole("button", { name: /^HQ expenses.csv/ });
    await expect(file).toBeVisible();
    const box = (await picker.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    await file.click();
    await expect(picker).toHaveCount(0);
    await expect(page.getByText(original.filename, { exact: true })).toBeVisible();
    await expect(composer).toHaveValue("Summarize this.");
    await expect.poll(() => page.evaluate(() => (window as UploadObservedWindow).importedLibraryBytes)).toBe("meal,10\n");
    await expect.poll(() => page.evaluate(() => window.referenceV2.calls.length)).toBe(0);
    await expect
      .poll(() => page.evaluate(async () => (await window.referenceV2.read()).draft?.attachments[0]?.fileId))
      .toBe("art_selected_copy");
    const recents = await openPicker(page);
    await expect(recents.getByRole("button", { name: /^HQ expenses.csv/ })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(recents).toHaveCount(0);
    await expect(composer).toBeFocused();
    await page.route("**/api/v1/chat/send", (route) => {
      expect(route.request().postDataJSON()).toMatchObject({
        message: "Summarize this.",
        attachments: ["art_selected_copy"],
      });
      return route.fulfill({ json: started });
    });
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.locator("#turn")).toHaveText(started.turn_id);
    expect(scopes).toEqual(expect.arrayContaining(["space", "team", "all"]));
    expect(uploadCount).toBe(1);
  });
}

test("discards a pending library selection when the draft Space changes", async ({ page }) => {
  await openComposer(page);
  await page.route("**/api/v1/kb/files?**", (route) => route.fulfill({ json: { files: [original], total: 1 } }));
  let releaseDownload!: () => void;
  let finishDownload!: () => void;
  const held = new Promise<void>((resolve) => {
    releaseDownload = resolve;
  });
  const finished = new Promise<void>((resolve) => {
    finishDownload = resolve;
  });
  const downloadUrl = "**/api/v1/kb/files/kbf_hq_expenses/download";
  await page.route(downloadUrl, async (route) => {
    await held;
    try {
      await route.fulfill({ contentType: "text/csv", body: "meal,10\n" });
    } catch {
      // The old Composer can abort the browser request before fulfillment.
    } finally {
      finishDownload();
    }
  });
  let uploads = 0;
  await page.route("**/api/v1/files/upload", async (route) => {
    uploads++;
    await route.abort();
  });
  const picker = await openPicker(page);
  await picker.getByRole("tab", { name: "Team materials" }).click();
  const pendingRequest = page.waitForRequest(downloadUrl);
  await picker.getByRole("button", { name: /^HQ expenses.csv/ }).click();
  await pendingRequest;
  await page.evaluate(() => window.referenceV2.configure({ otherScope: true }));
  await expect(picker).toHaveCount(0);
  releaseDownload();
  await finished;
  await expect(page.locator(".v2-chip")).toHaveCount(0);
  expect(uploads).toBe(0);
});
