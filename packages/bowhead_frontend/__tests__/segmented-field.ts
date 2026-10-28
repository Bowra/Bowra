import { fireEvent } from "@testing-library/react";

/** Test helpers for a SegmentedControl (role="radiogroup") that replaced a native select. */

function options(group: HTMLElement): HTMLButtonElement[] {
  return Array.from(group.querySelectorAll<HTMLButtonElement>('[role="radio"][data-composite-value]'));
}

/** The value of the checked segment, like a select's `value`. */
export function segmentedValue(group: HTMLElement): string | null {
  return options(group).find((option) => option.getAttribute("aria-checked") === "true")?.getAttribute("data-composite-value") ?? null;
}

/** Selects the segment carrying `value`, like changing a select. */
export function chooseSegment(group: HTMLElement, value: string): void {
  const option = options(group).find((item) => item.getAttribute("data-composite-value") === value);
  if (!option) throw new Error(`No segment with value ${value}`);
  fireEvent.click(option);
}

/** True when no segment can be chosen (disabled through the control or an enclosing fieldset). */
export function segmentDisabled(group: HTMLElement): boolean {
  return options(group).every((option) => option.matches(":disabled") || option.getAttribute("aria-disabled") === "true");
}
