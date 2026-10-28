/**
 * Batch 3 4.1 · table collapse/expand/sorting guard/exact format (chart-table-ux).
 *
 * Pin three user-visible contracts:
 *  1. Default 10 rows + "展开全部 N 行", where N is row_count, not available-row count;
 *  2. Expansion uses ensureFullRows to complete truncated data (old paginator paginated partial data,
 *     yet footer said /98 — false reporting);
 *  3. Disable sorting while collapsed (sorting only available ten rows = false Top10).
 */

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChartTable } from "./chart-table";
import type { ChartSpec } from "@/lib/chart/types";

const { mockLocaleRef } = vi.hoisted(() => ({ mockLocaleRef: { v: "zh-CN" } }));
vi.mock("next-intl", () => ({
  // Chart component localizes x-axis time buckets / granularity column names (2026-09-01) —
  // component really calls useLocale(); missing mock throws throughout file.
  useLocale: () => mockLocaleRef.v,
  useTranslations: () => (key: string, values?: Record<string, unknown>) => {
    if (key === "showing") return `Showing ${values?.start}-${values?.end} of ${values?.total}`;
    if (key === "expand_all") return `Show all ${values?.total} rows`;
    if (key === "expand_truncated") return `Loaded first ${values?.limit} rows`;
    const labels: Record<string, string> = {
      open_data: "Open Data",
      download_csv: "Download CSV",
      more_actions: "More actions",
      expanding: "Loading",
      sort_needs_expand: "Expand to sort",
    };
    return labels[key] ?? key;
  },
}));

vi.mock("@/lib/chart/view-transforms", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/chart/view-transforms")>();
  return { ...actual, ensureFullRows: vi.fn() };
});

import { ensureFullRows } from "@/lib/chart/view-transforms";

const mockEnsure = vi.mocked(ensureFullRows);

/** After quiet-by-default behavior, action buttons live in `⋯`; open menu before clicking them.
 *  (Chart-kind switcher stays outside, persistently visible; see `table-actions.tsx` comment.) */
function openMoreActions() {
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
}

function spec(rowCount: number, rowsInHand: number): ChartSpec {
  return {
    kind: "table",
    title: "门店明细",
    columns: [
      { key: "shop", header: "门店", type: "categorical" },
      { key: "amt", header: "营业额", type: "numeric" },
    ],
    rows: Array.from({ length: rowsInHand }, (_, i) => ({ shop: `S${i}`, amt: 1000 + i })),
    row_count: rowCount,
    data_artifact_id: "art_x",
  };
}

afterEach(() => cleanup());
beforeEach(() => mockEnsure.mockReset());

describe("ChartTable observation values", () => {
  it("shares ordinary money and percentage precision on screen and in static output", () => {
    const observed: ChartSpec = {
      kind: "table", title: "Observed values",
      columns: [
        { key: "period", header: "Period", type: "categorical" },
        { key: "amount", header: "Revenue", type: "numeric", unit: "USD" },
        { key: "margin", header: "Margin", type: "numeric", format: "percent", percent_scale: "fraction" },
      ],
      rows: [{ period: "Q1", amount: 1234.56789, margin: 0.1744 }, { period: "Q2", amount: null, margin: 0 }],
      row_count: 2,
    };
    for (const surface of ["canvas", "export"] as const) {
      const result = render(<ChartTable spec={observed} surface={surface} />);
      expect(screen.getByText("Revenue (USD)")).toBeInTheDocument();
      expect(screen.getByText("1,234.57")).toBeInTheDocument();
      expect(screen.getByText("17.44%")).toBeInTheDocument();
      expect(screen.getByText("0%")).toBeInTheDocument();
      expect(screen.getByText("—")).toBeInTheDocument();
      result.unmount();
    }
  });
});

