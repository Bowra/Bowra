/**
 * Map data_table → ChartSpec, enforcing **capability relocation without loss**:
 * assert each capability unique to the old DataTableSection renderer (198 lines).
 */

import { describe, expect, it } from "vitest";

import { dataTableToRendering, normalizeLegacyTableShape, TOTAL_ROW_FLAG } from "./data-table-to-spec";
import type { ReportSection } from "@/lib/types";

type DT = Extract<ReportSection, { kind: "data_table" }>;

/** Actual 116 production section shape (all ten sections match, checked verbatim against canvas_sections). */
const real = (): DT => ({
  id: "sec_27422d932ba8",
  kind: "data_table",
  caption: "2026年Q1 各医院等级 销售金额达成率",
  columns: [
    { key: "医院等级", header: "医院等级", numeric: false },
    { key: "销售金额达成率", header: "销售金额达成率", numeric: true },
  ],
  rows: [
    { 医院等级: "Tier 2", 销售金额达成率: 0.9127463390158274 },
    { 医院等级: "Tier 3", 销售金额达成率: 0.8853017746620391 },
  ],
} as DT);

describe("data_table → ChartSpec", () => {
  it("caption → title；列的 numeric 布尔 → ChartColumn.type", () => {
    const { spec } = dataTableToRendering(real());
    expect(spec.kind).toBe("table");
    expect(spec.title).toBe("2026年Q1 各医院等级 销售金额达成率");
    expect(spec.columns?.map((c) => c.type)).toEqual(["categorical", "numeric"]);
    expect(spec.row_count).toBe(2);
  });

  it("★ row_count 必须是真行数——`ChartTable` 的页脚和「展开全部 N 行」都读它", () => {
    const d = real();
    d.rows = Array.from({ length: 37 }, (_, i) => ({ 医院等级: `T${i}`, 销售金额达成率: i / 100 }));
    expect(dataTableToRendering(d).spec.row_count).toBe(37);
  });

  it("★ totalRow → 末尾一行 + 标记键（不是「最后一行就算合计」）", () => {
    const d = real();
    d.totalRow = { 医院等级: "合计", 销售金额达成率: 1 };
    const { spec } = dataTableToRendering(d);
    expect(spec.rows).toHaveLength(3);
    expect((spec.rows![2] as Record<string, unknown>)[TOTAL_ROW_FLAG]).toBe(true);
    // Without totalRow, **no row** may carry the flag, or actual data would render as totals
    expect(dataTableToRendering(real()).spec.rows!.some(
      (r) => (r as Record<string, unknown>)[TOTAL_ROW_FLAG],
    )).toBe(false);
  });

  it("★ sandbox 徽标与原文件名搬过来了", () => {
    const d = real();
    d.meta = { source: "python_sandbox", artifact_id: "art_x", filename: "out.csv" };
    const r = dataTableToRendering(d);
    expect(r.sandboxBadge).toBe(true);
    expect(r.downloadFilename).toBe("out.csv");
    // Wire artifact to data_artifact_id for ChartTable's "打开数据/下载" actions to recognize it
    expect(r.spec.data_artifact_id).toBe("art_x");
  });

  it("非 sandbox 段不出徽标", () => {
    const r = dataTableToRendering(real());
    expect(r.sandboxBadge).toBe(false);
    expect(r.downloadFilename).toBeNull();
  });
});

describe("legacy 形态（0.39.8 #4）", () => {
  it("★ string[] 列 + 数组行 → 正常 key/header 与 dict 行", () => {
    const out = normalizeLegacyTableShape(["省", "营业额"], [["上海", 100], ["北京", 90]]);
    expect(out).not.toBeNull();
    // ★ Infer numeric **from data** (legacy column names lack this information)
    expect(out!.columns).toEqual([
      { key: "省", header: "省", numeric: false },
      { key: "营业额", header: "营业额", numeric: true },
    ]);
    expect(out!.rows).toEqual([{ 省: "上海", 营业额: 100 }, { 省: "北京", 营业额: 90 }]);
  });

  it("★★ 首格为空时要**往后翻**再判 numeric（真 prod 段 sec_00f08ec49602 就是这形状）", () => {
    // row[0] = ["", 9905]: checking only the first row would classify col_a as numeric (right alignment + numeric formatting).
    const out = normalizeLegacyTableShape(["col_a", "col_b"], [["", 9905], ["Tier 1", 1024]]);
    expect(out!.columns.map((c) => c.numeric)).toEqual([false, true]);
  });

  it("★ 新形态必须返回 null（否则每段都白转一次、还可能转坏）", () => {
    expect(normalizeLegacyTableShape(
      [{ key: "省", header: "省" }],
      [{ 省: "上海" }],
    )).toBeNull();
  });

  it("整条链路：legacy 段也能映射出可渲染的 spec", () => {
    const d = { ...real(), columns: ["省", "额"], rows: [["上海", 1]] } as unknown as DT;
    const { spec } = dataTableToRendering(d);
    expect(spec.columns?.map((c) => c.key)).toEqual(["省", "额"]);
    expect(spec.rows).toEqual([{ 省: "上海", 额: 1 }]);
  });
});

it("preserves declared report-table semantics without changing values", () => {
  const table = real();
  table.columns[1] = { ...table.columns[1]!, unit: "$", currency_code: "CAD", format: "currency", display: { decimal_places: 3, decimal_mode: "fixed" } };
  const rendered = dataTableToRendering(table);
  expect(rendered.spec.columns[1]).toMatchObject({ currency_code: "CAD", unit: "$", display: { decimal_places: 3, decimal_mode: "fixed" } });
  expect(rendered.spec.rows).toEqual(table.rows);
});
