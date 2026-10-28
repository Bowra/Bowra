import { expect, test } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

for (const width of [412, 1440]) {
  for (const scene of ["profile", "spaces", "reference-retention", "general"] as const) {
    test(`grouped settings keep usable form geometry: ${scene} at ${width}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      await mockAuthedApp(page, { subagentManagement: true });
      await page.route("**/api/v1/admin/spaces?**", (route) => route.fulfill({ json: { items: [], next_cursor: null } }));
      await page.route("**/api/v1/team-settings/reference-retention", (route) => route.fulfill({ json: { days: 30, version: 1 } }));
      await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", (route) =>
        route.fulfill({ json: { name: "Example Space", description: "", revision: 1 } }),
      );
      await page.route("**/api/v1/workspace-settings/ws_smoke", (route) => route.fulfill({ json: { settings: {} } }));
      await page.goto(scene === "profile" ? "/settings/profile/" : scene === "general" ? "/space/settings/general/" : `/team/settings/${scene}/`);
      const input = page.locator(
        scene === "profile"
          ? ".legacy-input-profile"
          : scene === "spaces"
            ? "#settings-team-spaces-new-name"
            : scene === "general"
              ? "#space-general-name"
              : "#reference-retention-days",
      );
      await expect(input).toBeVisible();
      const row = input.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ui-group-row ') or contains(concat(' ', normalize-space(@class), ' '), ' settings-field ')][1]",
      );
      const geometry = await row.evaluate((element) => {
        const control = element.querySelector("input")!.getBoundingClientRect();
        const label = element.querySelector("label, .ui-group-label")!.getBoundingClientRect();
        const bounds = element.getBoundingClientRect();
        const css = getComputedStyle(element);
        return {
          control: { left: control.left, right: control.right, top: control.top, width: control.width },
          label: { left: label.left, right: label.right, bottom: label.bottom },
          right: bounds.right,
          padding: css.paddingRight,
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
      expect(geometry.overflow).toBe(false);
      expect(geometry.padding).toBe(width < 768 ? "16px" : "24px");
      if (width < 768) {
        expect(geometry.control.top).toBeGreaterThanOrEqual(geometry.label.bottom);
        expect(Math.abs(geometry.control.left - geometry.label.left)).toBeLessThanOrEqual(1);
      } else {
        expect(geometry.control.left - geometry.label.right).toBeGreaterThanOrEqual(31);
      }
      if (scene === "reference-retention") {
        expect(geometry.control.width).toBeLessThanOrEqual(180);
        const gap = await page
          .locator(".legacy-retention-form")
          .evaluate((form) => form.getBoundingClientRect().top - form.previousElementSibling!.getBoundingClientRect().bottom);
        expect(gap).toBeGreaterThanOrEqual(24);
      } else if (scene === "profile" || scene === "general") {
        expect(Math.abs(geometry.right - geometry.control.right - Number.parseFloat(geometry.padding))).toBeLessThanOrEqual(1);
      }
      if (scene === "general") {
        const groupGap = await page
          .locator(".ui-ws-i18n-group")
          .evaluate((group) => group.getBoundingClientRect().top - document.querySelector(".ui-space-general-group")!.getBoundingClientRect().bottom);
        expect(groupGap).toBe(32);
      }
      await page.screenshot({ path: test.info().outputPath(`${scene}-${width}.png`), fullPage: true });
    });
  }
}
