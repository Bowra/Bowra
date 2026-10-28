/**
 * Wide tables expose overflow instead of displaying partially clipped values.
 *
 * Screen: while columns lie past the right edge the scroll box carries `has-more-right` (the edge fades, the first
 * column turns sticky), so a cell cut by the boundary never reads as a whole number. Export: a screenshot cannot
 * scroll, so a too-wide table is zoomed down to fit, never below EXPORT_MIN_ZOOM (0.6).
 *
 * jsdom has no layout; the scroll box's and table's widths are stubbed per test.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChartTable } from "./chart-table";
import type { ChartSpec } from "@/lib/chart/types";

vi.mock("next-intl", () => ({
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string, values?: Record<string, unknown>) =>
    key === "hidden_columns" ? `${values?.n} more columns not shown` : key,
}));

const SPEC: ChartSpec = {
  kind: "table",
  title: "宽表",
  row_count: 2,
  columns: [
    { key: "shop", header: "门店", type: "categorical" },
    { key: "a", header: "1月", type: "numeric" },
    { key: "b", header: "2月", type: "numeric" },
  ],
  rows: [
    { shop: "S1", a: 1_608_444_631, b: 2_543_163_033 },
    { shop: "S2", a: 1_129_192_226, b: 1_650_185_314 },
  ],
};

/** Stub layout: the scroll box is `box` px wide, its content (the table) `content` px wide. */
function stubWidths(box: number, content: number) {
  const isScroll = (el: Element) => el.classList.contains("v2-chart-table-scroll");
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
    return isScroll(this) ? box : 0;
  });
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
    if (this.tagName === "TABLE") {
      const zoom = Number((this as HTMLElement).style.zoom || 1);
      return content * zoom;
    }
    return isScroll(this) ? content * Number(this.querySelector("table")?.style.zoom || 1) : 0;
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const scrollBox = (c: HTMLElement) => c.querySelector(".v2-chart-table-scroll") as HTMLElement;
const table = (c: HTMLElement) => c.querySelector("table") as HTMLTableElement;

describe("ChartTable horizontal overflow", () => {
  it("screen: marks the edge with hidden columns", () => {
    stubWidths(400, 900);
    const { container } = render(<ChartTable spec={SPEC} surface="canvas" />);
    expect(scrollBox(container)).toHaveClass("has-more-right");
    expect(table(container).style.zoom).toBe("");
  });

  it("screen: a table that fits carries no marker", () => {
    stubWidths(900, 400);
    const { container } = render(<ChartTable spec={SPEC} surface="canvas" />);
    expect(scrollBox(container)).not.toHaveClass("has-more-right");
  });

  it("export: zooms a too-wide table down to fit the frame instead of cropping columns", () => {
    stubWidths(1000, 1500);
    const { container } = render(<ChartTable spec={SPEC} surface="export" />);
    expect(Number(table(container).style.zoom)).toBeCloseTo(1000 / 1500);
    expect(scrollBox(container)).not.toHaveClass("has-more-right");
  });

  it("export: never shrinks below 0.6 — a still-wider table keeps the faded edge as its marker", () => {
    stubWidths(1000, 3000);
    const { container } = render(<ChartTable spec={SPEC} surface="export" />);
    expect(table(container).style.zoom).toBe("0.6");
    expect(scrollBox(container)).toHaveClass("has-more-right");
  });

  it("export: re-fitting after new rows measures the table at its own size, not the already-zoomed one", () => {
    stubWidths(1000, 1500);
    const { container, rerender } = render(<ChartTable spec={SPEC} surface="export" />);
    const more: ChartSpec = { ...SPEC, row_count: 3, rows: [...SPEC.rows!, { shop: "S3", a: 1, b: 2 }] };
    rerender(<ChartTable spec={more} surface="export" />);
    expect(Number(table(container).style.zoom)).toBeCloseTo(1000 / 1500);
  });

  it("export: a table that fits is left at its own size", () => {
    stubWidths(1000, 800);
    const { container } = render(<ChartTable spec={SPEC} surface="export" />);
    expect(table(container).style.zoom).toBe("");
  });

  it("export: names the columns still past the frame at the minimum zoom instead of cutting them silently", () => {
    stubWidths(1000, 3000);
    // Frame right edge at 1000; header cells end at 400, 900 and 1500 (the last one is past the frame).
    const rights = [400, 900, 1500];
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const right = this.classList.contains("v2-chart-table-scroll")
        ? 1000
        : this.tagName === "TH"
          ? rights[[...this.parentElement!.children].indexOf(this)] ?? 0
          : 0;
      return { right, left: 0, top: 0, bottom: 0, width: right, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
    });
    const { getByTestId } = render(<ChartTable spec={SPEC} surface="export" />);
    expect(getByTestId("table-hidden-columns")).toHaveTextContent("1 more columns not shown");
  });

  it("export: a table that fits after zooming has no hidden-columns note", () => {
    stubWidths(1000, 1500);
    const { queryByTestId } = render(<ChartTable spec={SPEC} surface="export" />);
    expect(queryByTestId("table-hidden-columns")).toBeNull();
  });
});
