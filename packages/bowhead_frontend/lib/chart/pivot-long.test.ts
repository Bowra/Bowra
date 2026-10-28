/**
 * Long → wide table (chart-config-ux ⑪).
 *
 * Shape from real-device `Top 10品类大类月度趋势` on 116: 70 rows = 10 categories × 7 months,
 * columns `每月(date) / 种类大类(categorical) / 营业额(numeric)`.
 * Before fix, x-axis showed "2026年1月 ×10、2026年2月 ×10 …", with one legend line.
 */
import { describe, expect, it } from "vitest";

import { buildChartJSConfig } from "./build-config";
import { PIVOTABLE_KINDS, pivotLongSpec, transposeTimeWideSpec } from "./pivot-long";
import { resolveChartTheme } from "@/lib/chart-theme";
import type { ChartSpec } from "./types";

const THEME = resolveChartTheme();

/** Long table of n categories × m months. */
function longSpec(kind: ChartSpec["kind"], nCat = 3, nMonth = 4): ChartSpec {
  const rows: Array<Record<string, string | number | null>> = [];
  for (let m = 0; m < nMonth; m += 1) {
    for (let c = 0; c < nCat; c += 1) {
      rows.push({ 每月: `2026-0${m + 1}`, 品类: `C${c}`, 营业额: (c + 1) * 100 + m });
    }
  }
  return {
    kind,
    title: "各品类月度趋势",
    columns: [
      { key: "每月", header: "每月", type: "date" },
      { key: "品类", header: "品类", type: "categorical" },
      { key: "营业额", header: "营业额", type: "numeric", unit: "元" },
    ],
    rows,
    row_count: rows.length,
  };
}

describe("认长表", () => {
  it("★★ 1 数值 + 2 维度 + x 重复 → 摊成一列一个品类", () => {
    const out = pivotLongSpec(longSpec("line", 3, 4))!;
    expect(out).not.toBeNull();
    expect(out.seriesCount).toBe(3);
    expect(out.seriesCol.key).toBe("品类");
    // 4 months, 4 rows (not 12); columns = 1 x + 3 series
    expect(out.spec.rows).toHaveLength(4);
    expect(out.spec.columns).toHaveLength(4);
    expect(out.spec.columns.slice(1).map((c) => c.header)).toEqual(["C0", "C1", "C2"]);
    expect(out.spec.row_count).toBe(4);
  });

  it("★ 值填对格：第 m 月第 c 个品类的数不能串位", () => {
    const out = pivotLongSpec(longSpec("line", 3, 4))!;
    const first = out.spec.rows![0]!;
    expect(first["每月"]).toBe("2026-01");
    const keys = out.spec.columns.slice(1).map((c) => c.key);
    expect(first[keys[0]!]).toBe(100);
    expect(first[keys[1]!]).toBe(200);
    expect(first[keys[2]!]).toBe(300);
  });

  it("★★ 缺格是 null 不是 0——某品类某月没数据，不是卖了 0 块", () => {
    const s = longSpec("line", 2, 2);
    s.rows = s.rows!.filter((r) => !(r["每月"] === "2026-02" && r["品类"] === "C1"));
    const out = pivotLongSpec(s)!;
    const feb = out.spec.rows!.find((r) => r["每月"] === "2026-02")!;
    const c1 = out.spec.columns.find((c) => c.header === "C1")!;
    expect(feb[c1.key]).toBeNull();
    expect(feb[c1.key]).not.toBe(0);
  });

  it("★ 同一格多行 → 求和并标记（源里还有没摊开的维度）", () => {
    const s = longSpec("line", 2, 2); // 2 categories × 2 months; x has 2 values
    s.rows!.push({ 每月: "2026-01", 品类: "C0", 营业额: 5 });
    const out = pivotLongSpec(s)!;
    expect(out.aggregated).toBe(true);
    const c0 = out.spec.columns.find((c) => c.header === "C0")!;
    expect(out.spec.rows![0]![c0.key]).toBe(105);
  });

  it("★ 系列列继承单位，并带上原列名给百分比词面判定用", () => {
    const out = pivotLongSpec(longSpec("line", 2, 2))!;
    for (const c of out.spec.columns.slice(1)) {
      expect(c.unit).toBe("元");
      expect(c.source_header).toBe("营业额");
      expect(c.type).toBe("numeric");
    }
  });

  it("★★ 值轴标题还是原度量名，不是第一个品类名", () => {
    // Caught in local real browser: without `measure_header`, chart `各品类月度趋势`
    // y-axis says "外套（万）" — legend name displaces axis title. Dataset-count-only tests miss it.
    const out = pivotLongSpec(longSpec("line", 3, 4))!;
    for (const c of out.spec.columns.slice(1)) {
      expect(c.measure_header).toBe("营业额");
    }
    const cfg = buildChartJSConfig(longSpec("line", 3, 4), THEME);
    const yTitle = (cfg.options as { scales?: { y?: { title?: { text?: string } } } })
      .scales?.y?.title?.text;
    // Since Chart Embed Spec the single-measure axis states only its unit; it still must never name a slice.
    expect(yTitle).toBe("元");
    expect(yTitle).not.toContain("C0");
  });
});

