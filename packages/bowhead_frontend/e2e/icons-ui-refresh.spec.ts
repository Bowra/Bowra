import { expect, test, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * P2 batch 2 (icons) in a real browser. Runs on Chromium and WebKit (CROSS_BROWSER_SPECS).
 * Interface icons are Lucide (components/icon-refresh.tsx), stroke 1.5, colour follows text, the centre of an
 * icon button hits the button itself, and the accessible name comes from the button.
 */

async function open(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockAuthedApp(page);
  await page.goto("/?scenario=causal");
  await expect(page.getByTestId("msg-action-copy")).toBeVisible({ timeout: 20_000 });
}

test("sidebar and message action icons are Lucide, 1.5 stroke, text colour, hittable, names unchanged", async ({ page }) => {
  await open(page);
  const sidebarIcons = page.locator(".unified-sidebar svg.lucide");
  await expect.poll(() => sidebarIcons.count()).toBeGreaterThan(3);
  const facts = await page.locator("svg.lucide").evaluateAll((els) =>
    els
      .map((el) => {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return { w: r.width, visible: r.width > 0 && cs.visibility !== "hidden", stroke: cs.stroke, color: cs.color, sw: cs.strokeWidth };
      })
      .filter((f) => f.visible),
  );
  expect(facts.length).toBeGreaterThan(10);
  for (const f of facts.filter((x) => x.w >= 13)) expect(f.sw).toBe("1.5px");
  for (const f of facts) expect(f.stroke).toBe(f.color);

  const copy = page.getByTestId("msg-action-copy");
  await expect(copy.locator("svg.lucide-copy")).toHaveCount(1);
  await expect(copy).toHaveAccessibleName(/复制|Copy/);
  const hit = await copy.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const h = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return Boolean(h && (h === el || el.contains(h)));
  });
  expect(hit).toBe(true);
});

