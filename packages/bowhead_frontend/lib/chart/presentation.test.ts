/**
 * Batch 1 2.1b · presentation resolver unit tests (contract = design D, D5 frame).
 */

import { describe, expect, test } from "vitest";

import { mutedOf, resolvePresentation } from "./presentation";
import type { ChartSpec } from "./types";

function barSpec(extras: Partial<ChartSpec> = {}): ChartSpec {
  return {
    kind: "bar", title: "分品类销售",
    columns: [
      { key: "cat", header: "品类", type: "categorical" },
      { key: "v", header: "销售额", type: "numeric" },
    ],
    rows: [
      { cat: "女装", v: 340 },
      { cat: "男装", v: 210 },
      { cat: "童装", v: 480 },
    ],
    row_count: 3,
    ...extras,
  };
}

describe("resolvePresentation · 主角降级链", () => {
  test("图注命中类目 →  element lead（「女装一类贡献超三分之一」→ 女装柱）", () => {
    const pres = resolvePresentation(barSpec(), "女装一类贡献超三分之一");
    expect(pres.lead).toEqual({ kind: "element", index: 0 });
  });

  test("多候选取最长命中（「华东」与「华东大区」同现 → 华东大区）", () => {
    const spec = barSpec({
      rows: [
        { cat: "华东", v: 1 },
        { cat: "华东大区", v: 2 },
      ],
      row_count: 2,
    });
    const pres = resolvePresentation(spec, "华东大区领跑全国");
    expect(pres.lead).toEqual({ kind: "element", index: 1 });
  });

  test("图注不中 + 无 sort_by → null（全满色 fail-open）", () => {
    expect(resolvePresentation(barSpec(), "整体走势平稳").lead).toBeNull();
  });

  test("图注不中 + sort_by → 该指标最大元素（童装 480）", () => {
    const pres = resolvePresentation(barSpec({ sort_by: "v" }), "整体走势平稳");
    expect(pres.lead).toEqual({ kind: "element", index: 2 });
  });

  test("无图注时用 spec.subtitle 兜底", () => {
    const pres = resolvePresentation(barSpec({ subtitle: "男装环比走弱" }), null);
    expect(pres.lead).toEqual({ kind: "element", index: 1 });
  });

  test("line：图注匹配系列名 → dataset lead", () => {
    const spec: ChartSpec = {
      kind: "line", title: "月度趋势",
      columns: [
        { key: "m", header: "月份", type: "date" },
        { key: "rev", header: "营业额", type: "numeric" },
        { key: "profit", header: "利润", type: "numeric" },
      ],
      rows: [{ m: "2026-01", rev: 19.9, profit: 3.1 }],
      row_count: 1,
    };
    const hit = resolvePresentation(spec, "营业额自 1 月高点一路走低");
    expect(hit.lead).toEqual({ kind: "dataset", index: 0 });
    const miss = resolvePresentation(spec, "整体承压");
    expect(miss.lead).toBeNull();
  });

  test("stacked_bar pivot：图注匹配第二维度取值（渠道段）", () => {
    const spec: ChartSpec = {
      kind: "stacked_bar", title: "分行×渠道",
      columns: [
        { key: "branch", header: "分行", type: "categorical" },
        { key: "channel", header: "渠道", type: "categorical" },
        { key: "amt", header: "金额", type: "numeric" },
      ],
      rows: [
        { branch: "北京", channel: "网点", amt: 4 },
        { branch: "北京", channel: "线上", amt: 3 },
      ],
      row_count: 2,
    };
    expect(resolvePresentation(spec, "网点渠道贡献近半").lead).toEqual({ kind: "dataset", index: 0 });
    expect(resolvePresentation(spec, "线上增长最快").lead).toEqual({ kind: "dataset", index: 1 });
  });

  test("pie：图注不中也取最大扇区（占比图主角结构内建）", () => {
    const spec: ChartSpec = {
      kind: "pie", title: "渠道占比",
      columns: [
        { key: "ch", header: "渠道", type: "categorical" },
        { key: "v", header: "金额", type: "numeric" },
      ],
      rows: [
        { ch: "线上", v: 30 },
        { ch: "网点", v: 55 },
        { ch: "代理", v: 15 },
      ],
      row_count: 3,
    };
    const pres = resolvePresentation(spec, null);
    expect(pres.lead).toEqual({ kind: "element", index: 1 });
  });

  test("waterfall：主角恒为终点总计（rows.length 位）", () => {
    const spec: ChartSpec = {
      kind: "waterfall", title: "毛利桥",
      columns: [
        { key: "item", header: "项", type: "categorical" },
        { key: "d", header: "增量", type: "numeric" },
      ],
      rows: [{ item: "价格", d: 6.1 }, { item: "销量", d: -3.4 }],
      row_count: 2,
    };
    expect(resolvePresentation(spec, null).lead).toEqual({ kind: "element", index: 2 });
  });

  test("★单字类目不参与匹配（「减」会命中「环比减弱」并抢走 lead）", () => {
    const spec = barSpec({
      rows: [{ cat: "增", v: 1 }, { cat: "减", v: 2 }],
      row_count: 2,
      sort_by: "v",
    });
    // Rejected match → sort_by fallback (maximum = index 1), rather than choosing because text contains "减"
    const pres = resolvePresentation(spec, "整体略有下降，环比减弱");
    expect(pres.lead).toEqual({ kind: "element", index: 1 });
    // Negative control: two-character categories still match normally
    const ok = resolvePresentation(
      barSpec({ rows: [{ cat: "女装", v: 1 }, { cat: "男装", v: 2 }], row_count: 2 }),
      "男装环比走弱",
    );
    expect(ok.lead).toEqual({ kind: "element", index: 1 });
  });

  test("heatmap / sankey：无 lead 概念", () => {
    const spec = barSpec();
    expect(resolvePresentation({ ...spec, kind: "heatmap" }, "门店A 最热").lead).toBeNull();
    expect(resolvePresentation({ ...spec, kind: "sankey" }, "主流量").lead).toBeNull();
  });
});

