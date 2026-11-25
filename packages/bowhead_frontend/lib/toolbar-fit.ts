/**
 * Measured priority overflow for toolbars (narrow desktop layout design §3).
 *
 * A toolbar declares compression steps as tokens (`trust-short`, `export-menu`, …). `fitSteps` writes
 * them, one at a time and in order, until the toolbar's own `fits()` check holds; CSS selects each
 * step with `[data-fit~="token"]`. The result is always a prefix of the steps (after `first`), so
 * what gives way is predictable and the same widths always give the same layout.
 */

export type FitStep = string;

export const FIT_ATTRIBUTE = "data-fit";

/** Apply steps in order until `fits()` holds; return the tokens applied (`first`, then a prefix of `steps`). */
export function fitSteps(
  apply: (tokens: readonly FitStep[]) => void,
  fits: () => boolean,
  steps: readonly FitStep[],
  first: readonly FitStep[] = [],
): FitStep[] {
  const applied: FitStep[] = [...first];
  apply(applied);
  for (const step of steps) {
    if (fits()) break;
    applied.push(step);
    apply(applied);
  }
  return applied;
}

/** Write tokens to the element's `data-fit` attribute (removed when empty). */
export function writeFitTokens(el: Element, tokens: readonly FitStep[]): void {
  if (tokens.length) el.setAttribute(FIT_ATTRIBUTE, tokens.join(" "));
  else el.removeAttribute(FIT_ATTRIBUTE);
}

function isRendered(el: Element): boolean {
  const box = el.getBoundingClientRect();
  return box.width > 0 && box.height > 0;
}

/** Flex items of `row`: its element children, with `display: contents` children replaced by their own children. */
export function flexItems(row: Element): Element[] {
  const view = row.ownerDocument.defaultView;
  return Array.from(row.children).flatMap((child) =>
    view?.getComputedStyle(child).display === "contents" ? flexItems(child) : [child],
  );
}

/**
 * True when every rendered flex item of `row` sits on one line. Items matching `skip` (a deliberate
 * second row) are ignored. Items are on one line when each starts above the shortest item's bottom.
 */
export function itemsOnOneLine(row: Element | null | undefined, skip?: string): boolean {
  if (!row) return true;
  const items = flexItems(row).filter((item) => !(skip && item.matches(skip)) && isRendered(item));
  if (items.length < 2) return true;
  const boxes = items.map((item) => item.getBoundingClientRect());
  const firstBottom = Math.min(...boxes.map((box) => box.bottom));
  return boxes.every((box) => box.top < firstBottom - 2);
}

/** True when `el` does not paint past its own box horizontally. */
export function noOverflow(el: Element): boolean {
  return el.scrollWidth <= el.clientWidth + 1;
}