describe("不认的情形（每条都是「别乱动」）", () => {
  it("宽表不动：x 不重复", () => {
    const s = longSpec("line", 1, 4); // Only 1 row per month ⇒ x is not repeated
    expect(pivotLongSpec(s)).toBeNull();
  });

  it("两个数值列不动：「一列一系列」的老约定本来就成立", () => {
    const s = longSpec("line", 3, 4);
    s.columns = [...s.columns, { key: "利润", header: "利润", type: "numeric" }];
    s.rows = s.rows!.map((r) => ({ ...r, 利润: 1 }));
    expect(pivotLongSpec(s)).toBeNull();
  });

  it("三个维度列不动：摊完还剩维度列，x 轴又回到有歧义", () => {
    const s = longSpec("line", 3, 4);
    s.columns = [...s.columns, { key: "渠道", header: "渠道", type: "categorical" }];
    s.rows = s.rows!.map((r) => ({ ...r, 渠道: "线上" }));
    expect(pivotLongSpec(s)).toBeNull();
  });

  it("只摊出一条系列时不动（pivot 没有意义）", () => {
    const s = longSpec("line", 1, 4);
    s.rows = [
      { 每月: "2026-01", 品类: "C0", 营业额: 1 },
      { 每月: "2026-01", 品类: "C0", 营业额: 2 },
    ];
    expect(pivotLongSpec(s)).toBeNull();
  });

  it("★★ pie / scatter / dual_axis / combo 不 pivot——它们要的是两个不同度量", () => {
    for (const kind of ["pie", "scatter", "dual_axis", "combo", "heatmap", "sankey", "waterfall"] as const) {
      expect(pivotLongSpec(longSpec(kind, 3, 4)), kind).toBeNull();
    }
  });

  it("会 pivot 的正是那一族", () => {
    for (const kind of ["line", "area_stacked", "bar", "hbar", "stacked_bar"] as const) {
      expect(PIVOTABLE_KINDS.has(kind), kind).toBe(true);
      expect(pivotLongSpec(longSpec(kind, 3, 4)), kind).not.toBeNull();
    }
  });
});

describe("穿过 buildChartJSConfig（只验内部函数容易验个不 live 的分支）", () => {
  it("★★ 长表画出来是「4 个 x 点 × 3 条系列」，不是「12 个 x 点 × 1 条系列」", () => {
    const cfg = buildChartJSConfig(longSpec("line", 3, 4), THEME);
    expect(cfg.data.labels).toHaveLength(4);
    expect(cfg.data.datasets).toHaveLength(3);
    expect(cfg.data.datasets.map((d) => (d as { label?: string }).label)).toEqual(["C0", "C1", "C2"]);
  });

  it("★ 修前的行为作为对照：pie 不 pivot，仍是每行一个扇形", () => {
    // pie requires 1 category + 1 number; long table already qualifies ⇒ still draws 12 rows.
    const cfg = buildChartJSConfig(longSpec("pie", 3, 4), THEME);
    expect(cfg.data.labels).toHaveLength(12);
  });

  it("★ 宽表零影响：既有的「一列一系列」spec 走原路", () => {
    const wide: ChartSpec = {
      kind: "line",
      title: "w",
      columns: [
        { key: "m", header: "月份", type: "date" },
        { key: "a", header: "营业额", type: "numeric" },
        { key: "b", header: "利润", type: "numeric" },
      ],
      rows: [
        { m: "2026-01", a: 1, b: 2 },
        { m: "2026-02", a: 3, b: 4 },
      ],
      row_count: 2,
    };
    const cfg = buildChartJSConfig(wide, THEME);
    expect(cfg.data.labels).toHaveLength(2);
    expect(cfg.data.datasets).toHaveLength(2);
  });
});

