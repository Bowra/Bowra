import { expect, test, type Locator, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

/**
 * Question card layout by card width (styles/ui-refresh-cards.css, `clarification-card` container). The viewport
 * stays desktop-wide: the narrow card must come from the chat column (Canvas open), not from a phone media rule.
 * Measured 2026-11-23: a 325 card (narrowest chat column) stacks the question head and gives the primary action a
 * full-width last line; a 640 card keeps the one-line head and footer with the primary action at the right.
 */

type Box = { top: number; bottom: number; left: number; right: number; width: number };

async function open(page: Page, locale: string, width: number) {
  await page.setViewportSize({ width: 1280, height: 900 });
  await installReferenceComponent(page, "", "_clarification-card-entry.tsx");
  await page.goto(`/__reference_component_test?locale=${locale}&width=${width}`);
  await expect(page.locator("[data-case]").first()).toBeVisible();
}

function box(locator: Locator): Promise<Box> {
  return locator.evaluate((el) => {
    const r = el.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width };
  });
}

function footer(page: Page, id: string) {
  return page.locator(`[data-case="${id}"] .ui-card__footer`);
}

test("a narrow question card stacks the question head and gives Continue its own full-width line", async ({ page }) => {
  await open(page, "en-US", 325);

  const head = page.locator('[data-case="multi"] .ui-card-question__head').first();
  const [headBox, label, text, type] = await Promise.all([
    box(head), box(head.locator("b")), box(head.locator(".ui-card-question__text")), box(head.locator(".ui-card-question__type")),
  ]);
  expect(text.top, "question below 'Question 1'").toBeGreaterThanOrEqual(label.bottom - 1);
  expect(text.width, "question spans the head").toBeCloseTo(headBox.width, 0);
  expect(type.top, "type beside 'Question 1'").toBeCloseTo(label.top, -1);
  expect(type.right).toBeCloseTo(headBox.right, 0);

  for (const id of ["single", "multi-answered", "remember", "document"]) {
    const row = footer(page, id);
    const rowBox = await box(row);
    const buttons = row.locator("> button");
    const primary = await box(buttons.last());
    expect(primary.width, `${id}: Continue spans the footer`).toBeCloseTo(rowBox.width, 0);
    for (const secondary of await buttons.all()) {
      const b = await box(secondary);
      if (b.top === primary.top) continue;
      expect(b.bottom, `${id}: secondary actions above Continue`).toBeLessThanOrEqual(primary.top);
      expect(b.right, `${id}: inside the card`).toBeLessThanOrEqual(rowBox.right + 0.5);
    }
  }
  const remember = await box(footer(page, "remember").locator(".ui-card-check"));
  const cancel = await box(footer(page, "remember").getByRole("button", { name: "Cancel" }));
  expect(cancel.top, "Remember my choice on its own line").toBeGreaterThanOrEqual(remember.bottom - 1);
});

test("the longest footer label wraps inside its button instead of leaving the narrow card", async ({ page }) => {
  await open(page, "de-DE", 325);
  for (const id of ["single", "remember", "document", "pending"]) {
    const row = footer(page, id);
    const rowBox = await box(row);
    for (const button of await row.locator("> button").all()) {
      expect((await box(button)).right, id).toBeLessThanOrEqual(rowBox.right + 0.5);
    }
  }
});

test("a wide question card keeps one-line heads and footers with Continue at the right", async ({ page }) => {
  await open(page, "en-US", 640);

  const head = page.locator('[data-case="multi"] .ui-card-question__head').first();
  const [label, text] = await Promise.all([box(head.locator("b")), box(head.locator(".ui-card-question__text"))]);
  expect(text.left, "question beside 'Question 1'").toBeGreaterThan(label.right);

  for (const id of ["single", "multi-answered", "remember"]) {
    const row = footer(page, id);
    const rowBox = await box(row);
    const buttons = await Promise.all((await row.locator("> button").all()).map(box));
    const primary = buttons[buttons.length - 1];
    expect(new Set(buttons.map((b) => Math.round(b.top))).size, `${id}: one footer line`).toBe(1);
    expect(primary.right, `${id}: Continue at the right`).toBeCloseTo(rowBox.right, 0);
    expect(primary.width, `${id}: Continue keeps its own width`).toBeLessThan(rowBox.width / 2);
  }
});
