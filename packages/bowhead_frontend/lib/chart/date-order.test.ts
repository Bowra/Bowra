/**
 * Date-axis label order: **two implementations of the same rule, here and backend chart/infer.py _date_axis_sort_key.**
 *
 * Why both need it: this determines rendered order, and backend LTTB downsampling must inspect shapes along
 * **that rendered order** to select correct points (sampling the wrong curve picks the wrong points).
 *
 * ⚠ These fixtures are **verbatim identical** to Python's test_date_axis_order_matches_the_frontend.
 * Rule changes on either side must add new labels to both.
 */
import { describe, expect, it } from "vitest";

import { compareDateAxisValues } from "./date-order";

const PARITY_CASES: Array<[string[], string[]]> = [
  [
    ["2025-11", "2025-1", "2025-2", "2025-10"],
    ["2025-1", "2025-2", "2025-10", "2025-11"],
  ],
  [
    ["2025年11月", "2025年1月", "2024年12月"],
    ["2024年12月", "2025年1月", "2025年11月"],
  ],
  [
    ["2025-01-15", "2025-01-02", "2024-12-31"],
    ["2024-12-31", "2025-01-02", "2025-01-15"],
  ],
  [
    ["Q4", "Q1", "Q10", "Q2"],
    ["Q1", "Q2", "Q4", "Q10"],
  ],
  [
    ["2025Q4", "2025Q1", "2026Q1"],
    ["2025Q1", "2025Q4", "2026Q1"],
  ],
  // ★ Mixed non-date labels: these inputs crashed the entire initial backend chart (pivot-table trailing rows / SQL NULL)
  [
    ["合计", "2025-02", "2025-01"],
    ["2025-01", "2025-02", "合计"],
  ],
  [
    ["2025-02", "Q1", "2025-01"],
    ["2025-01", "2025-02", "Q1"],
  ],
  [
    ["小计", "合计"],
    ["合计", "小计"],
  ],
  [
    ["a", "1"],
    ["1", "a"],
  ],
  // Case does not affect order (backend casefold() / sensitivity:"base" here)
  [
    ["FY25", "fy24", "FY26"],
    ["fy24", "FY25", "FY26"],
  ],
  // Empty values and missing columns (backend str(None) = "None") sort last
  [
    ["2025-2", "", "2025-1", "None"],
    ["2025-1", "2025-2", "None", ""],
  ],
];

describe("时间轴排序（与后端对拍）", () => {
  it.each(PARITY_CASES)("%j 按数值排成 %j", (raw, expected) => {
    expect([...raw].sort(compareDateAxisValues)).toEqual(expected);
  });

  it("★ 这组夹具必须能区分「数值序」与「字面序」，否则是空测", () => {
    // Datarc returns unpadded month buckets; lexical order puts November before February.
    expect(["2025-11", "2025-2"].sort()).toEqual(["2025-11", "2025-2"]);
    expect(["2025-11", "2025-2"].sort(compareDateAxisValues)).toEqual(["2025-2", "2025-11"]);
  });

  it("空值排到最后，不参与比较", () => {
    expect(["2025-2", "", "2025-1"].sort(compareDateAxisValues)).toEqual(["2025-1", "2025-2", ""]);
    expect([null, "2025-1"].sort(compareDateAxisValues)).toEqual(["2025-1", null]);
  });
});