describe("ChartTable unit placement", () => {
  it("shows units once in the header instead of gluing them to every cell", () => {
    const spec: ChartSpec = {
      kind: "table", title: "Sales",
      columns: [
        { key: "region", header: "区域", type: "categorical" },
        { key: "sales", header: "销售金额", type: "numeric", unit: "工作区原始金额单位" },
        { key: "qty", header: "销量", type: "numeric", unit: "件" },
      ],
      rows: [{ region: "华东", sales: 170789558.1701, qty: 12 }],
      row_count: 1,
    };
    for (const surface of ["canvas", "export"] as const) {
      const result = render(<ChartTable spec={spec} surface={surface} />);
      expect(screen.getByRole("columnheader", { name: "销售金额（工作区原始金额单位）" })).toBeInTheDocument();
      expect(screen.getByText("170,789,558.1701")).toBeInTheDocument();
      expect(screen.queryByText(/170,789,558\.1701工作区/)).toBeNull();
      // Short units are named in the header too; only percentage marks stay in cells.
      expect(screen.getByRole("columnheader", { name: "销量（件）" })).toBeInTheDocument();
      expect(screen.getByText("12")).toBeInTheDocument();
      result.unmount();
    }
  });
});

describe("ChartTable 折叠", () => {
  it("默认只渲 10 行，按钮写 row_count 的总数（不是手里的行数）", () => {
    render(<ChartTable spec={spec(98, 50)} />);
    expect(screen.getAllByRole("row")).toHaveLength(11); // Header + 10
    expect(screen.getByRole("button", { name: "Show all 98 rows" })).toBeInTheDocument();
    expect(screen.getByText(/Showing 1-10 of 98/)).toBeInTheDocument();
  });

  it("总行数不超过阈值 → 不出展开按钮，且排序可用", () => {
    render(<ChartTable spec={spec(4, 4)} />);
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
    fireEvent.click(screen.getByText("营业额"));
    expect(screen.getByText("营业额").closest("th")).toHaveAttribute("aria-sort", "descending");
  });

  it("★展开走 ensureFullRows 补齐截断数据，渲的是补齐后的行", async () => {
    mockEnsure.mockResolvedValue({
      spec: { ...spec(98, 50), rows: Array.from({ length: 98 }, (_, i) => ({ shop: `S${i}`, amt: 1000 + i })) },
      truncated: false,
      ok: true,
    });
    render(<ChartTable spec={spec(98, 50)} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 98 rows" }));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(99));
    expect(mockEnsure).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Showing 1-98 of 98/)).toBeInTheDocument();
  });

  it("超上限时页脚标明只加载了前 N 行（不假装是全量）", async () => {
    mockEnsure.mockResolvedValue({
      spec: { ...spec(5000, 50), rows: Array.from({ length: 1000 }, (_, i) => ({ shop: `S${i}`, amt: i })) },
      truncated: true,
      ok: true,
    });
    render(<ChartTable spec={spec(5000, 50)} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 5000 rows" }));
    await waitFor(() => expect(screen.getByText(/Loaded first 1000 rows/)).toBeInTheDocument());
  });

  it("★折叠态点表头不排序（负对照：展开后同一次点击生效）", async () => {
    mockEnsure.mockResolvedValue({ spec: spec(98, 98), truncated: false, ok: true });
    render(<ChartTable spec={spec(98, 50)} />);
    fireEvent.click(screen.getByText("营业额"));
    expect(screen.getByText("营业额").closest("th")).not.toHaveAttribute("aria-sort");
    expect(screen.getByText(/Expand to sort/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show all 98 rows" }));
    await waitFor(() => expect(screen.queryByText(/Expand to sort/)).toBeNull());
    fireEvent.click(screen.getByText("营业额"));
    expect(screen.getByText("营业额").closest("th")).toHaveAttribute("aria-sort", "descending");
  });

  it("数值列带 is-num（右对齐 + tabular-nums 的挂点）", () => {
    const { container } = render(<ChartTable spec={spec(4, 4)} />);
    const heads = [...container.querySelectorAll("th")].map((el) => el.className);
    expect(heads).toEqual([undefined, "is-num"].map((c) => c ?? ""));
    expect(container.querySelectorAll("td.is-num")).toHaveLength(4);
  });

  it("★导出面不折叠、不出展开按钮（PDF 里没有可点的东西）", () => {
    mockEnsure.mockResolvedValue({ spec: spec(20, 20), truncated: false, ok: true });
    render(<ChartTable spec={spec(20, 20)} surface="export" />);
    expect(screen.getAllByRole("row")).toHaveLength(21); // Header + 20 rows, uncollapsed
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  it("★导出面数据不全时自动补全量（此前 ensureFullRows 只有交互展开会调，PDF 拿的是半截）", async () => {
    mockEnsure.mockResolvedValue({
      spec: { ...spec(98, 50), rows: Array.from({ length: 98 }, (_, i) => ({ shop: `S${i}`, amt: i })) },
      truncated: false,
      ok: true,
    });
    render(<ChartTable spec={spec(98, 50)} surface="export" />);
    await waitFor(() => expect(mockEnsure).toHaveBeenCalledTimes(1));
    // Footer must state actual total, not available 50
    await waitFor(() => expect(screen.getByText(/of 98/)).toBeInTheDocument());
  });

  it("★导出面渲染上限内渲，页脚写明总数（导出页 1600×900 硬裁，渲全量等于被切一半）", async () => {
    mockEnsure.mockResolvedValue({
      spec: { ...spec(98, 50), rows: Array.from({ length: 98 }, (_, i) => ({ shop: `S${i}`, amt: i })) },
      truncated: false,
      ok: true,
    });
    render(<ChartTable spec={spec(98, 50)} surface="export" />);
    await waitFor(() => expect(screen.getByText(/of 98/)).toBeInTheDocument());
    // Header + 25-row limit
    expect(screen.getAllByRole("row")).toHaveLength(26);
  });

  it("导出面数据已全 → 不发请求", () => {
    render(<ChartTable spec={spec(4, 4)} surface="export" />);
    expect(mockEnsure).not.toHaveBeenCalled();
  });

  it("★拉取失败不得标记成展开成功（否则排序解禁 = 对半截数据排假 Top10）", async () => {
    // ensureFullRows catches 403 and returns ok:false; component must remain collapsed and disclose failure
    mockEnsure.mockResolvedValue({ spec: spec(98, 50), truncated: true, ok: false });
    render(<ChartTable spec={spec(98, 50)} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 98 rows" }));
    await waitFor(() => expect(screen.getByText(/expand_failed/)).toBeInTheDocument());
    // Still collapsed: 10 rows, expand button remains, sorting locked
    expect(screen.getAllByRole("row")).toHaveLength(11);
    expect(screen.getByRole("button", { name: "Show all 98 rows" })).toBeInTheDocument();
    expect(screen.getByText(/Expand to sort/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("营业额"));
    expect(screen.getByText("营业额").closest("th")).not.toHaveAttribute("aria-sort");
  });

  it("★换了 spec 要丢掉上一张图补来的行（ChatInlineChart 没有 key 重挂）", async () => {
    mockEnsure.mockResolvedValue({
      spec: { ...spec(98, 50), rows: Array.from({ length: 98 }, (_, i) => ({ shop: `S${i}`, amt: i })) },
      truncated: false,
      ok: true,
    });
    const { rerender } = render(<ChartTable spec={spec(98, 50)} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 98 rows" }));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(99));
    rerender(<ChartTable spec={spec(3, 3)} />);
    expect(screen.getAllByRole("row")).toHaveLength(4); // Header + new spec's 3 rows
  });

  it("★R5-3 同一个 spec 对象重复渲染不重置（调用方必须 memo，ChartBlock 已 memo）", async () => {
    mockEnsure.mockResolvedValue({
      spec: { ...spec(98, 50), rows: Array.from({ length: 98 }, (_, i) => ({ shop: `S${i}`, amt: i })) },
      truncated: false,
      ok: true,
    });
    const stable = spec(98, 50);
    const { rerender } = render(<ChartTable spec={stable} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 98 rows" }));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(99));
    rerender(<ChartTable spec={stable} />);
    expect(screen.getAllByRole("row")).toHaveLength(99);
  });

  it("★R5-3 行数不变但内容改写了，必须重置（同 artifact 流式更新）", async () => {
    // R4-7 signature `artifact|title|row_count|rows.length` misses this:
    // continues rendering previously fetched data — exactly why effect exists.
    mockEnsure.mockResolvedValue({
      spec: { ...spec(98, 50), rows: Array.from({ length: 98 }, (_, i) => ({ shop: `S${i}`, amt: i })) },
      truncated: false,
      ok: true,
    });
    const { rerender } = render(<ChartTable spec={spec(98, 50)} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 98 rows" }));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(99));
    // Same artifact, row_count, row length; values alone changed
    const rewritten = spec(98, 50);
    rewritten.rows = rewritten.rows!.map((r) => ({ ...r, amt: 777 }));
    rerender(<ChartTable spec={rewritten} />);
    expect(screen.getAllByRole("row")).toHaveLength(11); // Collapse back to default 10 rows
    expect(screen.getAllByRole("cell").some((c) => c.textContent?.includes("777"))).toBe(true);
  });

  it("★R5-3 只改列（检视层关掉一个系列）也必须重置", async () => {
    mockEnsure.mockResolvedValue({
      spec: { ...spec(98, 50), rows: Array.from({ length: 98 }, (_, i) => ({ shop: `S${i}`, amt: i })) },
      truncated: false,
      ok: true,
    });
    const { rerender } = render(<ChartTable spec={spec(98, 50)} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 98 rows" }));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(99));
    const fewerCols = spec(98, 50);
    fewerCols.columns = fewerCols.columns.filter((c) => c.key !== "amt");
    rerender(<ChartTable spec={fewerCols} />);
    expect(screen.queryByRole("columnheader", { name: "营业额" })).toBeNull();
  });

  it("★R4-8 chromeless：层内不渲表自己的标题与动作条（层头层底已经有了）", () => {
    const { container, rerender } = render(<ChartTable spec={spec(3, 3)} />);
    expect(container.querySelector(".v2-chart-header")).not.toBeNull();
    // Positive control: normal surface has actions (inside `⋯`) — otherwise no-render assertion below
    // passes whenever entire action bar disappears for any reason.
    openMoreActions();
    expect(screen.getByRole("menuitem", { name: "Download CSV" })).toBeInTheDocument();
    rerender(<ChartTable spec={spec(3, 3)} chromeless />);
    expect(container.querySelector(".v2-chart-header")).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Download CSV" })).toBeNull();
    // Table body still renders
    expect(screen.getAllByRole("row")).toHaveLength(4);
  });

  it("★导出面不渲动作条，但**标题必须留着**（116 dogfood：PDF 里全是点不了的按钮）", () => {
    // Unlike chromeless, which hides title too (inspection header already has it), export hides only
    // actions: `spec.title` exists only in this header, with no duplicate in PDF body;
    // full chromeless would remove exported chart title.
    const { container, rerender } = render(<ChartTable spec={spec(3, 3)} />);
    openMoreActions(); // Positive control
    expect(screen.getByRole("menuitem", { name: "Download CSV" })).toBeInTheDocument();

    rerender(<ChartTable spec={spec(3, 3)} surface="export" />);
    expect(container.querySelector(".v2-chart-actions")).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Download CSV" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Open Data" })).toBeNull();
    // Title and body still render
    expect(container.querySelector(".v2-chart-header")).not.toBeNull();
    expect(container.querySelector(".v2-chart-title")?.textContent).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(4);
  });

  it("★R6-2 chromeless 下表头排序必须关掉（层内数据可能半截，且含「其余」聚合行）", () => {
    render(<ChartTable spec={spec(98, 30)} chromeless />);
    fireEvent.click(screen.getByText("营业额"));
    expect(screen.getByText("营业额").closest("th")).not.toHaveAttribute("aria-sort");
  });

  it("★R6-5 chromeless 渲全量时必须给滚动容器（否则 sticky 表头又成死代码）", () => {
    const { container } = render(<ChartTable spec={spec(30, 30)} chromeless />);
    expect(container.querySelector(".v2-chart-table-scroll.is-expanded")).not.toBeNull();
  });

  it("★R5-7 chromeless 收掉页脚，同时**不许**折叠（否则变成没有出口的静默截断）", () => {
    // Inspection supplies applyView-derived spec; footer "共 98 行" conflicts with inspection's own counts,
    // and expand-all replaces Top-N and other aggregation with original rows. Hiding footer requires disabling collapse too.
    const { container } = render(<ChartTable spec={spec(30, 30)} chromeless />);
    expect(container.querySelector(".v2-chart-table-footer")).toBeNull();
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
    expect(screen.getAllByRole("row")).toHaveLength(31); // Header + 30 rows, none missing
  });
});

// ── Review coverage addition (2026-09-01): table date columns must follow locale too ────────────────────
// Localized chart x-axis but unlocalized table means same data diverges across surfaces: English users request monthly **table**,
// yet cells still show `2026年1月`.
describe("日期列按 locale 渲染", () => {
  it("en 下中文月份换成英文；空值仍是 —", () => {
    const spec: ChartSpec = {
      kind: "table",
      title: "月度",
      columns: [
        { key: "每月", header: "每月", type: "date" },
        { key: "amt", header: "金额", type: "numeric" },
      ],
      rows: [
        { 每月: "2026年1月", amt: 100 },
        { 每月: null, amt: 200 },
      ],
      row_count: 2,
    };
    mockLocaleRef.v = "zh-CN";
    const zh = render(<ChartTable spec={spec} />);
    expect(zh.container.textContent).toContain("2026年1月");
    expect(zh.container.textContent).toContain("—"); // Missing values remain —
    zh.unmount();

    mockLocaleRef.v = "en-US";
    const en = render(<ChartTable spec={spec} />);
    expect(en.container.textContent).toContain("Jan 2026");
    expect(en.container.textContent).not.toContain("2026年1月");
    expect(en.container.textContent).toContain("—");
    en.unmount();
    mockLocaleRef.v = "zh-CN";
  });
});

describe("☠ 展开态什么时候该丢、什么时候不该丢（客户报「点了展开随后自己收起」）", () => {
  const full = (rowCount: number) => ({
    spec: {
      ...spec(rowCount, rowCount),
      rows: Array.from({ length: rowCount }, (_, i) => ({ shop: `S${i}`, amt: 1000 + i })),
    },
    truncated: false,
    ok: true,
  });

  async function renderExpanded(rowCount = 98) {
    mockEnsure.mockResolvedValue(full(rowCount));
    const view = render(<ChartTable spec={spec(rowCount, 50)} />);
    fireEvent.click(screen.getByRole("button", { name: `Show all ${rowCount} rows` }));
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(rowCount + 1));
    return view;
  }

  it("★★ 换壳不换内容的重渲**不许**收起（这就是客户看到的形状）", async () => {
    const { rerender } = await renderExpanded();
    // Caller creates `{...spec}` every render (`insight-card` retrieves from chartLookup,
    // inspection and future surfaces may too). Former **object identity** checks
    // immediately cleared expansion — users expanded all, then it collapsed itself.
    rerender(<ChartTable spec={{ ...spec(98, 50) }} />);
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(99));
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  // ☠☠ Three cases below **changed assertions on 2026-09-10** (user reported "有时候刚展开就收起来，
  // 有时候过段时间收起"). Formerly asserted content change ⇒ collapse + expand button.
  //
  // **Real reason** for this effect: discard previously fetched rows instead of rendering old data —
  // still valid (`fullSpec` discarded). Mandatory recollapse and another user click
  // were not required by that reason, but consequences of combining **user intent** and **data availability** in
  // one `expanded` flag: discarding data discarded intent too.
  //
  // New semantics separate both; three cases assert real reason + preserved intent:
  //   ① Refetched (`mockEnsure` called again); ② **new** content rendered, not old;
  //   ③ No extra user click required.
  it("★ 同 artifact 流式改写了值、行数没变 → 丢旧行重取，但**不**收起（R4-7 浅签名漏的第①条）", async () => {
    const { rerender } = await renderExpanded();
    expect(mockEnsure).toHaveBeenCalledTimes(1);

    const rewritten = spec(98, 50);
    rewritten.rows![0]!.amt = 999999; // Same row count, changed value
    // Refetch returns **rewritten** full data
    const refreshed = full(98);
    refreshed.spec.rows![0]!.amt = 999999;
    mockEnsure.mockResolvedValue(refreshed);
    rerender(<ChartTable spec={rewritten} />);

    await waitFor(() => expect(mockEnsure).toHaveBeenCalledTimes(2)); // ① Actually refetched
    await waitFor(() => expect(screen.getByText("999,999")).toBeInTheDocument()); // ② New value (exact format includes thousands separators)
    expect(screen.getAllByRole("row")).toHaveLength(99);
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull(); // ③ Still expanded
  });

  it("★ 只改 columns（检视层关掉一个系列）→ 重取但不收起（漏的第②条）", async () => {
    const { rerender } = await renderExpanded();
    const dropped = spec(98, 50);
    dropped.columns = dropped.columns!.slice(0, 1);
    mockEnsure.mockResolvedValue(full(98));
    rerender(<ChartTable spec={dropped} />);

    await waitFor(() => expect(mockEnsure).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  it("★ 换了 artifact → 重取（P3 这条 effect 存在的本来理由：别渲上一份数据）", async () => {
    const { rerender } = await renderExpanded();
    const other = spec(98, 50);
    other.data_artifact_id = "art_y";
    mockEnsure.mockResolvedValue(full(98));
    rerender(<ChartTable spec={other} />);

    // Refetch receives **new** artifact — direct evidence old data is not rendered
    await waitFor(() => expect(mockEnsure).toHaveBeenCalledTimes(2));
    expect(mockEnsure.mock.calls[1]![0]).toMatchObject({ data_artifact_id: "art_y" });
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  it("★★ 负对照：内容变了但**重取失败** ⇒ 收起 + 报失败，且不许无限重试", async () => {
    const { rerender } = await renderExpanded();
    const half = spec(98, 50);
    half.rows![0]!.amt = 12345;
    // Cannot fetch full data (e.g. shared-report 403)
    mockEnsure.mockResolvedValue({ spec: half, truncated: true, ok: false });
    rerender(<ChartTable spec={half} />);

    await waitFor(() => expect(screen.getByText(/expand_failed/)).toBeInTheDocument());
    // Return collapsed: 10 rows and expand button restored (user can retry manually)
    expect(screen.getAllByRole("row")).toHaveLength(11);
    expect(screen.getByRole("button", { name: "Show all 98 rows" })).toBeInTheDocument();
    // ⚠ Critical: **no** automatic retry after failure — otherwise completion effect loops forever
    const callsAfterFailure = mockEnsure.mock.calls.length;
    await new Promise((r) => setTimeout(r, 50));
    expect(mockEnsure.mock.calls.length).toBe(callsAfterFailure);
  });
});


it("displays and sorts exact integer tokens without Number rounding", () => {
  const exact: ChartSpec = {
    kind: "table", title: "Exact amounts", row_count: 2,
    columns: [{ key: "name", header: "Name", type: "categorical" }, { key: "value", header: "Amount", type: "numeric" }],
    rows: [{ name: "Larger", value: "9007199254740993" }, { name: "Smaller", value: "9007199254740992" }],
  };
  const { rerender } = render(<ChartTable spec={exact} />);
  expect(screen.getByText("9,007,199,254,740,993")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("columnheader", { name: "Amount" }));
  fireEvent.click(screen.getByRole("columnheader", { name: /Amount/ }));
  const rows = screen.getAllByRole("row");
  expect(rows[1]).toHaveTextContent("Smaller");
  expect(rows[2]).toHaveTextContent("Larger");
  rerender(<ChartTable spec={exact} exactNumbers />);
  expect(screen.getByText("9007199254740993")).toBeInTheDocument();
});

 it("returns to a compact table and reuses loaded rows when expanded again", async () => {
  const data = spec(30, 10);
  mockEnsure.mockResolvedValue({ spec: spec(30, 30), ok: true, truncated: false });
  const view = render(<ChartTable spec={data} />);
  fireEvent.click(screen.getByRole("button", { name: "Show all 30 rows" }));
  await waitFor(() => expect(view.container.querySelectorAll("tbody tr")).toHaveLength(30));
  fireEvent.click(screen.getByRole("columnheader", { name: "营业额" }));
  expect(view.container.querySelector("tbody tr td")).toHaveTextContent("S29");
  const collapse = screen.getByRole("button", { name: "collapse" });
  collapse.focus();
  fireEvent.click(collapse);
  expect(view.container.querySelectorAll("tbody tr")).toHaveLength(10);
  expect(screen.getByRole("button", { name: "Show all 30 rows" })).toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "Show all 30 rows" }));
  await waitFor(() => expect(view.container.querySelectorAll("tbody tr")).toHaveLength(30));
  expect(mockEnsure).toHaveBeenCalledTimes(1);
  expect(view.container.querySelector("tbody tr td")).toHaveTextContent("S29");
 });
