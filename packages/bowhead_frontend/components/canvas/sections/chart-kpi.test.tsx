/**
 * KPI main-value truncation (design "KPI 主值截断 - 四条出路", option 1c + 1d fallback).
 *
 * Before: `minmax(120px, 1fr)` tracks with a 22px main value and no overflow rule, so long values were painted under the
 * neighbouring card's opaque background, making a clipped number look complete.
 * Contract pinned here:
 *   - from KPI_APPROX_THRESHOLD the approximate magnitude is the main line and the exact value the secondary line;
 *   - below it (and for percentages) the exact value stays the main line with no secondary line;
 *   - the exact value is never dropped;
 *   - the width estimate behind the CSS fit fallback never under-reports a measured browser width.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ChartKPI } from "./chart-kpi";
import { kpiMagnitude, kpiValueEms, unitCarriesMagnitude } from "@/lib/chart/format";
import type { ChartSpec } from "@/lib/chart/types";

function kpiSpec(values: Record<string, number>, extra: Partial<Record<string, object>> = {}): ChartSpec {
  return {
    kind: "kpi",
    title: "KPI",
    row_count: 1,
    rows: [values],
    columns: Object.keys(values).map((key) => ({ key, header: key, type: "numeric" as const, ...(extra[key] ?? {}) })),
  };
}

function cell(header: string): HTMLElement {
  return screen.getByText(header).closest(".v2-chart-kpi-cell") as HTMLElement;
}

describe("ChartKPI main/secondary swap", () => {
  beforeEach(() => {
    document.documentElement.lang = "zh-CN";
  });
  afterEach(() => {
    cleanup();
    document.documentElement.lang = "";
  });

  it("uses the Card magnitude and an ordinary secondary readout", () => {
    render(<ChartKPI spec={kpiSpec({ 净营业额: 61234567, 退款额: -7123456 })} />);
    const net = cell("净营业额");
    expect(net.querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^6,123万$/);
    expect(net.querySelector(".v2-chart-kpi-exact")).toHaveTextContent(/^61,234,567$/);
    const refund = cell("退款额");
    expect(refund.querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^-712.35万$/);
    expect(refund.querySelector(".v2-chart-kpi-exact")).toHaveTextContent(/^-7,123,456$/);
  });

  it("uses the locale compact scale below one million", () => {
    render(<ChartKPI spec={kpiSpec({ 客流: 999999 })} />);
    const c = cell("客流");
    expect(c.querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^100万$/);
    expect(c.querySelector(".v2-chart-kpi-exact")).toHaveTextContent("999,999");
  });

  it("never swaps percentages, however large", () => {
    // A percentage has no compact magnitude: `≈250万%` would be nonsense. The value is deliberately past the threshold
    // so the guard, not the size, is what keeps it exact.
    render(<ChartKPI spec={kpiSpec({ 环比增长率: 2500000 }, { 环比增长率: { unit: "%" } })} />);
    const c = cell("环比增长率");
    expect(c.querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^2,500,000%$/);
    expect(c.querySelector(".v2-chart-kpi-exact")).toBeNull();
  });

  it("rounds declared percentages without multiplication tails", () => {
    render(<ChartKPI spec={kpiSpec({ margin: 0.1987352816409153 }, { margin: { format: "percent", percent_scale: "fraction" } })} />);
    expect(cell("margin").querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^19\.87%$/);
  });

  it("hands the CSS fit fallback the width of the string it actually renders", () => {
    render(<ChartKPI spec={kpiSpec({ GMV: 12345678901 })} />);
    const value = cell("GMV").querySelector(".v2-chart-kpi-value") as HTMLElement;
    expect(value.style.getPropertyValue("--kpi-ems")).toBe(String(kpiValueEms("123.46亿")));
  });
});

describe("kpiMagnitude", () => {
  afterEach(() => {
    document.documentElement.lang = "";
  });

  it("drops the decimal once the mantissa has three integer digits", () => {
    document.documentElement.lang = "zh-CN";
    expect(kpiMagnitude(61234567)).toBe("6123万");
    expect(kpiMagnitude(12345678901)).toBe("123亿");
    expect(kpiMagnitude(1234567890)).toBe("12.3亿");
    expect(kpiMagnitude(-234567890)).toBe("-2.3亿");
  });

  it("follows the interface locale", () => {
    document.documentElement.lang = "en-US";
    expect(kpiMagnitude(61234567)).toBe("61.2M");
    expect(kpiMagnitude(234567890)).toBe("235M");
    document.documentElement.lang = "de-DE";
    expect(kpiMagnitude(12345678901)).toBe("12,3 Mrd.");
  });
});

describe("kpiValueEms", () => {
  // Widest measured rendering at 22px (Inter 600, tabular-nums; max of Chromium and WebKit) of strings the
  // synthetic numeric shapes cover. Tabular digits preserve measured widths across digit substitutions. The estimate must never be narrower, or the fallback would let them overflow again.
  const MEASURED_PX_AT_22: Array<[string, number]> = [
    ["≈-20,0 Mio.", 124.7],
    ["≈-234 Mio.", 119.0],
    ["≈12,3 Mrd.", 113.5],
    ["≈61,2 Mio.", 110.7],
    ["≈-2345万", 104.7],
    ["-123,456", 103.8],
    ["¥999,999", 102.0],
    ["≈-20,0 M", 100.3],
    ["≈6123万", 90.7],
    ["-12,345", 89.8],
  ];

  it.each(MEASURED_PX_AT_22)("does not under-report %s", (text, px) => {
    expect(kpiValueEms(text) * 22).toBeGreaterThanOrEqual(px);
  });

  it("measures by character class, not character count", () => {
    // Same length (8), very different widths: grouping commas are a fraction of a digit, CJK a full em.
    expect(kpiValueEms("1,2,3,4,")).toBeLessThan(kpiValueEms("12345678"));
    expect(kpiValueEms("营业额营业额营业")).toBeGreaterThan(kpiValueEms("12345678"));
  });
});

describe("ChartKPI units and locale grouping (review follow-ups)", () => {
  afterEach(() => {
    cleanup();
    document.documentElement.lang = "";
  });

  it("keeps the unit on the magnitude line", () => {
    document.documentElement.lang = "zh-CN";
    render(
      <ChartKPI
        spec={kpiSpec(
          { 利润: 8123456, 退款额: -7123456, 客流: 12345678 },
          { 利润: { format: "currency", unit: "¥" }, 退款额: { unit: "¥" }, 客流: { unit: "人次" } },
        )}
      />,
    );
    expect(cell("利润").querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^¥812.35万$/);
    expect(cell("退款额").querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^-¥712.35万$/);
    expect(cell("客流").querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^1,235万人次$/);
    expect(cell("客流").querySelector(".v2-chart-kpi-exact")).toHaveTextContent(/^12,345,678人次$/);
  });

  it("normalizes a known stored magnitude without stacking multipliers", () => {
    document.documentElement.lang = "zh-CN";
    render(<ChartKPI spec={kpiSpec({ 营业额: 1500000 }, { 营业额: { unit: "万元" } })} />);
    const c = cell("营业额");
    expect(c.querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^150亿元$/);
    expect(c.querySelector(".v2-chart-kpi-exact")).toHaveTextContent("15,000,000,000元");
  });

  it("groups the exact line the interface locale's way, like the magnitude line", () => {
    document.documentElement.lang = "de-DE";
    render(<ChartKPI spec={kpiSpec({ Umsatz: 8123456 })} />);
    expect(cell("Umsatz").querySelector(".v2-chart-kpi-value")).toHaveTextContent("8,12");
    expect(cell("Umsatz").querySelector(".v2-chart-kpi-exact")).toHaveTextContent(/^8\.123\.456$/);
  });
});

describe("unitCarriesMagnitude", () => {
  // [unit, header, expected]. Every miss here prints a doubled magnitude such as `≈150万（万元）`.
  const CASES: Array<[string | undefined, string | undefined, boolean]> = [
    ["万元", undefined, true],
    ["千元", undefined, true],
    ["亿", undefined, true],
    ["（万元）", undefined, true],
    ["(万元)", undefined, true],
    ["人民币万元", undefined, true],
    ["元(万)", undefined, true],
    ["¥万", undefined, true],
    ["萬元", undefined, true],
    ["億元", undefined, true],
    ["Mn USD", undefined, true],
    ["USD M", undefined, true],
    ["USD mn", undefined, true],
    ["RMB mn", undefined, true],
    ["EUR Mio.", undefined, true],
    ["Mio. €", undefined, true],
    ["TEUR", undefined, true],
    ["KUSD", undefined, true],
    ["K", undefined, true],
    // Unit empty: the header's unit qualifier decides.
    [undefined, "营业额(万元)", true],
    [undefined, "营业额（亿元）", true],
    [undefined, "营业额 / 万元", true],
    [undefined, "营业额_亿元", true],
    [undefined, "Revenue ($M)", true],
    ["", "Revenue (USD mn)", true],
    // Negative controls.
    ["元", undefined, false],
    ["人次", undefined, false],
    ["¥", undefined, false],
    ["%", undefined, false],
    ["千克", undefined, false],
    [undefined, undefined, false],
    [undefined, "营业额", false],
    [undefined, "万达广场营业额", false],
    [undefined, "客流量 / 人次", false],
    [undefined, "营业额_2026", false],
    [undefined, "Revenue (USD)", false],
  ];
  it.each(CASES)("unit %s, header %s -> %s", (unit, header, expected) => {
    expect(unitCarriesMagnitude(unit, header)).toBe(expected);
  });

  it("recognizes a stated monetary magnitude in a header without losing its exact amount", () => {
    document.documentElement.lang = "zh-CN";
    render(<ChartKPI spec={kpiSpec({ "营业额(万元)": 1500000 })} />);
    const c = cell("营业额");
    expect(c.querySelector(".v2-chart-kpi-value")).toHaveTextContent(/^150亿元$/);
    expect(c.querySelector(".v2-chart-kpi-exact")).toHaveTextContent("15,000,000,000元");
    cleanup();
    document.documentElement.lang = "";
  });
});


it("keeps explicit integer precision on the ordinary secondary KPI value", () => {
  document.documentElement.lang = "en-US";
  try {
    render(<ChartKPI spec={kpiSpec({ Amount: 1234567.49 }, { Amount: { format: "int", unit: "USD" } })} />);
    expect(cell("Amount").querySelector(".v2-chart-kpi-exact")).toHaveTextContent("USD 1,234,567");
  } finally { document.documentElement.lang = ""; }
});