describe("两道防误伤的闸（都是 116 真语料逼出来的）", () => {
  it("★★ x 只有一个取值不 pivot —— 会把 Top20 横条图毁成「1 个 x 点 × 20 条系列」", () => {
    // Real-device `示例药品甲 Top 20 医院` on 116: 20 rows, identical drug names, different hospital name in each row.
    const s: ChartSpec = {
      kind: "hbar",
      title: "示例药品甲 Top 20 医院",
      columns: [
        { key: "drug", header: "药品", type: "categorical" },
        { key: "hosp", header: "医院", type: "categorical" },
        { key: "amt", header: "销售额", type: "numeric" },
      ],
      rows: Array.from({ length: 20 }, (_, i) => ({ drug: "示例药品甲", hosp: `H${i}`, amt: 100 - i })),
      row_count: 20,
    };
    expect(pivotLongSpec(s)).toBeNull();
  });

  it("★★ 两个维度没真交叉不 pivot —— 摊出来是个几乎全空的对角矩阵", () => {
    // Real-device `门店面积与营业额关系` on 116: 39 rows become 39 series, fill ratio 0.20.
    const s: ChartSpec = {
      kind: "bar",
      title: "门店面积与营业额关系",
      columns: [
        { key: "seg", header: "面积段", type: "categorical" },
        { key: "shop", header: "门店", type: "categorical" },
        { key: "amt", header: "营业额", type: "numeric" },
      ],
      // 5 area ranges × 8 uniquely named stores per range = 40 rows; fill ratio 40/(5×40)=0.2
      rows: Array.from({ length: 40 }, (_, i) => ({
        seg: `S${i % 5}`,
        shop: `店${i}`,
        amt: i,
      })),
      row_count: 40,
    };
    expect(pivotLongSpec(s)).toBeNull();
  });

  it("正对照：交叉得好的同形状照样 pivot（否则上面那条可能因为别的原因绿）", () => {
    const s: ChartSpec = {
      kind: "bar",
      title: "省 × 品类",
      columns: [
        { key: "prov", header: "省", type: "categorical" },
        { key: "cat", header: "品类", type: "categorical" },
        { key: "amt", header: "营业额", type: "numeric" },
      ],
      // 5 provinces × 4 categories fully populated = 20 rows, fill ratio 1.0
      rows: Array.from({ length: 20 }, (_, i) => ({
        prov: `P${i % 5}`,
        cat: `C${Math.floor(i / 5)}`,
        amt: i,
      })),
      row_count: 20,
    };
    const out = pivotLongSpec(s);
    expect(out).not.toBeNull();
    expect(out!.seriesCount).toBe(4);
    expect(out!.spec.rows).toHaveLength(5);
  });
});

describe("★★ pivot 不许改变谁当 x 轴（两个 builder 口径不同）", () => {
  const branchMonth = (kind: ChartSpec["kind"]): ChartSpec => ({
    kind,
    title: "分行 × 月份",
    columns: [
      { key: "branch", header: "分行", type: "categorical" },
      { key: "month", header: "月份", type: "date" },
      { key: "sales", header: "销量", type: "numeric" },
    ],
    rows: [
      { branch: "广西", month: "2026-01", sales: 10 },
      { branch: "广西", month: "2026-02", sales: 11 },
      { branch: "海南", month: "2026-01", sales: 14 },
      { branch: "海南", month: "2026-02", sales: 15 },
    ],
    row_count: 4,
  });

  it("bar 按列序：分行在前 ⇒ 分行做 x，月份摊成系列（review[2] 定的口径）", () => {
    const out = pivotLongSpec(branchMonth("bar"))!;
    expect(out.spec.rows!.map((r) => r["branch"])).toEqual(["广西", "海南"]);
    expect(out.seriesCol.key).toBe("month");
  });

  it("line 时间优先：同一份数据 ⇒ 月份做 x，分行摊成系列", () => {
    const out = pivotLongSpec(branchMonth("line"))!;
    expect(out.spec.rows!.map((r) => r["month"])).toEqual(["2026-01", "2026-02"]);
    expect(out.seriesCol.key).toBe("branch");
  });

  it("穿过 builder 复核：bar 的 x 标签仍是分行，没被 date 抢走", () => {
    const cfg = buildChartJSConfig(branchMonth("bar"), THEME);
    expect(cfg.data.labels).toEqual(["广西", "海南"]);
  });
});


