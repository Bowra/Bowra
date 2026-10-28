import { expect, test, type APIResponse, type Locator, type Page, type Route } from "@playwright/test";

async function openAppearanceSettings(page: Page): Promise<Locator> {
  const personalReady = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/theme/personal") &&
      response.status() === 200,
  );
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const mobile = (page.viewportSize()?.width ?? 1280) <= 720;
  const drawer = page.getByRole("dialog", { name: /导航抽屉|Navigation drawer/ });
  if (mobile) {
    await page.getByTestId("mobile-topbar-hamburger").click();
    await drawer.getByRole("button", { name: /账户|Account/ }).click();
  } else {
    await page
      .getByTestId("unified-sidebar")
      .getByRole("button", { name: /账户|Account/ })
      .click();
  }
  await page
    .locator('[data-user-menu="true"]')
    .getByRole("menuitem", { name: /^(设置|Settings)$/ })
    .click();

  await expect(page).toHaveURL(/\/settings\/profile\/?$/);
  // Settings sections are links in the settings sidebar; on narrow screens
  // that sidebar lives in the navigation drawer.
  if (mobile) {
    await page.getByTestId("mobile-topbar-hamburger").click();
    await drawer.getByRole("link", { name: /^(外观|Appearance)$/ }).click();
  } else {
    await page.getByRole("link", { name: /^(外观|Appearance)$/ }).click();
  }
  await expect(page).toHaveURL(/\/settings\/appearance\/?$/);
  await personalReady;
  const surface = page.getByTestId("personal-appearance-v3");
  await expect(surface).toBeVisible();
  return surface;
}

test("personal background and accent save independently from the real menu entry", async ({
  page,
}) => {
  let dialog = await openAppearanceSettings(page);

  const backgroundSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/theme/personal") &&
      response.request().method() === "PATCH" &&
      response.status() === 200,
  );
  await dialog
    .getByTestId("personal-appearance-background")
    .getByRole("button", { name: /纯净白|Pure white/ })
    .click();
  await backgroundSaved;
  const accentSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/theme/personal") &&
      response.request().method() === "PATCH" &&
      response.status() === 200,
  );
  await dialog
    .getByTestId("personal-appearance-accent")
    .getByRole("button", { name: /海蓝|Ocean/ })
    .click();
  await accentSaved;
  await expect(
    dialog.getByText(/所有更改已保存|All changes saved/),
  ).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(() => ({
        background: getComputedStyle(document.documentElement)
          .getPropertyValue("--c-bg")
          .trim(),
        accent: getComputedStyle(document.documentElement)
          .getPropertyValue("--c-accent")
          .trim(),
        themeColor: document.querySelector<HTMLMetaElement>(
          'meta[name="theme-color"]',
        )?.content,
      })),
    )
    .toMatchObject({
      background: "#FFFFFF",
      accent: "#2F6F84",
      themeColor: "#FFFFFF",
    });

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect
    .poll(() =>
      page.evaluate(() => ({
        background: getComputedStyle(document.documentElement)
          .getPropertyValue("--c-bg")
          .trim(),
        accent: getComputedStyle(document.documentElement)
          .getPropertyValue("--c-accent")
          .trim(),
      })),
    )
    .toEqual({ background: "#FFFFFF", accent: "#2F6F84" });

  dialog = await openAppearanceSettings(page);
  await expect(
    dialog
      .getByTestId("personal-appearance-background")
      .getByRole("button", { name: /纯净白|Pure white/ }),
  ).toHaveAttribute("aria-pressed", "true");
});

/**
 * Keep this page's text size off the shared mock server. Personal appearance is one mock-server record
 * read by every parallel test (`/theme/personal`, `/theme/effective`); saving "Extra large" there would
 * enlarge the root font size of unrelated tests until the reset ran. Reads still come from the server;
 * the text size this page saves lives only in this page's routes.
 */