describe("mutedOf · 浅调不硬编", () => {
  test("浅色主题：向浅 surface 插值 → 变浅", () => {
    // #b04a2a toward #faf9f6 by 70% → each channel approaches surface
    expect(mutedOf("#b04a2a", "#faf9f6")).toBe("#e4c5b9");
  });

  test("深色主题：向深 surface 插值 → 变深（同一公式自动成立）", () => {
    expect(mutedOf("#b04a2a", "#1b1a17")).toBe("#48281d");
  });

  test("非 hex 输入 fail-open 原色返回", () => {
    expect(mutedOf("rgb(1,2,3)", "#ffffff")).toBe("rgb(1,2,3)");
  });
});

describe("图注是罗列时没有主角（116 语料 631 张有副标题的图里 17 张栽在这）", () => {
  test("★★ 副标题只是把所有品类列了一遍 ⇒ 不许挑一个加粗、把其余九条调浅", () => {
    // Real device on 116, 2026-09-05: subtitle of `2026年Top10品类大类月度营业额趋势` lists all ten
    // categories; old longest-name rule highlights `护肤美妆用品`, while body says "女装一骑绝尘".
    const cats = ["女装", "鞋包皮件", "金银珠宝", "护肤美妆用品", "钟表/眼镜", "运动休闲", "日用针纺", "男装", "童装", "内暖衣"];
    const spec: ChartSpec = {
      kind: "line",
      title: "Top10 品类月度趋势",
      subtitle: `${cats.join("、")}（1-9月）`,
      columns: [
        { key: "月份", header: "月份", type: "date" },
        ...cats.map((c) => ({ key: c, header: c, type: "numeric" as const })),
      ],
      rows: [{ 月份: "1月", ...Object.fromEntries(cats.map((c, i) => [c, 100 + i])) }],
      row_count: 1,
    };
    expect(resolvePresentation(spec, null).lead).toBeNull();
  });

  test("★★ 副标题是个**公式**时更不能挑主角（`客单价 = 营业额 / 客流量`）", () => {
    const spec: ChartSpec = {
      kind: "dual_axis",
      title: "2026年品牌客单价 Top 20",
      subtitle: "客单价 = 营业额 / 客流量",
      columns: [
        { key: "b", header: "品牌", type: "categorical" },
        { key: "客单价", header: "客单价", type: "numeric" },
        { key: "营业额", header: "营业额", type: "numeric" },
        { key: "客流量", header: "客流量", type: "numeric" },
      ],
      rows: [{ b: "A", 客单价: 1, 营业额: 2, 客流量: 3 }],
      row_count: 1,
    };
    expect(resolvePresentation(spec, null).lead).toBeNull();
  });

  test("★ 先出现者胜，不再是「名字最长者胜」", () => {
    // For "女装占37.2%，鞋包皮件占15.1%", old rule chooses `鞋包皮件` (4 characters),
    // although the sentence is about `女装`.
    // ⚠ Need enough categories: mentioning both of only two candidates is an A vs B comparison,
    //   which should cancel protagonist under enumeration criterion below (`Top10 & Bottom5` in corpus is such a case).
    const spec = barSpec({ subtitle: "2026年1-7月，女装占37.2%，鞋包皮件占15.1%" });
    spec.rows = [
      { cat: "鞋包皮件", v: 151 },
      { cat: "女装", v: 372 },
      { cat: "金银珠宝", v: 149 },
      { cat: "运动休闲", v: 90 },
      { cat: "童装", v: 60 },
    ];
    spec.row_count = 5;
    expect(resolvePresentation(spec, null).lead).toEqual({ kind: "element", index: 1 });
  });

  test("★★ 反向钉死：`华东` 与 `华东大区` 同现不算罗列（同一处提及）", () => {
    // Count **distinct mention positions**, rather than matched candidates; counting candidates breaks this existing case.
    const spec = barSpec({
      rows: [{ cat: "华东", v: 1 }, { cat: "华东大区", v: 2 }],
      row_count: 2,
    });
    expect(resolvePresentation(spec, "华东大区领跑全国").lead).toEqual({ kind: "element", index: 1 });
  });

  test("★ 饼图判成罗列后退到「最大扇区」，不是没有主角", () => {
    const spec: ChartSpec = {
      kind: "pie",
      title: "品牌营业额 Top 3 占比",
      subtitle: "金辉福 60.2% | 六福珠宝 21.8% | 阿迪达斯 18.0%",
      columns: [
        { key: "b", header: "品牌", type: "categorical" },
        { key: "v", header: "营业额", type: "numeric" },
      ],
      rows: [{ b: "金辉福", v: 602 }, { b: "六福珠宝", v: 218 }, { b: "阿迪达斯", v: 180 }],
      row_count: 3,
    };
    // Old name-length rule selects "六福珠宝" (middle slice); now falls back to largest slice = "金辉福"
    expect(resolvePresentation(spec, null).lead).toEqual({ kind: "element", index: 0 });
  });
});
