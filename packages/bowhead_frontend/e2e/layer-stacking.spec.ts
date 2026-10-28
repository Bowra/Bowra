import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import { probeLayer } from "./_layer-probe";
import type {} from "./_layer-stacking-entry";

/**
 * Real-browser layer-contract verification (docs/design/组件统一-技术设计.md, “图层”): after opening, sample points inside the overlay rectangle with
 * elementFromPoint; hits must belong to the overlay itself. jsdom lacks stacking and hit testing, so lock these here.
 */
async function open(page: Page, scene: string) {
  await installReferenceComponent(page, "", "_layer-stacking-entry.tsx");
  await page.goto("/__reference_component_test");
  await expect(page.getByRole("heading", { name: "layer stacking" })).toBeVisible();
  await page.evaluate((value) => window.layerScene.show(value as never), scene);
}

test("Toast 在顶层确认框之上，且可以点", async ({ page }) => {
  await open(page, "toast-over-confirm");
  await expect(page.getByRole("dialog", { name: "删除报告" })).toBeVisible();
  await page.evaluate(() =>
    (window as unknown as { layerToast: (t: { kind: string; text: string }) => void }).layerToast({
      kind: "error",
      text: "删除失败，请重试",
    }),
  );
  const item = page.getByTestId("toast-region-assertive").locator("> *").first();
  await expect(item).toBeVisible();
  expect((await probeLayer(item)).covered).toEqual([]);
  await item.getByRole("button", { name: "关闭" }).click();
  await expect(item).toHaveCount(0);
});

test("顶层弹窗里打开的菜单不被弹窗压住", async ({ page }) => {
  await open(page, "menu-in-top-dialog");
  await page.getByTestId("menu-trigger").click();
  const menu = page.getByTestId("demo-menu");
  await expect(menu).toBeVisible();
  expect((await probeLayer(menu)).covered).toEqual([]);
});

test("弹窗开着时轮询到的发信确认压在它上面，且能点", async ({ page }) => {
  let decided = "";
  await page.route("**/api/v1/email-send-confirmations/**", async (route) => {
    decided = route.request().postData() ?? "";
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await page.route("**/api/v1/email-send-confirmations", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        confirmations: [{
          id: "esc_1", turn_id: "t_1", session_id: "s_1", to: "a@example.com", subject: "周报",
          body: "正文", expires_at: "2099-01-01T00:00:00Z",
          attachments: [{ artifact_id: "art_deck", filename: "销售报告.pptx", mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation", size: 2048 }],
        }],
      }),
    }),
  );
  await open(page, "email-confirm-over-dialog");
  const confirm = page.locator(".personal-mcp-confirmation");
  await expect(confirm).toBeVisible();
  await expect(confirm.getByRole("region", { name: "附件" })).toContainText("销售报告.pptx (2 KB)");
  expect((await probeLayer(confirm)).covered).toEqual([]);
  await confirm.getByRole("button", { name: "不发送" }).click();
  await expect.poll(() => decided).toContain("reject");
});

test("不在浮层树里的高层宿主（9999）里打开的普通弹窗也抬到它之上", async ({ page }) => {
  await open(page, "dialog-from-high-host");
  // WebKit does not focus clicked buttons; opening-point detection uses focus, so open with the keyboard.
  await page.getByTestId("open-nested").focus();
  await page.keyboard.press("Enter");
  const panel = page.locator(".settings-dialog");
  await expect(panel).toBeVisible();
  expect((await probeLayer(panel)).covered).toEqual([]);
});
