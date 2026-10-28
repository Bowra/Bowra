import { expect, test, type Page } from "@playwright/test";
import fixture from "../__tests__/fixtures/document-draft-canvas.json";
import { installReferenceComponent } from "./_composer-reference-component-modules";

const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l1cAAAAASUVORK5CYII=",
  "base64",
);
async function start(page: Page, prefix: string) {
  await page.route(
    "**/api/v1/documents/djob_canvas_fixture/preview/page-1.png",
    (route) => route.fulfill({ contentType: "image/png", body: png }),
  );
  await installReferenceComponent(
    page,
    prefix,
    "_document-draft-canvas-entry.tsx",
  );
  await page.goto(`${prefix}/__reference_component_test`);
  await expect(page.getByRole("img")).toBeVisible();
  await page.getByRole("checkbox").check();
  await expect(
    page.getByRole("button", { name: "Confirm and publish" }),
  ).toBeEnabled();
}

for (const [prefix, width] of [
  ["", 1280],
  ["/bowhead", 320],
] as const) {
  test(`recovers the original confirmation without changing input at ${prefix || "root"} / ${width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    let published = false;
    const commands: unknown[] = [];
    const scopes: Record<string, string>[] = [];
    await page.route(
      "**/api/v1/sessions/s_doc/documents/djob_canvas_fixture",
      (route) => {
        scopes.push(route.request().headers());
        return route.fulfill({
          json: published ? fixture.published : fixture.review,
        });
      },
    );
    await page.route(
      "**/api/v1/documents/djob_canvas_fixture/confirm",
      async (route) => {
        commands.push(route.request().postDataJSON());
        if (commands.length === 1) {
          await route.abort("failed");
          return;
        }
        published = true;
        await route.fulfill({ json: fixture.published });
      },
    );
    await start(page, prefix);
    await page.getByRole("button", { name: "Confirm and publish" }).click();
    await expect(
      page.getByRole("button", { name: "Retry publishing" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Check status" }).click();
    await expect(
      page.getByRole("button", { name: "Retry publishing" }),
    ).toBeVisible();
    expect(commands).toHaveLength(1);
    await page
      .getByRole("button", { name: "Retry publishing" })
      .click();
    await expect(page.getByLabel("Published source")).toHaveText(
      "art_canvas_source",
    );
    expect(commands).toHaveLength(2);
    expect(commands[0]).toEqual(commands[1]);
    expect(
      scopes.every(
        (headers) =>
          headers["x-team-id"] === "team_doc" &&
          headers["x-space-id"] === "ws_doc",
      ),
    ).toBe(true);
    await expect(page.getByLabel("Unsent input")).toHaveValue("Keep my input");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath("published-adapter.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "Close view", exact: true }).click();
    await page
      .getByRole("button", { name: "Reopen view", exact: true })
      .click();
    await expect(page.getByTestId("document-published-card")).toBeVisible();
    expect(commands).toHaveLength(2);
  });
}

test("a background confirmation stays with its retained view and does not take focus", async ({
  page,
}) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let dispatched = false;
  await page.route(
    "**/api/v1/sessions/s_doc/documents/djob_canvas_fixture",
    (route) => route.fulfill({ json: fixture.review }),
  );
  await page.route(
    "**/api/v1/documents/djob_canvas_fixture/confirm",
    async (route) => {
      dispatched = true;
      await pending;
      await route.fulfill({ json: fixture.published });
    },
  );
  await start(page, "/bowhead");
  await page.getByRole("button", { name: "Confirm and publish" }).click();
  await expect.poll(() => dispatched).toBe(true);
  await page.getByRole("button", { name: "Switch view", exact: true }).click();
  await page.getByLabel("Unsent input").focus();
  release();
  await expect(page.getByLabel("Published source")).toHaveText(
    "art_canvas_source",
  );
  await expect(page.getByText("Another object is selected")).toBeVisible();
  await expect(page.getByLabel("Unsent input")).toBeFocused();
  await expect(page.getByLabel("Unsent input")).toHaveValue("Keep my input");
});
