import { afterEach, expect, it } from "vitest";
import { qualifyTableUnitText, tableColumnDisplays } from "./table-display";
import type { ChartColumn } from "./types";

afterEach(() => { document.documentElement.lang = ""; });

it.each(["zh-CN", "en-US", "de-DE", "fr-FR"])("keeps values, headers and disclosure in the same unit in %s", (locale) => {
  document.documentElement.lang = locale;
  const columns: ChartColumn[] = [{ key: "revenue", header: "Revenue (万元)", type: "numeric", unit: "万元" }];
  const rows = [{ revenue: 120.5 }, { revenue: 0 }, { revenue: -0.0009 }, { revenue: null }];
  const original = JSON.stringify({ columns, rows });
  const display = tableColumnDisplays(columns, rows, locale).get("revenue")!;
  expect(display.header).toBe("Revenue (元)");
  expect(display.displayUnit).toBe("元");
  expect(display.sourceUnit).toBe("万元");
  expect(display.note).toBe("conversion");
  expect(display.formatValue(120.5)).toBe(new Intl.NumberFormat(locale).format(1205000));
  expect(display.formatValue(-0.0009)).toBe(new Intl.NumberFormat(locale).format(-9));
  expect(display.formatValue(0)).toBe("0");
  expect(display.formatValue(null)).toBe("—");
  const source = tableColumnDisplays(columns, rows, locale, true).get("revenue")!;
  expect(source.formatValue(120.5)).toBe("120.5");
  expect(source.header).toBe("Revenue (万元)");
  expect(source.note).toBe("source");
  expect(JSON.stringify({ columns, rows })).toBe(original);
});

it("preserves collision units and declared percentage semantics", () => {
  document.documentElement.lang = "en-US";
  const columns: ChartColumn[] = [
    { key: "a", header: "Revenue (亿元)", type: "numeric", unit: "亿元" },
    { key: "b", header: "Revenue (万元)", type: "numeric", unit: "万元" },
    { key: "rate", header: "Rate", type: "numeric", unit: "%", percent_scale: "fraction" },
    { key: "pp", header: "Change", type: "numeric", unit: "pp" },
    { key: "custom", header: "Custom", type: "numeric", unit: "Prepared units" },
  ];
  const displays = tableColumnDisplays(columns, [{ a: 0.0009, b: 9, rate: 0.125, pp: 2, custom: 4 }], "en-US");
  expect(displays.get("a")!.formatValue(0.0009)).toBe("0.0009");
  expect(displays.get("b")!.note).toBeUndefined();
  expect(displays.get("rate")!.formatValue(0.125)).toBe("12.5%");
  expect(displays.get("pp")!.displayUnit).toBe("pp");
  expect(displays.get("custom")!.displayUnit).toBe("Prepared units");
  expect(displays.get("custom")!.note).toBeUndefined();
  expect(qualifyTableUnitText("Revenue (万元)", displays.values(), (unit) => `Source unit: ${unit}`)).toBe("Revenue (万元)");
});

it("uses a known currency identity consistently without inferring an ambiguous symbol", () => {
  const column: ChartColumn = { key: "amount", header: "Amount ($M)", type: "numeric", unit: "$M", currency_code: "CAD" };
  const display = tableColumnDisplays([column], [], "en-US").get("amount")!;
  expect(display.header).toBe("Amount (CAD)");
  expect(display.displayUnit).toBe("CAD");
  expect(tableColumnDisplays([{ ...column, currency_code: undefined }], [], "en-US").get("amount")!.displayUnit).toBe("$");
});

it("qualifies only isolated source-unit markers proven by the displayed columns", () => {
  const displays = tableColumnDisplays([{ key: "amount", header: "Revenue", type: "numeric", unit: "万元" }], [], "en-US");
  const label = (unit: string) => `Source unit: ${unit}`;
  expect(qualifyTableUnitText("单位：万元；synthetic values", displays.values(), label)).toBe("Source unit: 万元；synthetic values");
  expect(qualifyTableUnitText("Revenue (万元)", displays.values(), label)).toBe("Revenue (Source unit: 万元)");
  for (const text of ["The axis uses 万元 without conversion", "纵轴单位：万元；未换算", "单位：亿元", "Revenue (excluding tax)", "万亿计划"]) {
    expect(qualifyTableUnitText(text, displays.values(), label)).toBe(text);
  }
  const source = tableColumnDisplays([{ key: "amount", header: "Revenue", type: "numeric", unit: "万元" }], [], "en-US", true);
  expect(qualifyTableUnitText("单位：万元", source.values(), label)).toBe("单位：万元");
});
