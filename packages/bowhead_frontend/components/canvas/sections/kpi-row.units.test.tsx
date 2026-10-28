/**
 * P2 batch 6: KPI number and unit are separate elements ("P2 Icons & Type Spec": unit 13 / --c-ink-3).
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { splitKpiValue } from "@/lib/kpi-value-parts";
import type { ReportSection } from "@/lib/types";

import { KpiRowSection } from "./kpi-row";

type KpiRow = Extract<ReportSection, { kind: "kpi_row" }>;

const ROW = {
  id: "sec_kpi",
  kind: "kpi_row",
  cells: [
    { lbl: "GMV", val: "12.84亿元" },
    { lbl: "同比", val: "−8.4%" },
    { lbl: "客单价", val: "¥186.20" },
    { lbl: "门店", val: "示例品｜23.45亿元" },
  ],
} as unknown as KpiRow;

afterEach(() => {
  cleanup();
});

describe("splitKpiValue", () => {
  it("splits CJK magnitude units, percent signs, currency prefixes and Latin units", () => {
    expect(splitKpiValue("12.84亿元")).toEqual({ prefix: "", number: "12.84", unit: "亿元" });
    expect(splitKpiValue("2,341万")).toEqual({ prefix: "", number: "2,341", unit: "万" });
    expect(splitKpiValue("−8.4%")).toEqual({ prefix: "", number: "−8.4", unit: "%" });
    expect(splitKpiValue("+1.3pp")).toEqual({ prefix: "", number: "+1.3", unit: "pp" });
    expect(splitKpiValue("186.20 元")).toEqual({ prefix: "", number: "186.20", unit: "元" });
    expect(splitKpiValue("¥186.20")).toEqual({ prefix: "¥", number: "186.20", unit: "" });
    expect(splitKpiValue("US$ 1,200")).toEqual({ prefix: "US$", number: "1,200", unit: "" });
    expect(splitKpiValue("824.9 ¥M")).toEqual({ prefix: "", number: "824.9", unit: "¥M" });
    expect(splitKpiValue("540.14 元/人")).toEqual({ prefix: "", number: "540.14", unit: "元/人" });
  });

  it("leaves bare numbers and anything that is not number + short unit alone", () => {
    for (const value of ["1,000", "示例品｜23.45亿元", "12–15%", "3 / 5", "约 20 个", "", "N/A"]) {
      expect(splitKpiValue(value), value).toBeNull();
    }
  });
});

describe("KpiRowSection value parts", () => {
  it("wraps number and unit", () => {
    const { container } = renderIntl(<KpiRowSection data={ROW} />);
    const vals = Array.from(container.querySelectorAll(".val"));

    expect(vals[0].querySelector(".v2-kpi-num")?.textContent).toBe("12.84");
    expect(vals[0].querySelector(".v2-kpi-unit")?.textContent).toBe("亿元");
    expect(vals[1].querySelector(".v2-kpi-unit.is-tight")?.textContent).toBe("%");
    expect(vals[2].querySelector(".v2-kpi-unit.is-prefix")?.textContent).toBe("¥");
    expect(vals[3].children).toHaveLength(0);
    // Same visible text and size tier as before
    expect(vals.map((v) => v.textContent)).toEqual(["12.84亿元", "−8.4%", "¥186.20", "示例品｜23.45亿元"]);
    expect(vals[3].getAttribute("data-len")).toBe("l");
  });
});
