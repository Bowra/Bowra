/**
 * Report data tables: content-sized columns only when every row is rendered.
 *
 * A virtualized table (`is-virtual`, > 200 rows expanded) keeps only a window of rows in the DOM; with
 * `table-layout: auto` its columns were re-measured from that window and jumped while scrolling. The auto layout,
 * the unwrapped numbers and the text-column minimum must therefore exclude virtualized tables, which stay on fixed
 * layout with a stable per-column minimum width. jsdom cannot cascade, so the selectors are pinned from source.
 */
import { describe, expect, it } from "vitest";

import { parseRules, read, stripComments } from "./css-contract";

const RULES = parseRules(stripComments(read("styles/report-canvas.css")));
const decl = (body: string, prop: string) =>
  body.match(new RegExp(`(?:^|;|\\s)${prop}\\s*:\\s*([^;]+)`))?.[1]?.trim();

describe("report data table layout", () => {
  it("applies content-sized layout only to tables that render every row", () => {
    const auto = RULES.filter((r) => /v2-chart-table-el/.test(r.selector) && decl(r.body, "table-layout") === "auto");
    expect(auto.length).toBeGreaterThan(0);
    for (const rule of [
      ...auto,
      ...RULES.filter((r) => /v2-chart-table-el td/.test(r.selector)),
    ]) {
      expect(rule.selector).toContain(".v2-chart-table-scroll:not(.is-virtual)");
    }
  });

  it("keeps virtualized tables on fixed layout with a minimum width from the column count", () => {
    const virtual = RULES.find((r) => /\.v2-chart-table-scroll\.is-virtual\s*>\s*table\.v2-chart-table-el/.test(r.selector));
    expect(virtual).toBeDefined();
    expect(decl(virtual!.body, "table-layout")).toBe("fixed");
    expect(decl(virtual!.body, "min-width")).toContain("var(--chart-table-columns");
  });
});
