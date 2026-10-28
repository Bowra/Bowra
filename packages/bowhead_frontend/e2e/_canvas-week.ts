import { expect, type Page } from "@playwright/test";

/** Space preferences hosted in Canvas show the week start as a segmented control (role="radiogroup"). */
const LABELS = { monday: "周一", sunday: "周日" } as const;
type Week = keyof typeof LABELS;

export const canvasWeek = (page: Page) => page.getByRole("radiogroup", { name: "周起始日", exact: true });

export async function expectCanvasWeek(page: Page, value: Week, options?: { timeout?: number }) {
  await expect(canvasWeek(page).getByRole("radio", { name: LABELS[value], exact: true })).toHaveAttribute("aria-checked", "true", options);
}

export async function chooseCanvasWeek(page: Page, value: Week) {
  await canvasWeek(page).getByRole("radio", { name: LABELS[value], exact: true }).click();
}
