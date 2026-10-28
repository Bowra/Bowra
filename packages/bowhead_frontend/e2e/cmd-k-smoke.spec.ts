import { expect, test } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * W11-11 e2e:Cmd+K (W2-8) — focus unified work search.
 */

test("Cmd+K focuses unified work search", async ({ page }) => {
  await mockAuthedApp(page);
  await page.goto("/");

  // SSR markup can be visible before client effects register the shortcut.
  await expect(
    page
      .getByTestId("unified-sidebar")
      .getByRole("button", { name: /账户|Account/ }),
  ).toHaveAttribute("title", "smoke@example.com");

  // In Compact mode the input is inside the flyout; the shortcut itself opens and focuses it.
  await page.keyboard.press("Meta+k");
  const search = page.locator(".unified-work-search input");
  await expect(search).toBeVisible();
  await expect(search).toBeFocused();
});
