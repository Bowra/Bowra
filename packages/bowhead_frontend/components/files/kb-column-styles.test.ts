import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const FRONTEND_ROOT = path.resolve(__dirname, "../..");
const GLOBALS_CSS = readFileSync(path.join(FRONTEND_ROOT, "app/globals.css"), "utf-8");
const UI_REFRESH_CSS = readFileSync(path.join(FRONTEND_ROOT, "styles/ui-refresh.css"), "utf-8");

/** Declarations of every rule whose selector list is exactly `selector` (comments stripped). */
function ruleBodies(css: string, selector: string): string[] {
  const source = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const bodies: string[] = [];
  const rule = /([^{}]+)\{([^{}]*)\}/g;
  for (let match = rule.exec(source); match; match = rule.exec(source)) {
    if (match[1].replace(/\s+/g, " ").trim() === selector) bodies.push(match[2]);
  }
  return bodies;
}

describe("Files page knowledge-base column style contract", () => {
  it("never cuts a state badge: no hidden overflow, ellipsis or forced single line", () => {
    const bodies = ruleBodies(GLOBALS_CSS, ".kbbadge");
    expect(bodies.length).toBeGreaterThan(0);
    for (const body of bodies) {
      expect(body).not.toMatch(/overflow\s*:\s*hidden/);
      expect(body).not.toMatch(/text-overflow/);
      expect(body).not.toMatch(/white-space\s*:\s*nowrap/);
    }
    expect(bodies.join("")).toMatch(/overflow-wrap\s*:\s*anywhere/);
  });

  it("lets the badge wrap in the badge system, which owns white-space for .kbbadge", () => {
    const [body] = ruleBodies(UI_REFRESH_CSS, ":root:root :where(.kbcol .kbbadge)");
    expect(body).toBeDefined();
    expect(body).toMatch(/--ui-badge-ws\s*:\s*normal/);
    expect(body).toMatch(/--ui-badge-h\s*:\s*auto/);
  });

  it("wraps the column links inside the column instead of spilling into size and date", () => {
    const [body] = ruleBodies(GLOBALS_CSS, ".kbcol a, .kbcol .kbcol-link");
    expect(body).toBeDefined();
    expect(body).not.toMatch(/white-space\s*:\s*nowrap/);
    expect(body).toMatch(/max-width\s*:\s*100%/);
    expect(body).toMatch(/overflow-wrap\s*:\s*anywhere/);
  });
});