async function isolatePersonalTypeScale(page: Page) {
  let typeScale: string | null = null;
  let version = 0;
  const forward = async (route: Route) => {
    const headers = { ...route.request().headers() };
    delete headers["if-none-match"];
    delete headers["if-match"];
    return route.fetch({ method: "GET", headers, postData: undefined });
  };
  // Reuse the server's headers (CORS, exposed ETag) but not its length: the overlaid body differs.
  const headersFor = (response: APIResponse, extra: Record<string, string> = {}) => {
    const headers = { ...response.headers(), ...extra };
    delete headers["content-length"];
    return headers;
  };
  const withTypeScale = (effective: Record<string, unknown>) =>
    typeScale === null ? effective : { ...effective, typeScale };

  await page.route("**/api/v1/theme/personal", async (route) => {
    const method = route.request().method();
    if (method !== "GET" && method !== "PATCH") return route.fallback();
    if (method === "PATCH") {
      const changes = (route.request().postDataJSON()?.changes ?? {}) as Record<string, unknown>;
      if (Object.keys(changes).some((key) => key !== "typeScale")) return route.fallback();
      typeScale = (changes.typeScale as string | null | undefined) ?? null;
      version += 1;
    }
    const response = await forward(route);
    const body = await response.json();
    const overrides = { ...body.overrides };
    if (typeScale === null) delete overrides.typeScale;
    else overrides.typeScale = typeScale;
    // Local row version, with an ETag in the canonical shape the client validates.
    const rowVersion = Number(body.rowVersion ?? 0) + version;
    const etag = `"personal-v3:${rowVersion.toString(16).padStart(64, "0")}"`;
    await route.fulfill({
      status: response.status(),
      headers: headersFor(response, { etag }),
      json: {
        ...body,
        rowVersion,
        overrides,
        effective: withTypeScale(body.effective),
        sources: { ...body.sources, typeScale: typeScale === null ? body.sources?.typeScale : "user" },
      },
    });
  });
  await page.route("**/api/v1/theme/effective", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const response = await forward(route);
    const body = await response.json();
    await route.fulfill({
      status: response.status(),
      headers: headersFor(response),
      json: {
        ...body,
        effective: withTypeScale(body.effective),
        source: { ...body.source, typeScale: typeScale === null ? body.source?.typeScale : "user" },
      },
    });
  });
}

test("text size scales the root font size live, after reload and on first paint", async ({
  page,
}) => {
  await isolatePersonalTypeScale(page);
  const rootPx = () =>
    page.evaluate(() =>
      Number.parseFloat(getComputedStyle(document.documentElement).fontSize),
    );
  const bodyPx = () =>
    page.evaluate(() =>
      Number.parseFloat(getComputedStyle(document.body).fontSize),
    );
  const personalReady = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/theme/personal") &&
      response.status() === 200,
  );
  await page.goto("/settings/appearance/", { waitUntil: "domcontentloaded" });
  await personalReady;
  const dialog = page.getByTestId("personal-appearance-v3");
  await expect(dialog).toBeVisible();
  expect(await rootPx()).toBe(16);
  expect(await bodyPx()).toBe(14);

  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/theme/personal") &&
      response.request().method() === "PATCH" &&
      response.status() === 200,
  );
  await dialog
    .getByTestId("personal-appearance-type-scale")
    .getByRole("radio", { name: /超大|Extra Large/ })
    .click();
  await saved;
  // Live: the root (and with it every rem size) scales without a reload; --fs-base lands on 16px, not 16 × 16/14.
  await expect.poll(rootPx).toBeCloseTo((16 * 16) / 14, 3);
  expect(await bodyPx()).toBeCloseTo(16, 3);

  // First paint after a reload: the pre-hydration script applies the saved size before the body exists.
  await page.addInitScript(() => {
    const record = () => {
      (window as unknown as { __firstBodyRootPx?: number }).__firstBodyRootPx ??= Number.parseFloat(
        getComputedStyle(document.documentElement).fontSize,
      );
    };
    if (document.body) record();
    else
      new MutationObserver((_, observer) => {
        if (!document.body) return;
        record();
        observer.disconnect();
      }).observe(document, { childList: true, subtree: true });
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  expect(
    await page.evaluate(() => (window as unknown as { __firstBodyRootPx?: number }).__firstBodyRootPx),
  ).toBeCloseTo((16 * 16) / 14, 3);
  await expect.poll(rootPx).toBeCloseTo((16 * 16) / 14, 3);
  await expect
    .poll(() => page.evaluate(() => document.documentElement.dataset.typeScale))
    .toBe("extra-large");

  // The shared mock server never saw the change, so parallel tests keep the default text size.
  const shared = await page.request.get(
    `http://127.0.0.1:${process.env.PLAYWRIGHT_API_PORT ?? "18080"}/api/v1/theme/personal`,
  );
  expect((await shared.json()).overrides?.typeScale).toBeUndefined();
});

