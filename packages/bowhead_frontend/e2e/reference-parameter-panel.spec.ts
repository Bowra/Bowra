import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import type {} from "./_reference-parameter-panel-entry";
async function open(page: Page, locale: "en-US" | "zh-CN" = "en-US") {
  await installReferenceComponent(
    page,
    "",
    "_reference-parameter-panel-entry.tsx",
  );
  await page.goto("/__reference_component_test");
  await expect(page.locator("#open-parameters")).toBeVisible();
  await page.evaluate(
    (locale) => window.referenceParameterPanel.update({ locale }),
    locale,
  );
  await page.locator("#open-parameters").click();
  await expect(page.getByRole("dialog")).toBeVisible();
}
test("native focus, trap, IME, cancellation, defaults and explicit commit", async ({
  page,
  context,
}) => {
  await open(page);
  const company = page
    .getByLabel(/^Company/)
    .and(page.locator("input,textarea,select"));
  await expect(page.getByRole("dialog").locator("header")).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(
    page.getByRole("button", { name: "Apply Parameters" }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("dialog").locator("header")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(company).toBeFocused();
  const cdp = await context.newCDPSession(page);
  await cdp.send("Input.imeSetComposition", {
    text: "经营分析",
    selectionStart: 4,
    selectionEnd: 4,
  });
  await page.keyboard.press("Enter");
  expect(
    await page.evaluate(() => window.referenceParameterPanel.commits),
  ).toEqual([]);
  await cdp.send("Input.insertText", { text: "经营分析" });
  await page.keyboard.press("Enter");
  await expect(company).toHaveValue(/经营分析\n/);
  expect(
    await page.evaluate(() => window.referenceParameterPanel.keys),
  ).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator("#open-parameters")).toBeFocused();
  expect(
    await page.evaluate(() => window.referenceParameterPanel.commits),
  ).toEqual([]);
  expect(await page.evaluate(() => document.body.style.overflow)).toBe("");
  await page.locator("#open-parameters").click();
  await expect(company).toHaveValue("Example");
  await page
    .getByLabel(/^Years/)
    .and(page.locator("input,textarea,select"))
    .fill("1.");
  await expect(
    page.getByRole("button", { name: "Apply Parameters" }),
  ).toBeDisabled();
  await page
    .getByLabel(/^Years/)
    .and(page.locator("input,textarea,select"))
    .fill("2");
  await page
    .getByLabel(/^End Date/)
    .and(page.locator("input,textarea,select"))
    .fill("2024-02-29");
  await page.getByRole("button", { name: "Apply Parameters" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const commits = await page.evaluate(
    () => window.referenceParameterPanel.commits,
  );
  expect(commits).toHaveLength(1);
  expect(commits[0].parameters).toEqual({
    company: "Example",
    count: 2,
    date: "2024-02-29",
    enabled: false,
    period: "year",
    detail: { units: 3 },
  });
  await expect(page.locator("#open-parameters")).toBeFocused();
});
test("current owner changes remove private form values and restore the background", async ({
  page,
}) => {
  await open(page);
  await page
    .getByLabel(/^Company/)
    .and(page.locator("input,textarea,select"))
    .fill("Private edit");
  await page.evaluate(() =>
    window.referenceParameterPanel.update({
      scope: {
        userId: "u_test",
        teamId: "t_test",
        workspaceId: "ws_new",
        requestedSessionId: null,
        authSessionGeneration: "auth_test",
      },
    }),
  );
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(await page.content()).not.toContain("Private edit");
  expect(
    await page.evaluate(() => window.referenceParameterPanel.commits),
  ).toEqual([]);
  await expect(page.locator("#root")).not.toHaveAttribute("inert");
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.locator("#open-parameters").click();
  await expect(
    page.getByLabel(/^Company/).and(page.locator("input,textarea,select")),
  ).toHaveValue("Example");
});
for (const locale of ["zh-CN", "en-US"] as const) {
  for (const width of [320, 390, 412, 768, 1440]) {
    // Phone widths run as a touch device: controls are 44px on coarse pointers; fine pointers use the
    // control height tiers, which stay at or above the WCAG 2.2 minimum target size of 24px.
    const touch = width <= 412;
    test.describe(`${locale} ${width}px`, () => {
      test.use(touch ? { hasTouch: true, isMobile: true } : {});
      test(`parameter panel viewport, scroll, theme and a11y ${locale} ${width}px`, async ({
        page,
      }, info) => {
        await page.setViewportSize({ width, height: 740 });
        await open(page, locale);
        if (width === 390) {
          await page.evaluate(() => {
            for (const token of [
              "--fs-xs",
              "--fs-sm",
              "--fs-md",
              "--fs-base",
              "--fs-lg",
              "--fs-xl",
            ]) {
              const value = parseFloat(
                getComputedStyle(document.documentElement).getPropertyValue(
                  token,
                ),
              );
              document.documentElement.style.setProperty(token, `${value * 2}px`);
            }
          });
        }
        if (width === 412) {
          await page.emulateMedia({
            reducedMotion: "reduce",
            colorScheme: "dark",
          });
          await page.evaluate(() => {
            document.documentElement.dataset.theme = "dark";
          });
        }
        if (width === 320) await page.setViewportSize({ width, height: 420 });
        // Narrow screens open the panel as a bottom sheet: wait for its slide-in and for the sheet to pick up the
        // resized visual viewport before measuring.
        await page.getByRole("dialog").evaluate((node) =>
          Promise.all(
            (node.closest(".ui-dialog-sheet") ?? node)
              .getAnimations({ subtree: true })
              .map((animation) => animation.finished),
          ),
        );
        await expect
          .poll(async () => {
            const box = (await page.getByRole("dialog").boundingBox())!;
            return box.y >= 0 && box.y + box.height <= page.viewportSize()!.height;
          })
          .toBe(true);
        const bounds = await page.getByRole("dialog").boundingBox();
        expect(bounds!.x).toBeGreaterThanOrEqual(0);
        expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
        expect(bounds!.y).toBeGreaterThanOrEqual(0);
        expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(
          page.viewportSize()!.height,
        );
        expect(
          await page.evaluate(() => document.documentElement.scrollWidth),
        ).toBeLessThanOrEqual(width);
        for (const control of await page
          .getByRole("dialog")
          .locator("input,textarea,select,button")
          .all()) {
          expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(
            touch ? 44 : 24,
          );
        }
        const scroll = page.locator(".reference-parameter-panel__fields");
        await page
          .getByLabel(/^Note/)
          .and(page.locator("input,textarea,select"))
          .focus();
        await expect
          .poll(() => scroll.evaluate((node) => node.scrollTop))
          .toBeGreaterThan(0);
        await page.screenshot({ path: info.outputPath("panel.png") });
        await page.evaluate(axe.source);
        const violations = await page.evaluate(
          async () =>
            (
              await (window as unknown as { axe: typeof axe }).axe.run(
                document.querySelector('[role="dialog"]')!,
                {
                  runOnly: ["wcag2a", "wcag2aa", "wcag21aa"],
                },
              )
            ).violations,
        );
        expect(
          violations.map((v) => ({
            id: v.id,
            nodes: v.nodes.map((n) => n.target),
          })),
        ).toEqual([]);
      });
    });
  }
}
