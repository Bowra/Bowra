/**
 * chart/format.ts locale-aware formatNumber + abbrChinese tests.
 *
 * i18n v1: verify cross-locale behavior after switching abbrChinese to Intl.NumberFormat CLDR.
 * Mock localStorage to stub getCurrentLocale().
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";

import {
  formatDateLabel,
  formatGrainLabel,
  abbrChinese,
  abbrTick,
  formatCompactValue,
  formatNumber,
  formatPercentCell,
  formatObservedPercentCell,
  formatExact,
  formatObservedValue,
  formatSourceValue,
  formatDisplayValue,
  resolveDisplayMagnitude,
  resolveAxisMagnitude,
  axisUnitOf,
  monthlyAxisTickLabels,
  formatPrecise,
  comparePreparedNumbers,
  inlineCellUnit,
  resolvePercentColumn,
  resolvePercentScale,
  tableHeaderWithUnit,
} from "./format";

describe("readable value magnitudes and ordinary localized observations", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    [0.0009, "9万元", "90,000元"],
    [0.0000078, "780元", "780元"],
    [0.00000025, "25元", "25元"],
    [-3.736878438839402, "-3.74亿元", "-373,687,843.88元"],
    [0, "0元", "0元"],
  ])("renders stored %s hundred-million yuan as a readable base quantity", (value, compact, exact) => {
    setLocale("zh-CN");
    expect(formatDisplayValue(value, resolveDisplayMagnitude([value], "亿元")!)).toBe(compact);
    expect(formatObservedValue(value, "亿元")).toBe(exact);
  });

  it.each([
    ["zh-CN", "1,234,567.89", "9万", "1.2亿"],
    ["en-US", "1,234,567.89", "90K", "120M"],
    ["de-DE", "1.234.567,89", "90.000", "120 Mio."],
    ["fr-FR", "1 234 567,89", "90 k", "120 M"],
  ] as const)("uses %s grouping, decimal separators and native compact thresholds", (locale, exact, small, large) => {
    setLocale(locale);
    expect(formatObservedValue(1234567.89)).toBe(exact);
    expect(formatDisplayValue(90000, resolveDisplayMagnitude([90000])!)).toBe(small);
    expect(formatDisplayValue(120000000, resolveDisplayMagnitude([120000000])!)).toBe(large);
  });

  it("localizes currency and percent placement with ordinary precision and no FX", () => {
    setLocale("de-DE");
    expect(formatObservedValue(-1234.56789, "EUR")).toBe("-1.234,57 EUR");
    expect(formatObservedValue(0.0009, "CNY 亿")).toBe("90.000 CNY");
    expect(formatObservedPercentCell(0.123456, "fraction")).toBe("12,35 %");
    expect(formatPercentCell(0.123456, "fraction")).toBe("12,35 %");
    setLocale("fr-FR");
    expect(formatObservedValue(1234567.89, "EUR")).toBe("1 234 567,89 EUR");
    expect(formatPercentCell(-12.3456, "points")).toBe("-12,35 %");
  });

  it("rounds for reading without inventing units or multiplier provenance", () => {
    setLocale("zh-CN");
    expect(formatObservedValue(1421464192.74227, "元")).toBe("1,421,464,192.74元");
    expect(formatObservedValue(0.0009, "custom scale")).toBe("0.0009custom scale");
    expect(resolveDisplayMagnitude([0.0009], "custom scale").shift).toBe(0);
    expect(axisUnitOf({ key: "v", header: "万达营业额", type: "numeric" })).toBeUndefined();
    expect(axisUnitOf({ key: "v", header: "营业额_亿元", type: "numeric" })).toBe("亿元");
    expect(formatObservedValue(null, "元")).toBe("—");
    expect(formatObservedValue(-0, "元")).toBe("0元");
  });

  it("honors explicit table precision without converting source units", () => {
    setLocale("zh-CN");
    expect(formatSourceValue(0.0009, "亿元", false, { decimal_places: 5, decimal_mode: "fixed" })).toBe("0.00090");
    expect(formatSourceValue(0.0009, "亿元")).toBe("0.0009亿元");
  });

  it("uses range differences for ticks and never turns a small finite value into zero", () => {
    setLocale("zh-CN");
    const display = resolveAxisMagnitude([1e9, 1e9 + 1], "元");
    expect(display.unit).toBe("元");
    expect(formatDisplayValue(1e9, display, false)).not.toBe(formatDisplayValue(1e9 + 1, display, false));
    const tiny = resolveDisplayMagnitude([1e-10], "元")!;
    expect(formatDisplayValue(1e-10, tiny)).toBe("1E-10元");
  });
});

function setLocale(locale: "zh-CN" | "en-US" | "de-DE" | "fr-FR"): void {
  // getCurrentLocale reads bowhead.user_profile.language
  const profile = JSON.stringify({ language: locale });
  vi.stubGlobal("window", {
    localStorage: {
      getItem: (k: string) => (k === "bowhead.user_profile" ? profile : null),
    },
  });
  vi.stubGlobal("navigator", { language: locale });
}

describe("abbrChinese (now locale-aware via Intl)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe("zh-CN", () => {
    beforeEach(() => setLocale("zh-CN"));

    it("12345 → contains 万", () => {
      expect(abbrChinese(12345, 1)).toContain("万");
    });

    it("120000000 → contains 亿", () => {
      expect(abbrChinese(120_000_000, 1)).toContain("亿");
    });

    it("small number unchanged", () => {
      // < 10K: CLDR does not compact; retain thousands grouping
      expect(abbrChinese(5000, 0)).toMatch(/5,?000/);
    });
  });

  describe("en-US", () => {
    beforeEach(() => setLocale("en-US"));

    it("12345 → contains K", () => {
      expect(abbrChinese(12345, 1)).toContain("K");
    });

    it("120000000 → contains M", () => {
      expect(abbrChinese(120_000_000, 1)).toContain("M");
    });
  });
});

describe("formatNumber chart wrapper", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe("zh-CN", () => {
    beforeEach(() => setLocale("zh-CN"));

    it("currency uses ¥ prefix", () => {
      const out = formatNumber(120_000_000, "currency", "¥");
      expect(out.startsWith("¥")).toBe(true);
      expect(out).toContain("亿");
    });

    it("percent renders as N.N%", () => {
      const out = formatNumber(0.123, "percent");
      expect(out).toMatch(/12.[23]%/);  // 12.3 ± rounding
    });

    it("null returns dash", () => {
      expect(formatNumber(null)).toBe("—");
      expect(formatNumber(undefined)).toBe("—");
    });
  });

  describe("en-US", () => {
    beforeEach(() => setLocale("en-US"));

    it("currency 1.2亿 → 120M with ¥", () => {
      const out = formatNumber(120_000_000, "currency", "¥");
      // Intl en-US compact: "120.0M" (currency defaults to decimals=1); chart wrapper prefixes ¥
      expect(out).toMatch(/120(\.0)?M/);
      expect(out.startsWith("¥")).toBe(true);
    });

    it("percent same regardless of locale", () => {
      const out = formatNumber(0.123, "percent");
      expect(out).toMatch(/12.[23]%/);
    });
  });

  // ─── chat-truth Phase L-FE (L2) — preserve decimals for sub-1 values ───
  describe("chat-truth Phase L sub-1 preservation", () => {
    beforeEach(() => setLocale("zh-CN"));

    it("L-T2 formatter -0.09 preserves decimals (not '-0')", () => {
      // walkthrough Session 4 bug: int/default path rounded -0.09 to "-0".
      // Fix preserves 2 decimals — output includes "0.09" and retains minus sign.
      const out = formatNumber(-0.09);
      expect(out).not.toBe("-0");
      expect(out).not.toBe("0");
      expect(out).toMatch(/0\.09/);
      // Contains ASCII "-" or Unicode minus (U+2212)
      expect(out).toMatch(/[-−]/);
    });

    it("L-T2b formatter 0.5 preserves decimals", () => {
      const out = formatNumber(0.5);
      // 0.5 should render "0.50" (decimals=2), never lose precision as "0"
      expect(out).not.toBe("0");
      expect(out).toMatch(/0\.5/);
    });

    it("L-T3 formatter 0 → '0' (no decimal pollution)", () => {
      // v === 0 uses original decimals=0 path; sub-1 branch must not turn it into "0.00"
      const out = formatNumber(0);
      expect(out).toBe("0");
    });

    it("L-T4 formatter 19.9 still compact-formatted (don't break 19.9 亿 path)", () => {
      // 19.9 is outside sub-1 range, using original int/default decimals=0 → Intl compact;
      // < 10K stays uncompressed → "20" (decimals=0 rounding). Key: no error or formatter crash.
      const out = formatNumber(19.9);
      expect(out).toMatch(/^(20|19|19.9)/);
    });

    it("L-T4b large compact path intact (199_000_000 → 1.99 亿)", () => {
      // decimals=0 path (>=1), compact still zh "亿" / en "M"
      const out = formatNumber(199_000_000);
      expect(out).toContain("亿");
    });

    it("percent fmt unaffected by sub-1 branch (-0.09 → '-9.0%')", () => {
      // Percent path returns early, skipping sub-1 branch — guarded by test
      const out = formatNumber(-0.09, "percent");
      expect(out).toMatch(/-9\.[01]%/);
    });
  });
});

// ─── Batch 0 0-E (chart-table-ux tech-design §1.5): three modes ───


describe("0-E formatPrecise — 精确档永不 compact", () => {
  it("1,284,302 不得压成 1M（真 prod KPI 订单数形状）", () => {
    const out = formatPrecise(1_284_302, "单");
    expect(out).toContain("1,284,302");
    expect(out).not.toMatch(/M|万|亿/);
  });

  it("负货币符号在货币符之前：-¥83,247", () => {
    expect(formatPrecise(-83_247, "¥")).toBe("-¥83,247");
  });

  it("正货币: ¥3,048,210", () => {
    expect(formatPrecise(3_048_210, "¥")).toBe("¥3,048,210");
  });

  it("小数值保留精度（4.2 元/㎡）", () => {
    expect(formatPrecise(4.2, "元/㎡")).toContain("4.2");
  });
});

describe("★★ 小于 1 的值不许渲成 0 —— 整条轴变成一串 0", () => {
  // 116 measurements: among 2753 buildable charts, 103 columns had all ticks rendered 0, 217 had some rendered 0,
  // and 181 rendered "-0". Nearly all affected columns were decimal-stored ratios like "同比变化率"
  // (0.0064 = 0.64% year-over-year; no percent format on column, so percentage formatting path is unavailable).
  it.each([
    [0.0004, "0.0004"],
    [0.004, "0.004"],
    [0.04, "0.04"],
    [0.0064, "0.0064"],
    [0.049316, "0.049"],
    [0.00012, "0.00012"],
  ])("abbrTick(%s) = %s", (v, expected) => {
    expect(abbrTick(v)).toBe(expected);
  });

  it("★ 负的极小值不许渲成「-0」 —— 屏幕上一个负零，读者只会当是 bug", () => {
    expect(abbrTick(-0.0004)).toBe("-0.0004");
    expect(abbrTick(-0.0004)).not.toBe("-0");
  });

  it("★ 真正的 0 仍然是 0，字面 `-0` 也是 0", () => {
    expect(abbrTick(0)).toBe("0");
    // `JSON.parse("-0")` produces -0, rendered "-0" by both formatting paths.
    expect(abbrTick(-0)).toBe("0");
    expect(abbrTick(JSON.parse("-0") as number)).toBe("0");
    expect(formatCompactValue(-0)).toBe("0");
  });

  it("★ 数据标签与摘要行走同一档（都经 abbrTick）", () => {
    expect(formatCompactValue(0.0064)).toBe("0.01");
    expect(formatCompactValue(0.0064, "%")).toBe("0.01%");
  });

  it("负对照：≥1 的值逐字不变，紧凑档没被这次改动动到", () => {
    expect(abbrTick(1.5)).toBe("1.5");
    expect(abbrTick(10)).toBe("10");
    expect(abbrTick(123_456_789)).toContain("亿");
  });

  it("★ 一张图上同时有 0.0006 与 0.35 时两头都读得出来（有效数字，不是固定小数位）", () => {
    expect(abbrTick(0.0006)).toBe("0.0006");
    expect(abbrTick(0.35)).toBe("0.35");
  });

  it("非有限值不进这条路（不抛）", () => {
    expect(() => abbrTick(Number.NaN)).not.toThrow();
    expect(() => abbrTick(Number.POSITIVE_INFINITY)).not.toThrow();
  });
});

describe("批1 1-D abbrTick — 轴刻度不出 .0 噪音", () => {
  beforeEach(() => setLocale("zh-CN"));
  afterEach(() => vi.unstubAllGlobals());

  it("整数刻度剥 .0（10.0万 → 10万）", () => {
    expect(abbrTick(100_000)).not.toContain(".0");
    expect(abbrTick(100_000)).toContain("10");
  });

  it("非整刻度保留 1 位小数（12.5万）", () => {
    expect(abbrTick(125_000)).toContain("12.5");
  });

  it("整十小数值：10 → \"10\" 不是 \"10.0\"", () => {
    expect(abbrTick(10)).toBe("10");
  });
});

describe("图内紧凑值", () => {
  beforeEach(() => setLocale("zh-CN"));
  afterEach(() => vi.unstubAllGlobals());

  it("十亿级参考线跟轴使用同一量级，不回退为完整原值", () => {
    const out = formatCompactValue(1_823_356_460);
    expect(out).toContain("18.2");
    expect(out).toContain("亿");
    expect(out).not.toContain("1,823,356,460");
  });

  it("保留单位位置并过滤无量纲伪单位", () => {
    expect(formatCompactValue(182_000_000, "¥")).toMatch(/^¥/);
    expect(formatCompactValue(0.51, "correlation")).not.toContain("correlation");
  });
});


// ─────────────────────────────────────────────────────────────
// review R0 regression guards (three findings in adversarial review 2026-08-12, all empirically verified)
// ─────────────────────────────────────────────────────────────




describe("R0-3 千分位不得吃进小数部分", () => {
  beforeEach(() => setLocale("zh-CN"));
  afterEach(() => vi.unstubAllGlobals());

  it("★|v|<1 走 4 位小数时不再变成 0.1,744（真 prod 1015 个单元格命中）", () => {
    expect(formatPrecise(0.1744)).toBe("0.1744");
    expect(formatPrecise(0.9524)).toBe("0.9524");
    expect(formatPrecise(-0.1744)).toBe("-0.1744");
    expect(formatPrecise(0.0001)).toBe("0.0001");
  });

  it("整数部分照常分组", () => {
    expect(formatPrecise(1_284_302)).toBe("1,284,302");
    expect(formatPrecise(-83_247, "¥")).toBe("-¥83,247");
    // Observation displays preserve all available decimal digits.
    expect(formatPrecise(1234.56)).toBe("1,234.56");
    // Grouping must not corrupt decimals.
    expect(formatPrecise(12.34)).toBe("12.34");
  });

  it("带单位时小数也不被污染", () => {
    expect(formatPrecise(0.9524, "天")).toBe("0.9524天");
  });
});

describe("observation precision", () => {
  it("preserves decimals and expands tiny scientific notation without rounding", () => {
    expect(formatExact(1_234.56789, "USD")).toBe("1,234.56789USD");
    expect(formatExact(-1234.56789, "$")).toBe("-$1,234.56789");
    expect(formatPrecise(-0.000000123456)).toBe("-1.2E-7");
    expect(formatExact(Number.MIN_VALUE)).toBe(`0.${"0".repeat(323)}5`);
    expect(formatExact(null)).toBe("—");
    expect(formatExact(Infinity)).toBe("—");
    expect(formatExact(-0)).toBe("0");
  });

  it("shifts explicit percentage fractions textually and leaves compact labels independent", () => {
    expect(formatObservedPercentCell(0.1744, "fraction")).toBe("17.44%");
    expect(formatObservedPercentCell(0.1987352816409153, "fraction")).toBe("19.87%");
    expect(formatObservedPercentCell(0.00000012, "fraction")).toBe("0.000012%");
    expect(formatObservedPercentCell(-12.3456789, "points")).toBe("-12.35%");
    expect(formatObservedPercentCell(null, "points")).toBe("—");
    expect(formatPercentCell(0.1744, "fraction")).toBe("17.44%");
  });
});

describe("scoped monthly axis labels", () => {
  it("shortens ticks only when supplied text names their single year", () => {
    expect(monthlyAxisTickLabels(["2026-01", "2026-06"], "Revenue · 2026", "zh-CN")).toEqual(["1月", "6月"]);
    expect(monthlyAxisTickLabels(["Jan 2026", "Jun 2026"], "2026 revenue", "en-US")).toEqual(["Jan", "Jun"]);
    expect(monthlyAxisTickLabels(["2026-01", "2026-06"], "Revenue", "en-US")).toBeNull();
    expect(monthlyAxisTickLabels(["2025-12", "2026-01"], "2025–2026", "en-US")).toBeNull();
    expect(monthlyAxisTickLabels(["2026-01-01", "2026-01-02"], "2026", "en-US")).toBeNull();
    expect(monthlyAxisTickLabels(["2026-13"], "2026", "en-US")).toBeNull();
  });
});

// English chart axes localize Chinese-formatted time buckets.
//
// Those are datarc-returned **data values** (observed spec.rows: [{"每月":"2026年1月", ...}]),
// old implementation recognized already-friendly format and passed through, showing English users Chinese months.

describe("formatDateLabel · locale", () => {
  it("中文 locale / 不传 locale → 逐字节不变（回归闸）", () => {
    for (const loc of [undefined, "zh-CN", "zh"]) {
      expect(formatDateLabel("2026年1月", loc)).toBe("2026年1月");
      expect(formatDateLabel("2026年第2季度", loc)).toBe("2026年第2季度");
      expect(formatDateLabel("2026年", loc)).toBe("2026年");
    }
  });

  it("en → 中文时间桶换成英文（真机值）", () => {
    expect(formatDateLabel("2026年1月", "en-US")).toBe("Jan 2026");
    expect(formatDateLabel("2026年12月", "en-US")).toBe("Dec 2026");
    expect(formatDateLabel("2026年第2季度", "en-US")).toBe("Q2 2026");
    expect(formatDateLabel("2026年2季度", "en-US")).toBe("Q2 2026");
    expect(formatDateLabel("2026年", "en-US")).toBe("2026");
  });

  it("ISO / 已英文的格式两种 locale 下都不变", () => {
    for (const loc of ["zh-CN", "en-US"]) {
      expect(formatDateLabel("2026-04", loc)).toBe("2026-04");
      expect(formatDateLabel("2026-04-30", loc)).toBe("2026-04-30");
      expect(formatDateLabel("2026-Q2", loc)).toBe("2026-Q2");
    }
  });

  it("不是时间桶的串一律原样——绝不猜", () => {
    expect(formatDateLabel("诺易1000IU", "en-US")).toBe("诺易1000IU");
    expect(formatDateLabel("2026年13月", "en-US")).toBe("2026年13月"); // Never fabricate out-of-range months
    expect(formatDateLabel("", "en-US")).toBe("");
  });
});

describe("formatGrainLabel", () => {
  it("时间粒度维度名按 locale 换（datarc 不会给它配 en-US）", () => {
    expect(formatGrainLabel("每月", "en-US")).toBe("Monthly");
    expect(formatGrainLabel("每季度", "en-US")).toBe("Quarterly");
    expect(formatGrainLabel("每月", "zh-CN")).toBe("每月");
    expect(formatGrainLabel("每月", undefined)).toBe("每月");
  });

  it("词表外的列名原样——业务维度/产品名没有对应英文，翻了就是编", () => {
    expect(formatGrainLabel("品规", "en-US")).toBe("品规");
    expect(formatGrainLabel("一级品牌名称英文", "en-US")).toBe("一级品牌名称英文");
  });
});

describe("百分比形状：单位就是 % ⇒ points（2026-09-20 实测 0.35% 被渲成 35.0%）", () => {
  const col = (over: Record<string, unknown> = {}) => ({ header: "退款率", unit: "%", ...over });

  it("★ 整列都小于 1% 时也判 points —— 这条只在数值都很小时露头", () => {
    // Backend restores `0.35%` text to 0.35 with unit=%; frontend formerly used only max ≤1 ⇒ fraction ⇒ ×100.
    expect(resolvePercentScale([0.35, 0.8, 0.12], col())).toBe("points");
    // Decimal half-up display rounding gives 0.4%; the original 0.35% remains in hover.
    expect(formatPercentCell(0.35, resolvePercentScale([0.35, 0.8, 0.12], col()))).toBe("0.35%");
  });

  it("跨过 1% / 全部大于 1% 也一样（三档一致，不随值域翻档）", () => {
    expect(resolvePercentScale([0.35, 1.2, 12.5], col())).toBe("points");
    expect(resolvePercentScale([15.37, 22.1, 31.4], col())).toBe("points");
  });

  it("★ 单位排在名称族**之前**：CSV 里写 `102.5%` 的「达成率」列不能再被名字判成小数", () => {
    // Name family picks fraction for "达成率", scaling 102.5 again ×100 to 10250%.
    expect(resolvePercentScale([102.5, 98.2], col({ header: "达成率" }))).toBe("points");
  });

  it("负对照：列上显式声明仍然最高优先", () => {
    expect(resolvePercentScale([0.35], col({ percent_scale: "fraction" }))).toBe("fraction");
  });

  it("does not infer fraction storage without a declaration", () => {
    expect(resolvePercentColumn({ header: "某比例" })).toBe(false);
  });
});


// ── Value range overturns name-guessed fraction (only this direction) ─────────────────────────

describe("abbrTick 去掉整数刻度的「,0 / .0」按当前语言的小数分隔符", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([
    ["zh-CN", ".", 100_000, 105_000, "10.5"],
    ["en-US", ".", 1_000_000, 1_500_000, "1.5"],
    ["de-DE", ",", 1_000_000, 1_500_000, "1,5"],
    ["fr-FR", ",", 1_000_000, 1_500_000, "1,5"],
  ] as const)("%s：整数刻度不带「%s0」，非整数保留一位", (locale, sep, whole, fractional, kept) => {
    setLocale(locale);
    expect(abbrTick(whole)).not.toContain(`${sep}0`);
    expect(abbrTick(fractional)).toContain(kept);
  });

  it("德语、法语去掉的正是 Intl 给出的「1,0」", () => {
    setLocale("de-DE");
    const german = new Intl.NumberFormat("de-DE", { notation: "compact", minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(1_000_000);
    expect(german).toContain("1,0");
    expect(abbrTick(1_000_000)).toBe(german.replace("1,0", "1"));
    setLocale("fr-FR");
    const french = new Intl.NumberFormat("fr-FR", { notation: "compact", minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(1_000_000);
    expect(abbrTick(1_000_000)).toBe(french.replace("1,0", "1"));
  });
});

describe("table unit placement: percentage marks inline, every other unit once in the header", () => {
  it("keeps only percentage-style marks inline in cells", () => {
    for (const unit of ["%", "‰", "‱"]) expect(inlineCellUnit(unit)).toBe(unit);
    expect(formatPrecise(12.5, inlineCellUnit("%"))).toBe("12.5%");
  });

  it("moves every other unit out of every cell, whatever its wording", () => {
    // Production shape (1.5.58): a model-written unit phrase was glued to each value and clipped mid-word.
    for (const unit of ["工作区原始金额单位", "元/人", "people", "usd", "CNY 10k", "¥", "$", "元", "亿元", "万元", "kg", "pp", "USD", "CNY"])
      expect(inlineCellUnit(unit)).toBeUndefined();
    expect(formatPrecise(170_789_558.1701, inlineCellUnit("亿元"))).toBe("170,789,558.1701");
    expect(formatPrecise(170_789_558.1701, inlineCellUnit("工作区原始金额单位"))).toBe("170,789,558.1701");
  });

  it("drops missing and dimensionless units from both cells and headers", () => {
    expect(inlineCellUnit(undefined)).toBeUndefined();
    expect(inlineCellUnit("  ")).toBeUndefined();
    expect(inlineCellUnit("decimal")).toBeUndefined();
    expect(tableHeaderWithUnit("Correlation", "correlation")).toBe("Correlation");
  });

  it("shows a non-inline unit once in the header with script-matched brackets", () => {
    expect(tableHeaderWithUnit("销售金额", "工作区原始金额单位")).toBe("销售金额（工作区原始金额单位）");
    expect(tableHeaderWithUnit("Sales Value", "工作区原始金额单位")).toBe("Sales Value (工作区原始金额单位)");
    expect(tableHeaderWithUnit("Visits", " people ")).toBe("Visits (people)");
  });

  it("names magnitude and currency units in the header", () => {
    expect(tableHeaderWithUnit("销售额", "亿元")).toBe("销售额（元）");
    expect(tableHeaderWithUnit("Revenue", "USD")).toBe("Revenue (USD)");
    expect(tableHeaderWithUnit("Revenue", "¥")).toBe("Revenue (¥)");
  });

  it("leaves the header alone when the unit is inline or already named", () => {
    expect(tableHeaderWithUnit("Share", "%")).toBe("Share");
    expect(tableHeaderWithUnit("营业额（万元）", "万元")).toBe("营业额（元）");
    expect(tableHeaderWithUnit("Revenue", undefined)).toBe("Revenue");
    expect(tableHeaderWithUnit("客单价（元/人）", "元/人")).toBe("客单价（元/人）");
  });
});


describe("ordinary chart readouts", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("rounds ties away from zero and cleans binary tails without changing source values", () => {
    setLocale("en-US");
    expect(formatObservedValue(1.005, "USD")).toBe("USD 1.01");
    expect(formatObservedValue(-1.005, "USD")).toBe("-USD 1.01");
    expect(formatObservedValue(0.30000000000000004)).toBe("0.3");
    expect(formatObservedValue(-0, "USD")).toBe("USD 0");
    expect(formatObservedValue(1.23456789)).toBe("1.2346");
  });

  it("keeps tiny automatic values visible but honors an explicit integer format", () => {
    setLocale("en-US");
    expect(formatObservedValue(0.004, "USD")).toBe("USD 0.004");
    expect(formatObservedValue(0.000000012345)).toBe("1.2E-8");
    expect(formatObservedValue(-0.4, undefined, "int")).toBe("0");
    expect(formatObservedPercentCell(2.1735, "fraction")).toBe("217.35%");
    expect(formatObservedValue(0.35, "pp")).toBe("0.35pp");
  });

  it.each([
    ["zh-CN", "90,000", "元", "12.35%"],
    ["en-US", "90,000", "元", "12.35%"],
    ["de-DE", "90.000", "元", "12,35 %"],
    ["fr-FR", "90 000", "元", "12,35 %"],
  ] as const)("shares base-unit cells and headers in %s", (locale, digits, unit, percent) => {
    setLocale(locale);
    expect(formatObservedValue(0.0009, "亿元", undefined, false)).toBe(digits);
    expect(formatObservedValue(0.0009, "亿元")).toBe(`${digits}${unit}`);
    expect(tableHeaderWithUnit("Amount (亿元)", "亿元")).toBe("Amount (元)");
    expect(formatObservedPercentCell(0.123456, "fraction")).toBe(percent);
  });

  it("preserves unsafe integer tokens and compares them without Number coercion", () => {
    setLocale("en-US");
    expect(formatObservedValue("9007199254740993", "USD", undefined, false)).toBe("9,007,199,254,740,993");
    expect(formatObservedPercentCell("9007199254740993", "fraction")).toBe("900,719,925,474,099,300%");
    expect(comparePreparedNumbers("9007199254740993", "9007199254740992")).toBe(1);
    expect(comparePreparedNumbers("-9007199254740993", -1.25)).toBe(-1);
  });
});
