import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { calendarAxisTickLabels, formatAxisTick, formatDisplayValue, formatObservedValue, formatObservedPercentCell, resolveAxisMagnitude, resolveDisplayMagnitude, resolvePercentColumn, resolvePercentScale } from "./format";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import { checkKindUnits } from "./kind-shape";
import type { ChartSpec } from "./types";

describe("shared reader-facing display standard", () => {
  let prior: string;
  beforeEach(() => { prior = document.documentElement.lang; document.documentElement.lang = "zh-CN"; });
  afterEach(() => { document.documentElement.lang = prior; });

  test.each([[1000.125, "1,000"], [10000, "10,000"], [10001, "1万"], [90000, "9万"], [100000001, "1亿"], [1421464192.74227, "14.21亿"]])("Card magnitude for %s", (value, expected) => {
    expect(formatDisplayValue(Number(value), resolveDisplayMagnitude([value]))).toBe(expected);
  });
  test("axis span and generated tick positions are separate from Card magnitude", () => {
    const close = resolveAxisMagnitude([100010, 100015], "元");
    expect(close.unit).toBe("元");
    expect(formatAxisTick(100012.5, close, [100010, 100012.5, 100015])).toBe("100,012.5");
    const ticks = resolveAxisMagnitude([0, 10]);
    expect(formatAxisTick(2.5, ticks, [0, 2.5, 5, 7.5, 10])).toBe("2.5");
    expect(formatAxisTick(1e-9, resolveAxisMagnitude([0, 2e-9]), [0, 1e-9, 2e-9])).toBe("1E-9");
    expect(formatAxisTick(1e-15, resolveAxisMagnitude([0, 2e-15]), [0, 1e-15, 2e-15])).toBe("1E-15");
  });
  test("fixed/max precision wins defaults without changing stored values", () => {
    expect(formatObservedValue(1.2, "USD", "currency", true, { decimal_places: 3, decimal_mode: "fixed" })).toBe("USD 1.200");
    expect(formatObservedValue(-0.0001, undefined, undefined, true, { decimal_places: 2, decimal_mode: "fixed" })).toBe("0.00");
    expect(formatObservedPercentCell(0.0000035, "fraction", { decimal_places: 2, decimal_mode: "max" })).toBe("0%");
    expect(formatObservedValue(1.23456, "USD", "float")).toBe("USD 1.2346");
  });
  test("names and overlapping ranges never establish a percentage shape", () => {
    expect(resolvePercentColumn({ header: "Growth rate", format: "float" })).toBe(false);
    expect(resolvePercentColumn({ header: "Revenue", percent_scale: "fraction" })).toBe(true);
    expect(resolvePercentScale([0.35, 0.8], { unit: "%" })).toBe("points");
    expect(formatObservedPercentCell(13.5, "fraction")).toBe("1,350%");
  });
  test("compatible units share actual geometry and preserve source rows", () => {
    const spec: ChartSpec = { kind: "bar", title: "Amounts", row_count: 1, columns: [{ key: "name", header: "Name", type: "categorical" }, { key: "a", header: "A", type: "numeric", unit: "万元" }, { key: "b", header: "B", type: "numeric", unit: "元" }], rows: [{ name: "A", a: 1, b: 9000 }] };
    expect(checkKindUnits(spec, "bar")).toEqual([]);
    expect(normalizeChartForRenderingV1(spec).spec.rows?.[0]).toEqual({ name: "A", a: 10000, b: 9000 });
    expect(spec.rows?.[0]?.a).toBe(1);
    expect(checkKindUnits({ ...spec, columns: spec.columns.map((c) => c.key === "b" ? { ...c, unit: "USD" } : c) }, "bar")).toHaveLength(1);
  });
  test.each(["zh-CN", "en-US", "de-DE", "fr-FR"])("calendar boundaries and repeated hours retain meaning in %s", (locale) => {
    const year = calendarAxisTickLabels(["2025-12-31", "2026-01-01"], "", locale)!;
    expect(year[0]).toContain("2025"); expect(year[1]).toContain("2026");
    const hours = calendarAxisTickLabels(["2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00"], "2026-11-01", locale)!;
    expect(hours[0]).toContain("-04:00"); expect(hours[1]).toContain("-05:00");
    expect(calendarAxisTickLabels(["2026-02-30"], "", locale)).toBeNull();
    expect(calendarAxisTickLabels(["Month 2", "Month 3"], "", locale)).toBeNull();
  });
});