test("overall palette saves atomically and imported JSON stays editable until apply", async ({
  page,
}) => {
  const dialog = await openAppearanceSettings(page);
  const palettes = dialog.getByTestId("personal-appearance-palettes");

  const presetRequest = page.waitForRequest(
    (request) =>
      request.url().endsWith("/api/v1/theme/personal") &&
      request.method() === "PATCH",
  );
  await palettes
    .getByRole("button", { name: /专业蓝|Professional blue/ })
    .click();
  expect((await presetRequest).postDataJSON()).toMatchObject({
    changes: {
      accent: { kind: "custom", seed: "#2563EB" },
      background: { kind: "custom", light: "#F8FAFC", dark: "#0F172A" },
    },
  });
  const compileResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/theme/personal/compile") &&
      response.status() === 200,
  );
  await palettes.locator('input[type="file"]').setInputFiles({
    name: "midnight-indigo.json",
    mimeType: "application/json",
    buffer: Buffer.from(
      JSON.stringify({
        version: 1,
        name: "Midnight indigo",
        accent: "#635BFF",
        background: { light: "#F6F9FC", dark: "#101323" },
      }),
    ),
  });
  await compileResponse;

  const imported = palettes.getByTestId("personal-palette-import-draft");
  await expect(
    imported.getByRole("heading", { name: "Midnight indigo" }),
  ).toBeVisible();
  await expect(imported.getByText(/配色可用|Palette ready/)).toBeVisible();

  const importedRequest = page.waitForRequest(
    (request) =>
      request.url().endsWith("/api/v1/theme/personal") &&
      request.method() === "PATCH",
  );
  await imported
    .getByRole("button", { name: /应用配色|Apply palette/ })
    .click();
  expect((await importedRequest).postDataJSON()).toMatchObject({
    changes: {
      accent: { kind: "custom", seed: "#635BFF" },
      background: { kind: "custom", light: "#F6F9FC", dark: "#101323" },
    },
  });
  await expect(imported).toHaveCount(0);
});

test("custom picker keeps incomplete Hex local and reset requires confirmation", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const dialog = await openAppearanceSettings(page);
  await expect(
    dialog.getByTestId("personal-appearance-palettes"),
  ).toContainText(/中性石墨|Neutral graphite/);
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true);

  await dialog
    .getByTestId("personal-appearance-accent")
    .getByRole("button", { name: /自定义强调色|Custom accent/ })
    .click();
  const picker = page.getByRole("dialog", {
    name: /自定义强调色|Custom accent/,
  });
  await expect(picker).toBeVisible();
  await expect
    .poll(() => picker.evaluate((element) => getComputedStyle(element).opacity))
    .toBe("1");
  const hex = picker.getByRole("textbox", { name: "Hex" });
  await hex.fill("#F");
  await expect(
    picker.getByRole("button", { name: /应用颜色|Apply color/ }),
  ).toBeDisabled();
  await picker
    .locator("footer")
    .getByRole("button", { name: /取消|Cancel/ })
    .click();
  await expect(picker).toHaveCount(0);

  await dialog
    .getByRole("button", { name: /清除个人外观|Clear personal appearance/ })
    .click();
  const confirmation = page.getByRole("dialog", {
    name: /清除个人外观|Clear personal appearance/,
  });
  await expect(confirmation).toContainText(
    /图表设置不会被清除|Chart settings won't be cleared/,
  );
  const resetSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/theme/personal") &&
      response.request().method() === "DELETE" &&
      response.status() === 200,
  );
  await confirmation
    .getByRole("button", { name: /确认清除|Confirm clear/ })
    .click();
  await resetSaved;
});

test("quick appearance controls lead the theme preview", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const dialog = await openAppearanceSettings(page);
  const palettes = dialog.getByTestId("personal-appearance-palettes");
  const mode = dialog.getByTestId("personal-appearance-mode");
  const accent = dialog.getByTestId("personal-appearance-accent");
  const background = dialog.getByTestId("personal-appearance-background");
  const preview = dialog.getByTestId("personal-appearance-preview");

  await expect(palettes.getByRole("heading", { level: 3 })).toBeInViewport();
  await expect(mode).toBeInViewport();
  const [accentBox, backgroundBox, previewBox] = await Promise.all([
    accent.boundingBox(),
    background.boundingBox(),
    preview.boundingBox(),
  ]);
  expect(accentBox).not.toBeNull();
  expect(backgroundBox).not.toBeNull();
  expect(previewBox).not.toBeNull();
  expect(backgroundBox!.y).toBeGreaterThan(accentBox!.y);
  expect(previewBox!.y).toBeGreaterThan(backgroundBox!.y);
});

test("desktop custom picker stays within the viewport at a modal scroll edge", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 700 });
  const dialog = await openAppearanceSettings(page);
  await dialog
    .getByRole("button", { name: /自定义页面底色|Custom page background/ })
    .click();
  const trigger = dialog
    .getByTestId("personal-appearance-background")
    .getByRole("button", { name: /深色页面底色|Dark page background/ });

  await trigger.evaluate((element) =>
    element.scrollIntoView({ block: "end", inline: "nearest" }),
  );
  await trigger.click();

  const picker = page.getByRole("dialog", {
    name: /深色页面底色|Dark page background/,
  });
  await expect(picker).toBeVisible();
  await picker.evaluate((element) =>
    Promise.all(element.getAnimations().map((animation) => animation.finished)),
  );
  const box = await picker.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.y).toBeGreaterThanOrEqual(12);
  expect(box!.y + box!.height).toBeLessThanOrEqual(688);
  await expect(
    picker.getByRole("heading", {
      name: /深色页面底色|Dark page background/,
    }),
  ).toBeInViewport();

  await picker.locator("footer").scrollIntoViewIfNeeded();
  await expect(picker.locator("footer")).toBeInViewport();
});
