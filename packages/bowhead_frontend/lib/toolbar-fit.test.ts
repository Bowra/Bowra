import { describe, expect, it } from "vitest";

import { FIT_ATTRIBUTE, fitSteps, flexItems, itemsOnOneLine, noOverflow, writeFitTokens } from "./toolbar-fit";

/** Give an element a fixed layout box (jsdom has no layout). */
function place(el: Element, top: number, height = 20, width = 40) {
  el.getBoundingClientRect = () =>
    ({ top, bottom: top + height, left: 0, right: width, width, height, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;
}

function row(...tops: number[]) {
  const parent = document.createElement("div");
  for (const top of tops) {
    const child = document.createElement("span");
    place(child, top);
    parent.append(child);
  }
  document.body.append(parent);
  return parent;
}

describe("fitSteps", () => {
  it("applies nothing when the toolbar already fits", () => {
    const writes: string[][] = [];
    const tokens = fitSteps((t) => writes.push([...t]), () => true, ["a", "b"]);
    expect(tokens).toEqual([]);
    expect(writes).toEqual([[]]);
  });

  it("applies steps in order and stops at the first that fits", () => {
    let applied: readonly string[] = [];
    const tokens = fitSteps((t) => { applied = t; }, () => applied.includes("b"), ["a", "b", "c"]);
    expect(tokens).toEqual(["a", "b"]);
  });

  it("returns every step when nothing fits, never skipping or reordering", () => {
    const tokens = fitSteps(() => {}, () => false, ["a", "b", "c"]);
    expect(tokens).toEqual(["a", "b", "c"]);
  });

  it("starts from `first` and resets earlier tokens", () => {
    const writes: string[][] = [];
    let applied: readonly string[] = [];
    const tokens = fitSteps(
      (t) => { applied = t; writes.push([...t]); },
      () => applied.length >= 2,
      ["a", "b"],
      ["mode-row"],
    );
    expect(tokens).toEqual(["mode-row", "a"]);
    expect(writes[0]).toEqual(["mode-row"]);
  });
});

describe("writeFitTokens", () => {
  it("writes space-separated tokens and removes the attribute when empty", () => {
    const el = document.createElement("div");
    writeFitTokens(el, ["trust-short", "export-icon"]);
    expect(el.getAttribute(FIT_ATTRIBUTE)).toBe("trust-short export-icon");
    expect(el.matches('[data-fit~="export-icon"]')).toBe(true);
    writeFitTokens(el, []);
    expect(el.hasAttribute(FIT_ATTRIBUTE)).toBe(false);
  });
});

describe("itemsOnOneLine", () => {
  it("accepts items on one line and rejects a wrapped item", () => {
    expect(itemsOnOneLine(row(10, 12, 8))).toBe(true);
    expect(itemsOnOneLine(row(10, 12, 40))).toBe(false);
  });

  it("ignores items matching the skip selector (a deliberate second row)", () => {
    const parent = row(10, 12);
    const hint = document.createElement("p");
    hint.className = "hint";
    place(hint, 40);
    parent.append(hint);
    expect(itemsOnOneLine(parent)).toBe(false);
    expect(itemsOnOneLine(parent, ".hint")).toBe(true);
  });

  it("ignores items without a box and treats a missing row as fitting", () => {
    const parent = row(10);
    const hidden = document.createElement("span");
    place(hidden, 80, 0, 0);
    parent.append(hidden);
    expect(itemsOnOneLine(parent)).toBe(true);
    expect(itemsOnOneLine(null)).toBe(true);
  });

  it("flattens display: contents children into the row's items", () => {
    const parent = row(10);
    const group = document.createElement("div");
    group.style.display = "contents";
    const inner = document.createElement("button");
    place(inner, 40);
    group.append(inner);
    parent.append(group);
    expect(flexItems(parent)).toContain(inner);
    expect(itemsOnOneLine(parent)).toBe(false);
  });
});

describe("noOverflow", () => {
  it("compares scroll width with client width", () => {
    const el = document.createElement("div");
    Object.defineProperty(el, "clientWidth", { value: 300 });
    Object.defineProperty(el, "scrollWidth", { value: 301, configurable: true });
    expect(noOverflow(el)).toBe(true);
    Object.defineProperty(el, "scrollWidth", { value: 320 });
    expect(noOverflow(el)).toBe(false);
  });
});