describe("⑯ 转置回来：时间当了系列、类目当了 x 轴", () => {
  /** Real-device `2026年各品类大类月度营业额趋势` on 116: 12 categories × 9 month columns. */
  function timeWide(kind: ChartSpec["kind"], nCat = 4, nMonth = 5): ChartSpec {
    const months = Array.from({ length: nMonth }, (_, m) => `${m + 1}月`);
    return {
      kind,
      title: "各品类月度营业额趋势",
      columns: [
        { key: "品类", header: "品类", type: "categorical" },
        ...months.map((m) => ({ key: m, header: m, type: "numeric" as const, unit: "元" })),
      ],
      rows: Array.from({ length: nCat }, (_, c) => ({
        品类: `C${c}`,
        ...Object.fromEntries(months.map((m, i) => [m, (c + 1) * 100 + i])),
      })),
      row_count: nCat,
    };
  }

  it("★★ x 轴换成月份、每个品类一条线", () => {
    const out = transposeTimeWideSpec(timeWide("line", 4, 5))!;
    expect(out).not.toBeNull();
    expect(out.spec.rows).toHaveLength(5); // 5 months
    expect(out.seriesCount).toBe(4); // 4 categories
    expect(out.spec.columns.slice(1).map((c) => c.header)).toEqual(["C0", "C1", "C2", "C3"]);
    expect(out.spec.rows![0]!["品类"]).toBe("1月");
  });

  it("★ 值不许串位：第 m 月第 c 个品类", () => {
    const out = transposeTimeWideSpec(timeWide("line", 3, 4))!;
    const keys = out.spec.columns.slice(1).map((c) => c.key);
    // C0 January = 100; C2 April = 303
    expect(out.spec.rows![0]![keys[0]!]).toBe(100);
    expect(out.spec.rows![3]![keys[2]!]).toBe(303);
  });

  it("★★ 穿过 builder：x 点是月份，不是品类", () => {
    const cfg = buildChartJSConfig(timeWide("line", 4, 5), THEME);
    expect(cfg.data.labels).toHaveLength(5);
    expect(cfg.data.datasets).toHaveLength(4);
    expect(cfg.data.datasets.map((d) => (d as { label?: string }).label)).toEqual(["C0", "C1", "C2", "C3"]);
  });

  it("★ 只要有一个数值列不像时间就不动——那不是纯时间宽表", () => {
    const s = timeWide("line", 4, 5);
    s.columns = [...s.columns, { key: "合计", header: "合计", type: "numeric" }];
    s.rows = s.rows!.map((r) => ({ ...r, 合计: 999 }));
    expect(transposeTimeWideSpec(s)).toBeNull();
  });

  it("★ 有真 date 列就轮不到这条（那是正常的时间轴）", () => {
    const s = timeWide("line", 4, 5);
    s.columns = [{ key: "月", header: "月", type: "date" }, ...s.columns];
    s.rows = s.rows!.map((r) => ({ ...r, 月: "2026-01" }));
    expect(transposeTimeWideSpec(s)).toBeNull();
  });

  it("★ 只有柱状族不转——折线才有「连接=连续性」的语义问题", () => {
    for (const k of ["bar", "hbar", "stacked_bar", "pie", "table"] as const) {
      expect(transposeTimeWideSpec(timeWide(k, 4, 5)), k).toBeNull();
    }
    for (const k of ["line", "area_stacked"] as const) {
      expect(transposeTimeWideSpec(timeWide(k, 4, 5)), k).not.toBeNull();
    }
  });

  it("★ 时间列太少不转（2 个点连不成趋势，且列名巧合更容易）", () => {
    expect(transposeTimeWideSpec(timeWide("line", 4, 2))).toBeNull();
  });

  it("★ 普通宽表零影响", () => {
    const wide: ChartSpec = {
      kind: "line",
      title: "w",
      columns: [
        { key: "省", header: "省", type: "categorical" },
        { key: "营业额", header: "营业额", type: "numeric" },
        { key: "利润", header: "利润", type: "numeric" },
        { key: "客流", header: "客流", type: "numeric" },
      ],
      rows: [
        { 省: "A", 营业额: 1, 利润: 2, 客流: 3 },
        { 省: "B", 营业额: 4, 利润: 5, 客流: 6 },
      ],
      row_count: 2,
    };
    expect(transposeTimeWideSpec(wide)).toBeNull();
  });
});


