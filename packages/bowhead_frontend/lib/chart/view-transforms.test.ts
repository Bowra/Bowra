/**
 * Batch 3 §3.2 · Derived-view pure-function tests (same foundation as batch 2 inspection).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  FULL_ROWS_LIMIT,
  REST_BUCKET_KEY,
  TABLE_FULL_ROWS_LIMIT,
  applyView,
  ensureFullRows,
  isViewTruncated,
} from "./view-transforms";
import type { ChartSpec } from "./types";

vi.mock("../api", () => ({
  fetchFilePreview: vi.fn(),
}));

import { fetchFilePreview } from "../api";

const mockPreview = vi.mocked(fetchFilePreview);

function spec(extras: Partial<ChartSpec> = {}): ChartSpec {
  return {
    kind: "bar",
    title: "门店经营",
    columns: [
      { key: "shop", header: "门店", type: "categorical" },
      { key: "amt", header: "营业额", type: "numeric", unit: "万元" },
      { key: "rate", header: "退款率", type: "numeric", percent_scale: "fraction" },
    ],
    // ★ Row order is **deliberately shuffled** and must remain so. Previously descending 100/80/60/40,
    // making unsorted Top-N (direct `rows.slice(0, topN)`) invisible throughout this suite
    // — slicing coincidentally equaled ranking. Recorded customer complaint "Top-5 结果很奇怪" is exactly this:
    // first five in **current row order**, not five largest.
    rows: [
      { shop: "B", amt: 80, rate: 0.03 },
      { shop: "D", amt: 40, rate: 0.02 },
      { shop: "A", amt: 100, rate: 0.05 },
      { shop: "C", amt: 60, rate: 0.09 },
    ],
    row_count: 4,
    data_artifact_id: "art_x",
    ...extras,
  };
}

describe("applyView", () => {
  it("ranks unsafe integers exactly and keeps omitted totals honest", () => {
    const exact = spec({ rows: [
      { shop: "A", amt: "9007199254740993", rate: null },
      { shop: "B", amt: 500, rate: null },
      { shop: "C", amt: "9007199254740995", rate: null },
    ] });
    const ranked = applyView(exact, { topN: 1 });
    expect(ranked.rows!.map((row) => row.amt)).toEqual(["9007199254740995", "9007199254741493"]);
    exact.rows![1]!.amt = 0.25;
    expect(applyView(exact, { topN: 1 }).rows![1]!.amt).toBeNull();
  });

  it("不改原 spec（派生返回，原对象逐字段不动）", () => {
    const original = spec();
    const snapshot = JSON.stringify(original);
    applyView(original, { sort: { key: "amt", dir: "asc" }, topN: 2 });
    expect(JSON.stringify(original)).toBe(snapshot);
  });

  it("排序：数值列按数值比较，不是字典序", () => {
    const s = spec({
      rows: [
        { shop: "A", amt: 9, rate: 0 },
        { shop: "B", amt: 100, rate: 0 },
      ],
      row_count: 2,
    });
    const out = applyView(s, { sort: { key: "amt", dir: "desc" } });
    expect(out.rows!.map((r) => r.amt)).toEqual([100, 9]);
  });

  it("排序：日期列按数字片段比较，不把 11 月排在 2 月前", () => {
    const s = spec({
      columns: [
        { key: "month", header: "月份", type: "date" },
        { key: "amt", header: "营业额", type: "numeric" },
      ],
      rows: [
        { month: "2025-1", amt: 1 },
        { month: "2025-11", amt: 11 },
        { month: "2025-2", amt: 2 },
      ],
      row_count: 3,
    });
    const out = applyView(s, { sort: { key: "month", dir: "asc" } });
    expect(out.rows!.map((row) => row.month)).toEqual(["2025-1", "2025-2", "2025-11"]);
  });

  it("Top-N：可加列求和进「其余」", () => {
    const out = applyView(spec(), { topN: 2 });
    expect(out.rows).toHaveLength(3);
    const rest = out.rows![2];
    expect(rest.shop).toBe(REST_BUCKET_KEY);
    expect(rest.amt).toBe(100); // 60 + 40
    // ★row_count is total source-data rows, unchanged by derived view — footer uses it as
    // total; setting 11 after Top-10 is precisely misrepresentation batch 3 4.1 eliminates (review R3②).
    expect(out.row_count).toBe(4);
  });

  it("★比率列的「其余」置 null——不平均、不置 0（诚实原则）", () => {
    const out = applyView(spec(), { topN: 2 });
    const rest = out.rows![2];
    expect(rest.rate).toBeNull();
    // Negative control: changing to averaging would make these three values 0.055 / 0 / any number
    expect(rest.rate).not.toBe(0);
    expect(rest.rate).not.toBe(0.055);
  });

  it("Top-N 不足阈值 → 不产生「其余」行", () => {
    const out = applyView(spec(), { topN: 10 });
    expect(out.rows).toHaveLength(4);
    expect(out.rows!.some((r) => r.shop === REST_BUCKET_KEY)).toBe(false);
  });

  it("全 tail 为空值的可加列 → 其余也置 null（不是 0）", () => {
    const s = spec({
      rows: [
        { shop: "A", amt: 100, rate: 0.05 },
        { shop: "B", amt: null, rate: null },
      ],
      row_count: 2,
    });
    const out = applyView(s, { topN: 1 });
    expect(out.rows![1].amt).toBeNull();
  });

  it("隐藏系列：留下的列不含被隐藏项；隐藏到一个不剩时 fail-open", () => {
    const kept = applyView(spec(), { hiddenSeries: ["rate"] });
    expect(kept.columns.map((c) => c.key)).toEqual(["shop", "amt"]);
    // Hide all → fail-open keeps first numeric column (original skipped filtering entirely, making click inert)
    const all = applyView(spec(), { hiddenSeries: ["amt", "rate"] });
    expect(all.columns.map((c) => c.key)).toEqual(["shop", "amt"]);
  });

  it("★无维度列（legacy spec 缺 columns）不做 Top-N，不产生空白行", () => {
    const legacy = spec({ columns: [], rows: [{ a: 1 }, { a: 2 }, { a: 3 }], row_count: 3 });
    const out = applyView(legacy, { topN: 1 });
    expect(out.rows).toHaveLength(3);
    expect(out.rows!.some((r) => Object.keys(r).length === 0)).toBe(false);
  });

  it("★均值型/序数型列不可加：其余置 null（客单价加起来同样是编数字）", () => {
    const s = spec({
      columns: [
        { key: "shop", header: "门店", type: "categorical" },
        { key: "aov", header: "客单价_元", type: "numeric" },
        { key: "rank", header: "排名", type: "numeric" },
        { key: "amt", header: "营业额", type: "numeric" },
      ],
      rows: [
        { shop: "A", aov: 100, rank: 1, amt: 10 },
        { shop: "B", aov: 200, rank: 2, amt: 20 },
        { shop: "C", aov: 300, rank: 3, amt: 30 },
      ],
      row_count: 3,
    });
    const out = applyView(s, { topN: 1 });
    // ★ Fixture rows are **ascending** (aov 100/200/300). Ranking uses first numeric column
    // = aov, so Top-1 is C. Old direct slicing selected A (lowest aov),
    // making rest 20+30=50 — that 50 actually guarded the defect itself.
    expect(out.rows![0]!.shop).toBe("C");
    const rest = out.rows![1]!;
    expect(rest.aov).toBeNull();
    expect(rest.rank).toBeNull();
    expect(rest.amt).toBe(30); // Additive values still sum: excluded B(20) + A(10)
  });

  it("showLabels 写进派生 spec.show_data_labels（喂 build-config 策略）", () => {
    expect(applyView(spec(), { showLabels: true }).show_data_labels).toBe(true);
    expect(applyView(spec(), { showLabels: false }).show_data_labels).toBe(false);
    expect(applyView(spec(), {}).show_data_labels).toBeUndefined();
  });
});

describe("R4 review 修复", () => {
  it("★R4-10 英文按 token 判可加性——`Copper Tonnage` 里的 `per ` 不算「每」", () => {
    // Original regex used substring matching (`per\s`), hitting both `Copper Tonnage` / `Paper Sales`,
    // setting these genuinely additive columns null in rest row, displayed —. This repeats the collision R0-1 just
    // fixed for `rate` (corporate/separate) in another regex.
    const s = spec({
      columns: [
        { key: "region", header: "地区", type: "categorical" },
        { key: "copper", header: "Copper Tonnage", type: "numeric" },
        { key: "aov", header: "Avg Order Value", type: "numeric" },
      ],
      rows: [
        { region: "A", copper: 10, aov: 5 },
        { region: "B", copper: 20, aov: 6 },
        { region: "C", copper: 30, aov: 7 },
      ],
      row_count: 3,
    });
    const out = applyView(s, { topN: 1 });
    // As above: ascending fixture (copper 10/20/30), Top-1 is C. Old slicing selected A,
    // making rest 20+30=50.
    expect(out.rows![0]!.region).toBe("C");
    const rest = out.rows![1]!;
    expect(rest.copper).toBe(30); // Additive: genuine sum (excluded B20 + A10)
    expect(rest.aov).toBeNull(); // Mean-type: still never sum
  });

  it("★R6-4 折叠了尾巴但行数没变，照样算 partial（topN+1 == 源行数）", () => {
    // applyView Top-N outputs head + rest = topN + 1 rows. Top-3 on four rows
    // has four rows before/after; counts alone say no truncation → reference line merely says "均值",
    // but averages three points plus one aggregate bucket.
    const s = spec(); // 4 rows
    const out = applyView(s, { topN: 3 });
    expect(out.rows).toHaveLength(4); // Row count genuinely unchanged
    expect(isViewTruncated(s, out)).toBe(true);
  });

  it("★R5-5 只有**真**截断才算 partial（点了 Top-N 不等于截到了东西）", () => {
    const s = spec(); // 4 rows
    // Top-10 > 4 rows → applyView truncates nothing
    expect(isViewTruncated(s, applyView(s, { topN: 10 }))).toBe(false);
    // Top-2 → genuinely truncated (2 + rest)
    expect(isViewTruncated(s, applyView(s, { topN: 2 }))).toBe(true);
    // Hide one series → fewer columns, also counts
    expect(isViewTruncated(s, applyView(s, { hiddenSeries: ["rate"] }))).toBe(true);
    // Nothing selected
    expect(isViewTruncated(s, applyView(s, {}))).toBe(false);
    // Legacy spec without dimension column: applyView skips Top-N and must not report partial
    const legacy = spec({
      columns: [{ key: "amt", header: "营业额", type: "numeric" }],
      rows: [{ amt: 1 }, { amt: 2 }, { amt: 3 }],
      row_count: 3,
    });
    expect(isViewTruncated(legacy, applyView(legacy, { topN: 1 }))).toBe(false);
  });

  it("★R5-4 复数形式同样不可加（token 化不能把旧正则的覆盖丢掉）", () => {
    const s = spec({
      columns: [
        { key: "region", header: "地区", type: "categorical" },
        { key: "sc", header: "Scores", type: "numeric" },
        { key: "rk", header: "Rankings", type: "numeric" },
        { key: "av", header: "Averages", type: "numeric" },
      ],
      rows: [
        { region: "A", sc: 90, rk: 1, av: 5 },
        { region: "B", sc: 80, rk: 2, av: 6 },
        { region: "C", sc: 70, rk: 3, av: 7 },
      ],
      row_count: 3,
    });
    const rest = applyView(s, { topN: 1 }).rows![1]!;
    expect(rest.sc).toBeNull();
    expect(rest.rk).toBeNull();
    expect(rest.av).toBeNull();
  });

  it("★R4-6 「其余」类目走调用方给的译文；不给才退回哨兵", () => {
    const withLabel = applyView(spec(), { topN: 2 }, { restLabel: "其余" }).rows![2]!;
    expect(withLabel.shop).toBe("其余");
    const bare = applyView(spec(), { topN: 2 }).rows![2]!;
    expect(bare.shop).toBe(REST_BUCKET_KEY);
  });
});

// ─────────────────────────────────────────────────────────────
// ★★ Top-N must genuinely select N largest values
//
// Customer recording: report chart is value-descending Top-30; opening "细看" and selecting Top-5 instead gives
// first five CSV rows. Two causes combine:
//   ① `applyView` Top-N directly uses `rows.slice(0, topN)`, **without any sorting**
//      (sorting occurs only on explicit `view.sort`);
//   ② on entry, `ensureFullRows` uses `/preview` (`files.py` reads CSV row by row, zero sorting)
//      to replace backend-sorted rows with **original file order**.
// Sorting must match backend `_take_top_n` (`bowhead_core/chart/infer.py`) — inconsistent implementations of one
// concept cause this defect. Backend is guarded by
// `test_hbar_restore_takes_top_by_value_not_head_slice`.
// ─────────────────────────────────────────────────────────────
describe("★★Top-N 的排序口径（与后端 _take_top_n 同一把尺子）", () => {
  it("★乱序进来也要取最大的 N 个，不是当前行序的前 N 个", () => {
    const out = applyView(spec(), { topN: 2 }, { restLabel: "其余" });
    expect(out.rows!.map((r) => r.shop)).toEqual(["A", "B", "其余"]);
    expect(out.rows!.map((r) => r.amt)).toEqual([100, 80, 100]); // Rest = 60 + 40
  });

  it("★升序进来更要排——直接切片拿到的是**最小**的 N 个（后端 hbar 栽过同一个坑）", () => {
    const asc = spec({
      rows: [
        { shop: "D", amt: 40, rate: 0 },
        { shop: "C", amt: 60, rate: 0 },
        { shop: "B", amt: 80, rate: 0 },
        { shop: "A", amt: 100, rate: 0 },
      ],
    });
    expect(applyView(asc, { topN: 2 }).rows!.map((r) => r.amt)).toEqual([100, 80, 100]);
  });

  it("★排名基准 = **第一个数值列**，与后端 `num_cols[0]` 同源", () => {
    // First numeric column is `score`; sorting later `amt` selects entirely different set (A rather than B),
    // so this also guards which column supplies ranking.
    const s = spec({
      columns: [
        { key: "shop", header: "门店", type: "categorical" },
        { key: "score", header: "得分", type: "numeric" },
        { key: "amt", header: "营业额", type: "numeric" },
      ],
      rows: [
        { shop: "A", score: 1, amt: 999 },
        { shop: "B", score: 9, amt: 1 },
        { shop: "C", score: 5, amt: 500 },
      ],
      row_count: 3,
    });
    expect(applyView(s, { topN: 1 }).rows![0]!.shop).toBe("B");
  });

  it("★空值/非数值排在最后（后端把它们当 -∞）", () => {
    const s = spec({
      rows: [
        { shop: "N", amt: null, rate: 0 },
        { shop: "A", amt: 100, rate: 0 },
        { shop: "T", amt: "n/a", rate: 0 },
        { shop: "B", amt: 80, rate: 0 },
      ],
    });
    expect(applyView(s, { topN: 2 }).rows!.map((r) => r.shop)).toEqual(["A", "B", REST_BUCKET_KEY]);
  });

  it("★用户显式设了排序 → 以用户的为准，默认排名不许夺权", () => {
    const out = applyView(spec(), { sort: { key: "amt", dir: "asc" }, topN: 2 });
    expect(out.rows!.map((r) => r.amt)).toEqual([40, 60, 180]); // First two ascending + rest(80+100)
  });

  it("★时间轴：按值选前 N，但**保持时间序**展示（重排出来的折线不是趋势图）", () => {
    const ts = spec({
      columns: [
        { key: "m", header: "月份", type: "date" },
        { key: "amt", header: "营业额", type: "numeric" },
      ],
      // ★★ Largest month must come **later** (value order ≠ time order), otherwise both paths produce same result and
      // this test catches nothing. First draft used February 90 / April 80: value order was also February,
      // April, remaining green without entire order-preserving branch — mutation checking immediately exposed ineffective guard.
      rows: [
        { m: "2026-01", amt: 10 },
        { m: "2026-02", amt: 80 },
        { m: "2026-03", amt: 20 },
        { m: "2026-04", amt: 90 },
      ],
      row_count: 4,
    });
    const out = applyView(ts, { topN: 2 }, { restLabel: "其余" });
    // Selected April(90) and February(80), but display chronologically — February first.
    expect(out.rows!.map((r) => r.m)).toEqual(["2026-02", "2026-04", "其余"]);
    // 80 precedes 90: this **nonmonotonic** sequence itself proves no value-based reordering.
    expect(out.rows!.map((r) => r.amt)).toEqual([80, 90, 30]); // Rest = 10 + 20
  });
});

describe("ensureFullRows", () => {
  beforeEach(() => mockPreview.mockReset());

  it("rows 已够（97.2% 的生产情形）→ 零请求", async () => {
    const out = await ensureFullRows(spec());
    expect(mockPreview).not.toHaveBeenCalled();
    expect(out.truncated).toBe(false);
    expect(out.ok).toBe(true);
  });

  it("无 artifact_id 的截断 spec → 零请求，保持现状", async () => {
    const out = await ensureFullRows(spec({ row_count: 98, data_artifact_id: undefined }));
    expect(mockPreview).not.toHaveBeenCalled();
    expect(out.spec.rows).toHaveLength(4);
    expect(out.ok).toBe(false);
  });

  it("截断 → 拉全量，按 header 名对齐列 key", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: [
        { 门店: "A", 营业额: 100, 退款率: 0.05 },
        { 门店: "E", 营业额: 20, 退款率: 0.01 },
      ],
    } as never);
    const out = await ensureFullRows(spec({ row_count: 2, rows: [{ shop: "A", amt: 100, rate: 0.05 }] }));
    expect(mockPreview).toHaveBeenCalledWith("art_x", { mode: "rows", maxRows: 1000 });
    expect(out.spec.rows).toEqual([
      { shop: "A", amt: 100, rate: 0.05 },
      { shop: "E", amt: 20, rate: 0.01 },
    ]);
    expect(out.truncated).toBe(false);
  });

  // Expanded CSV numeric columns retain values instead of rendering empty cells.
  //
  // ⚠ Header-alignment mock above supplies numeric values as **numbers** — our own
  // constructed happy path. Actual preview parses CSV and returns **all strings**,
  // hiding bug entirely in that fixture. Mock below uses actual device shape.

  it("preview 回来的数字字符串必须归一成 number（否则数值列整列渲成 —）", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      // Actual device shape (art_0ac892c9513f /files/{id}/preview measurement): all values str
      columns: ["门店", "营业额", "退款率"],
      rows: [
        { 门店: "A", 营业额: "88494000.0", 退款率: "0.05" },
        { 门店: "E", 营业额: "1,234.5", 退款率: "0.01" },
      ],
    } as never);
    const out = await ensureFullRows(spec({ row_count: 2, rows: [{ shop: "A", amt: 100, rate: 0.05 }] }));
    expect(out.ok).toBe(true);
    // Numeric column → number (renderer accepts only `typeof v === "number"`)
    expect(out.spec.rows![0]!.amt).toBe(88494000);
    expect(out.spec.rows![1]!.amt).toBe(1234.5); // Accept thousands separators
    expect(out.spec.rows![0]!.rate).toBe(0.05);
    // Categorical columns stay strings; never coerce them to numbers
    expect(out.spec.rows![0]!.shop).toBe("A");
    for (const r of out.spec.rows ?? []) {
      expect(typeof r.amt).toBe("number");
    }
  });

  it("preserves unsafe integer digits when completing CSV rows", async () => {
    mockPreview.mockResolvedValue({
      family: "table", mode: "rows", text: null, columns: ["门店", "营业额", "退款率"],
      rows: [
        { 门店: "A", 营业额: "9007199254740993", 退款率: "0.05" },
        { 门店: "B", 营业额: "9007199254740997.0", 退款率: "0.01" },
        { 门店: "C", 营业额: "9.007199254740995e15", 退款率: "0.01" },
      ],
    } as never);
    const out = await ensureFullRows(spec({ row_count: 3, rows: [{ shop: "A", amt: 1, rate: 0.05 }] }));
    expect(out.ok).toBe(true);
    expect(out.spec.rows!.map((row) => row.amt)).toEqual(["9007199254740993", "9007199254740997", "9007199254740995"]);
  });

  it("★ 带符号的百分号与千分位：与后端 `_FMT_PERCENT_RE`/`_FMT_GROUPED_RE` 逐字对齐", async () => {
    // Backend accepts `+1.50%` as numeric column; rejecting here renders whole column — on "展开 / 细看" completion
    // (same column arriving with different types through two paths is common shape of this bug family).
    vi.mocked(fetchFilePreview).mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: [
        { 门店: "A", 营业额: "+1,234.50", 退款率: "+1.50%" },
        { 门店: "B", 营业额: "-2,345.00", 退款率: "-0.35%" },
      ],
    } as never);
    const source = spec({ row_count: 2, rows: [{ shop: "A", amt: 1, rate: 1 }] });
    source.columns[2]!.percent_scale = "points";
    const out = await ensureFullRows(source);
    expect(out.ok).toBe(true);
    expect(out.spec.rows![0]!.amt).toBe(1234.5);
    expect(out.spec.rows![0]!.rate).toBe(1.5);
    expect(out.spec.rows![1]!.amt).toBe(-2345);
    expect(out.spec.rows![1]!.rate).toBe(-0.35);
  });

  it("数值列里真的非数字 → null（渲成 —），不是把原串塞回去", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: [
        { 门店: "A", 营业额: "N/A", 退款率: "" },
        { 门店: "B", 营业额: "1,2,3", 退款率: "0.02" },
      ],
    } as never);
    const out = await ensureFullRows(spec({ row_count: 2, rows: [{ shop: "A", amt: 100, rate: 0.05 }] }));
    expect(out.spec.rows![0]!.amt).toBeNull();
    expect(out.spec.rows![0]!.rate).toBeNull(); // Empty string is not 0
    expect(out.spec.rows![1]!.amt).toBeNull(); // Never swallow "1,2,3" as 123
    expect(out.spec.rows![1]!.rate).toBe(0.02);
  });

  it("已经是 number 的 preview 逐值不变（回归闸）", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: [
        { 门店: "A", 营业额: 100, 退款率: 0.05 },
        { 门店: "B", 营业额: 80, 退款率: 0.03 },
      ],
    } as never);
    const out = await ensureFullRows(spec({ row_count: 2, rows: [{ shop: "A", amt: 1, rate: 0.1 }] }));
    expect(out.spec.rows).toEqual([
      { shop: "A", amt: 100, rate: 0.05 },
      { shop: "B", amt: 80, rate: 0.03 },
    ]);
  });

  // ── maxRows (chart-config-ux batch 5: raise table row limit after virtual scrolling) ──────────
  it("★缺省走图的上限——检视层的图不能被喂进上万个几何图元", async () => {
    mockPreview.mockResolvedValue({
      family: "table", mode: "rows", text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: [{ 门店: "A", 营业额: 1, 退款率: 0 }],
    } as never);
    await ensureFullRows(spec({ row_count: 98, rows: [{ shop: "A", amt: 1, rate: 0 }] }));
    expect(mockPreview.mock.calls[0]![1]).toMatchObject({ maxRows: FULL_ROWS_LIMIT });
  });

  it("★表格显式传 TABLE_FULL_ROWS_LIMIT 时必须真的发到请求上——只改常量不接线等于没改", async () => {
    mockPreview.mockResolvedValue({
      family: "table", mode: "rows", text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: [{ 门店: "A", 营业额: 1, 退款率: 0 }],
    } as never);
    await ensureFullRows(spec({ row_count: 98, rows: [{ shop: "A", amt: 1, rate: 0 }] }), {
      maxRows: TABLE_FULL_ROWS_LIMIT,
    });
    expect(mockPreview.mock.calls[0]![1]).toMatchObject({ maxRows: 10_000 });
  });

  it("★★表格上限必须与后端 /preview 的 `le=10_000` 同数——喊得比它大只会拿 422", () => {
    // Backend `test_preview_max_rows_cap.py` guards same number (each language guards only its half).
    expect(TABLE_FULL_ROWS_LIMIT).toBe(10_000);
    expect(FULL_ROWS_LIMIT).toBeLessThanOrEqual(TABLE_FULL_ROWS_LIMIT);
  });

  it("超上限 → truncated=true（调用方据此标口径/禁用）", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: Array.from({ length: 1000 }, (_, i) => ({ 门店: `S${i}`, 营业额: i, 退款率: 0.01 })),
    } as never);
    const out = await ensureFullRows(spec({ row_count: 5000, rows: [{ shop: "A", amt: 1, rate: 0 }] }));
    expect(out.truncated).toBe(true);
    expect(out.ok).toBe(true);
    expect(out.spec.row_count).toBe(5000);
  });

  // feedback-triage-2026-08-26: 116 device footer "显示 1–1000 / 100 行".
  // Above test row_count(5000) exceeds fetched rows, coincidentally avoiding defect —
  // gap is when old row_count is **less than** actual rows, so guard separately.
  it("★旧 row_count 小于补回来的行数 → 总数必须跟着抬，页脚不许自相矛盾", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: Array.from({ length: 1000 }, (_, i) => ({ 门店: `S${i}`, 营业额: i, 退款率: 0.01 })),
      total_rows: 4000,
      truncated: true,
    } as never);
    const out = await ensureFullRows(spec({ row_count: 100, rows: [{ shop: "A", amt: 1, rate: 0 }] }));
    expect(out.spec.rows).toHaveLength(1000);
    // Express invariant (displayed count ≤ total), not just fixed 4000 — must survive different fetching conventions.
    expect(out.spec.row_count!).toBeGreaterThanOrEqual(out.spec.rows!.length);
    expect(out.spec.row_count).toBe(4000);
    // Genuine total 4000 > available 1000 ⇒ admit incomplete data, or "展开全部" misrepresents completion
    expect(out.truncated).toBe(true);
  });

  it("★preview 不报 total_rows 时，总数至少不小于实际行数", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: Array.from({ length: 300 }, (_, i) => ({ 门店: `S${i}`, 营业额: i, 退款率: 0.01 })),
    } as never);
    const out = await ensureFullRows(spec({ row_count: 100, rows: [{ shop: "A", amt: 1, rate: 0 }] }));
    expect(out.spec.row_count!).toBeGreaterThanOrEqual(out.spec.rows!.length);
    expect(out.truncated).toBe(false);
  });

  it("★数据源自报 truncated 就认，不拿行数去推翻它", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: Array.from({ length: 50 }, (_, i) => ({ 门店: `S${i}`, 营业额: i, 退款率: 0.01 })),
      total_rows: 50,
      truncated: true,
    } as never);
    const out = await ensureFullRows(spec({ row_count: 10, rows: [{ shop: "A", amt: 1, rate: 0 }] }));
    expect(out.spec.row_count).toBe(50);
    expect(out.truncated).toBe(true);
  });

  it("列名一个都对不上 → 保留原 rows（宁可不展开也不错位）", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["完全不相干的列"],
      rows: [{ 完全不相干的列: 1 }],
    } as never);
    const out = await ensureFullRows(spec({ row_count: 98, rows: [{ shop: "A", amt: 1, rate: 0 }] }));
    expect(out.spec.rows).toEqual([{ shop: "A", amt: 1, rate: 0 }]);
  });

  it("preview 抛错（分享报告 403）→ fail-open 保持现状", async () => {
    // ⚠ Must use mockImplementationOnce: earlier tests used same mock
    // with mockResolvedValue; a **persistent** throwing mockImplementation afterward is recorded by vitest
    // as failure here (minimal reproduction verified: resolve→reset→throw fails), although actual
    // tested behavior is correct (debugging confirms ensureFullRows did not throw and returned fail-open result).
    // Once form expresses same contract without masking any genuine defect.
    mockPreview.mockImplementationOnce(() => {
      throw new Error("403");
    });
    const out = await ensureFullRows(spec({ row_count: 98 }));
    expect(out.spec.rows).toHaveLength(4);
    // review R1: failure must return ok:false and honest truncated (available 4 rows < 98)
    expect(out.ok).toBe(false);
    expect(out.truncated).toBe(true);
  });

  it("★部分列对得上也不替换（未映射列会整列渲成 —，比不展开更糟）", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "销售额(元)"], // Only first column matches
      rows: [{ 门店: "A", "销售额(元)": 1 }, { 门店: "B", "销售额(元)": 2 }],
    } as never);
    const out = await ensureFullRows(spec({ row_count: 98, rows: [{ shop: "A", amt: 1, rate: 0 }] }));
    expect(out.ok).toBe(false);
    expect(out.spec.rows).toEqual([{ shop: "A", amt: 1, rate: 0 }]);
  });

  it("★R4-13 两个 spec 列解析到同一个 preview 列时，展开不该整个失败", async () => {
    // Duplicate headers / A.header === B.key are both common in LLM-generated specs. Original used
    // **preview column name** as map key, collapsing two entries → size < specCols.length →
    // always returned "加载完整数据失败" despite every column matching.
    const dupSpec = spec({
      row_count: 3,
      rows: [{ shop: "A", amt: 1, rate: 0 }],
      columns: [
        { key: "shop", header: "门店", type: "categorical" },
        { key: "amt", header: "营业额", type: "numeric" },
        { key: "amt_dup", header: "营业额", type: "numeric" }, // Identical headers
      ],
    });
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额"],
      rows: [
        { 门店: "A", 营业额: 1 },
        { 门店: "B", 营业额: 2 },
        { 门店: "C", 营业额: 3 },
      ],
    } as never);
    const out = await ensureFullRows(dupSpec);
    expect(out.ok).toBe(true);
    expect(out.spec.rows).toHaveLength(3);
    expect(out.spec.rows![1]).toEqual({ shop: "B", amt: 2, amt_dup: 2 });
  });

  it("★ 数据不全时「其余」一律不给小计——加不全的和不是和", () => {
    // user 2026-09-02: subtotal despite incomplete fetch, "还不如不要聚合值".
    // Tail lacks rows; sum covers only loaded tail, not all remaining rows.
    const full = applyView(spec(), { topN: 2 }, { restLabel: "其余" });
    expect(full.rows![2].amt).toBe(100); // 60 + 40; still sum normally with complete data
    const part = applyView(spec({ row_count: 99 }), { topN: 2 }, { restLabel: "其余", partial: true });
    expect(part.rows![2].amt).toBeNull();
    expect(part.rows![2].rate).toBeNull(); // Ratio columns already omit values; preserve that
    expect(part.rows![2].shop).toBe("其余"); // Row remains, just without numeric value
  });

  it("拿回来的比手里还少 → 不替换（替换等于凭空丢行）", async () => {
    mockPreview.mockResolvedValue({
      family: "table",
      mode: "rows",
      text: null,
      columns: ["门店", "营业额", "退款率"],
      rows: [{ 门店: "A", 营业额: 1, 退款率: 0 }],
    } as never);
    const out = await ensureFullRows(spec({ row_count: 98 })); // 4 available rows
    expect(out.ok).toBe(false);
    expect(out.spec.rows).toHaveLength(4);
  });
});
