/** C7 change row: before/after labels (Updated, Suggested, Vorgeschlagen) never break mid-word. */
import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const CSS = readFileSync(path.join(__dirname, "canvas-object.module.css"), "utf-8");

function rule(selector: string): string {
  const start = CSS.indexOf(`\n${selector} {`);
  expect(start, `missing rule ${selector}`).toBeGreaterThanOrEqual(0);
  return CSS.slice(start, CSS.indexOf("}", start));
}

describe("ChangeRow label column style contract", () => {
  it("sizes the label column to its content instead of a fixed width", () => {
    const values = rule(".changeValues");
    expect(values).toContain("grid-template-columns: fit-content(40%) minmax(0, 1fr);");
    expect(values).not.toMatch(/grid-template-columns:\s*\d+px/);
  });

  it("lets labels wrap only between words, overriding the surface's overflow-wrap: anywhere", () => {
    const label = rule(".changeValues dt");
    expect(label).toContain("overflow-wrap: normal;");
    expect(label).toContain("word-break: normal;");
    expect(label).toContain("min-width: 44px;");
  });
});