describe("⑯ 自查修：转置后两个名字都没有真来源", () => {
  function timeWide(): ChartSpec {
    const months = ["1月", "2月", "3月", "4月", "5月"];
    return {
      kind: "line",
      title: "各品类月度营业额趋势",
      columns: [
        { key: "品类", header: "品类", type: "categorical" },
        ...months.map((m) => ({ key: m, header: m, type: "numeric" as const, unit: "元" })),
      ],
      rows: [
        { 品类: "女装", ...Object.fromEntries(months.map((m, i) => [m, 100 + i])) },
        { 品类: "鞋包", ...Object.fromEntries(months.map((m, i) => [m, 200 + i])) },
      ],
      row_count: 2,
    };
  }

  it("★★ y 轴标题不许变成月份——源里的列名就是月份，度量叫什么根本没出现过", () => {
    // First version reused `nums[0].header` as measure_header ⇒ rendered "1月（元）".
    // Invisible from reading code; revealed by rendering.
    const cfg = buildChartJSConfig(timeWide(), THEME);
    const y = (cfg.options as { scales?: { y?: { title?: { text?: string } } } }).scales?.y?.title?.text;
    expect(y).toBe("元");
    expect(y).not.toContain("月");
  });

  it("★ x 列名不许还叫「品类」——那一列现在装的是月份", () => {
    const out = transposeTimeWideSpec(timeWide())!;
    expect(out.spec.columns[0]!.header).toBe("");
    expect(out.spec.rows![0]![out.spec.columns[0]!.key]).toBe("1月");
  });

  it("★ 没有单位时干脆不给轴标题，而不是渲个空括号", () => {
    const s = timeWide();
    s.columns = s.columns.map((c) => (c.type === "numeric" ? { ...c, unit: undefined } : c));
    const cfg = buildChartJSConfig(s, THEME);
    const y = (cfg.options as { scales?: { y?: { title?: unknown } } }).scales?.y?.title;
    expect(y).toBeUndefined();
  });

  it("★ 两条变换叠加不会互相拆台：长表(两个 categorical) 走完 seam 仍是月份做 x", () => {
    // `pivotLongSpec` follows column order, treating "品类" as x to produce month series; `transposeTimeWideSpec`
    // transposes it back — correct result. Non-obvious; pin to avoid one-sided changes silently reversing it.
    const rows: Array<Record<string, string | number | null>> = [];
    for (const cat of ["女装", "鞋包", "美妆"]) {
      for (let m = 1; m <= 4; m += 1) rows.push({ 品类: cat, 月份: `${m}月`, 营业额: m * 10 });
    }
    const cfg = buildChartJSConfig(
      {
        kind: "line",
        title: "长表",
        columns: [
          { key: "品类", header: "品类", type: "categorical" },
          { key: "月份", header: "月份", type: "categorical" },
          { key: "营业额", header: "营业额", type: "numeric" },
        ],
        rows,
        row_count: rows.length,
      },
      THEME,
    );
    expect(cfg.data.labels).toEqual(["1月", "2月", "3月", "4月"]);
    expect(cfg.data.datasets.map((d) => (d as { label?: string }).label)).toEqual(["女装", "鞋包", "美妆"]);
  });

  it("★ 串联时度量名不许被清掉——「没有真度量名」只在直接命中 ⑯ 时成立", () => {
    // Prior case pinned only x / series, not y-axis title, letting unconditional blank measure_header in ⑯
    // introduce an unnoticed regression during self-review fix: long-table path `pivotLongSpec` already
    // set `measure_header='营业额'`, but transpose cleared it ⇒ y-axis retained only "元".
    const rows: Array<Record<string, string | number | null>> = [];
    for (const cat of ["女装", "鞋包", "美妆"]) {
      for (let m = 1; m <= 4; m += 1) rows.push({ 品类: cat, 月份: `${m}月`, 营业额: m * 10 });
    }
    const cfg = buildChartJSConfig(
      {
        kind: "line",
        title: "长表",
        columns: [
          { key: "品类", header: "品类", type: "categorical" },
          { key: "月份", header: "月份", type: "categorical" },
          { key: "营业额", header: "营业额", type: "numeric", unit: "元" },
        ],
        rows,
        row_count: rows.length,
      },
      THEME,
    );
    // The axis title is unit-only now (Chart Embed Spec), so the measure name is checked where it lives: on the
    // plotted column every dataset carries (legend/tooltip/dual-axis titles read it from there).
    const y = (cfg.options as { scales?: { y?: { title?: { text?: string } } } }).scales?.y?.title?.text;
    expect(y).toBe("元");
    for (const ds of cfg.data.datasets) {
      expect((ds as { _yCol?: { measure_header?: string } })._yCol?.measure_header).toBe("营业额");
    }
  });
});
