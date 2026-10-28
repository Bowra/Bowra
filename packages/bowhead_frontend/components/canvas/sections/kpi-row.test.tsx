/**
 * KPI value-length tiers (feedback-triage-2026-08-24).
 *
 * Observed on 116: fixed 30/32px serif values in minmax(180px, 1fr) cards wrap and consume
 * bottom padding: 示例品｜23.45亿元 spans two lines, 内销销售金额合计 104.71亿元 three,
 * 26 ASCII characters four. Cards grow taller (**no border overflow**), but three/four-line KPI numbers are unacceptable.
 *
 * Tiers use **display width**, not length: one Chinese character takes about two Western character widths. Cases
 * enforce that distinction: length would put 示例品｜23.45亿元 (11) and 824.9 ¥M (8)
 * in the same tier, despite nearly twice the actual width.
 */
import { describe, expect, it } from "vitest";

import { kpiValueSizeTier } from "./kpi-row";

describe("kpiValueSizeTier", () => {
  it("短值留在最大字号", () => {
    expect(kpiValueSizeTier("824.9 ¥M")).toBe("s");
    expect(kpiValueSizeTier("40.9%")).toBe("s");
    expect(kpiValueSizeTier("20个")).toBe("s");
  });

  it("★ 按显示宽度分档，不是按字符数", () => {
    // Similar lengths (8 versus 11), display widths 8 versus 18: require different tiers.
    expect(kpiValueSizeTier("824.9 ¥M")).toBe("s");
    expect(kpiValueSizeTier("示例品｜23.45亿元")).not.toBe("s");
  });

  it("116 上真折过行的那几个值都降档", () => {
    expect(kpiValueSizeTier("示例品｜23.45亿元")).toBe("l"); // Observed two lines at 32px; one at 18px
    expect(kpiValueSizeTier("内销销售金额合计 104.71亿元")).toBe("xl"); // Observed three lines at 32px; two at 15px
    expect(kpiValueSizeTier("ABCDEFGHIJKLMNOPQRSTUVWXYZ")).toBe("xl"); // Observed four lines at 32px; two at 15px
  });

  it("档位随长度单调不增", () => {
    const rank = { s: 0, m: 1, l: 2, xl: 3 } as const;
    const samples = ["1", "12345", "1234567890", "12345678901234", "123456789012345678901234567890"];
    const tiers = samples.map((s) => rank[kpiValueSizeTier(s)]);
    for (let i = 1; i < tiers.length; i += 1) {
      expect(tiers[i]).toBeGreaterThanOrEqual(tiers[i - 1]);
    }
  });

  it("★ 10 位纯数字必须退出最大档——实测里唯一会吃掉卡片下 padding 的漏网用例", () => {
    expect(kpiValueSizeTier("8888888888")).not.toBe("s");
    expect(kpiValueSizeTier("999999999")).toBe("s"); // Nine digits remain safe
  });

  it("空值不炸，落最大字号", () => {
    expect(kpiValueSizeTier("")).toBe("s");
    expect(kpiValueSizeTier(null)).toBe("s");
    expect(kpiValueSizeTier(undefined)).toBe("s");
  });

  it("★ 量的是渲染后的串——[cite:N] 这类 token 会被摘掉，不该把档位撑大", () => {
    // Marked value renders only 824.9 ¥M, so its tier must equal the unmarked value's tier.
    expect(kpiValueSizeTier("824.9 ¥M[cite:12]")).toBe(kpiValueSizeTier("824.9 ¥M"));
  });
});
