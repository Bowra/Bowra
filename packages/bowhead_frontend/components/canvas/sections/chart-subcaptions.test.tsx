/**
 * Truncation notices (chart-config-ux batch 3 ⑤).
 *
 * Backend uses top-N rather than full-table fallback above row limit. Table footer already says "显示 1-30 / 共 98",
 * but Chart.js path had **no equivalent** — silently omitting 68 rows is false reporting.
 * Pin disclosures: required when truncated, forbidden otherwise, and required in all four rendering branches.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChartBlock } from "./chart-block";
import { resetChartRowCompletionCache } from "@/lib/chart/complete-chart-rows";
import type { ChartSpec } from "@/lib/chart/types";

const { fetchFilePreview } = vi.hoisted(() => ({ fetchFilePreview: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchFilePreview,
}));
// jsdom lacks 2d context; real Chart.js throws and ErrorBoundary substitutes fallback table (including subcaption).
// Replace with stub exposing Chart.js x-axis and series in DOM — verify exactly which rows reach chart.
const { chartDataSeen } = vi.hoisted(() => ({ chartDataSeen: [] as unknown[] }));
vi.mock("react-chartjs-2", () => ({
  Chart: ({ data }: { data: { labels?: unknown[]; datasets?: Array<{ label?: string; data?: unknown[] }> } }) => {
    chartDataSeen.push(data);
    return (
      <div
        data-testid="chartjs"
        data-labels={JSON.stringify(data.labels ?? [])}
        data-series={JSON.stringify((data.datasets ?? []).map((d) => d.label))}
        data-values={JSON.stringify((data.datasets ?? []).map((d) => d.data))}
      />
    );
  },
}));

function drawnLabels(): string[] {
  return JSON.parse(screen.getByTestId("chartjs").getAttribute("data-labels") ?? "[]");
}
function drawnSeries(): string[] {
  return JSON.parse(screen.getByTestId("chartjs").getAttribute("data-series") ?? "[]");
}

vi.mock("next-intl", () => ({
  // Chart component localizes x-axis time buckets / granularity column names (2026-09-01) —
  // component really calls useLocale(); missing mock would throw throughout file.
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string, values?: Record<string, unknown>) => {
    if (key === "chart.truncated_top_n") return `Showing top ${values?.shown} of ${values?.total}`;
    if (key === "chart.truncated_top_by") return `Top ${values?.shown} of ${values?.total} by |${values?.measure}|`;
    if (key === "chart.truncated_series") return `Top ${values?.shown} of ${values?.total} series`;
    if (key === "chart.downsampled_points") return `${values?.total} pts down to ${values?.shown}`;
    if (key === "chart.auto_from_sandbox") return "Auto-generated from sandbox data";
    return key;
  },
}));

afterEach(() => {
  cleanup();
  fetchFilePreview.mockReset();
  resetChartRowCompletionCache();
  chartDataSeen.length = 0;
});

function spec(over: Partial<ChartSpec> = {}): ChartSpec {
  return {
    kind: "table",
    title: "门店排名",
    row_count: 98,
    top_n: 30,
    columns: [
      { key: "门店", header: "门店", type: "categorical" },
      { key: "金额", header: "金额", type: "numeric" },
    ],
    rows: Array.from({ length: 30 }, (_, i) => ({ 门店: `S${i}`, 金额: 100 - i })),
    ...over,
  };
}

describe("chart truncation subcaption", () => {
  it("说出截断了多少 —— 两个数都要真", () => {
    render(<ChartBlock spec={spec()} />);
    expect(screen.getByTestId("chart-truncated-subcaption")).toHaveTextContent(
      "Showing top 30 of 98",
    );
  });

  it("没截断就不出（`top_n` 缺省 = 全量）", () => {
    render(<ChartBlock spec={spec({ top_n: undefined, row_count: 30 })} />);
    expect(screen.queryByTestId("chart-truncated-subcaption")).toBeNull();
  });

  it("★ 表格被 inline 上限切过不出这句——表格页脚自己会说「显示 1-50 / 共 60」", () => {
    render(
      <ChartBlock
        spec={spec({ top_n: undefined, row_count: 60, rows: Array.from({ length: 50 }, (_, i) => ({ 门店: `S${i}`, 金额: i })) })}
      />,
    );
    expect(screen.queryByTestId("chart-truncated-subcaption")).toBeNull();
  });

  it("与「自动生成」提示共存，互不吞掉", () => {
    render(<ChartBlock spec={spec({ auto_charted: true })} />);
    expect(screen.getByTestId("chart-truncated-subcaption")).toBeInTheDocument();
    expect(screen.getByTestId("auto-chart-subcaption")).toBeInTheDocument();
  });

  it("★ KPI 分支也说 —— 四条渲染分支共用同一个组件，不能只有一条带提示", () => {
    render(<ChartBlock spec={spec({ kind: "kpi", rows: [{ 门店: "S0", 金额: 1 }] })} />);
    expect(screen.getByTestId("chart-truncated-subcaption")).toBeInTheDocument();
  });

  it("★ 不支持的图种走兜底表那条也说", () => {
    render(<ChartBlock spec={spec({ kind: "geo" })} />);
    expect(screen.getByTestId("chart-truncated-subcaption")).toBeInTheDocument();
  });

  it("★★ 检视层（chromeless）里一律不出 —— 层有自己更准的口径", () => {
    // Caught on real device on 116: inspection `ensureFullRows` fetched all 40 rows and footer says "显示 40 / 共 40 行",
    // while this still says "仅显示前 30 项" — `top_n` marks **original spec**, but inspection renders completed rows.
    // Even worse if user chooses Top-5 inside inspection.
    render(<ChartBlock spec={spec()} chromeless />);
    expect(screen.queryByTestId("chart-truncated-subcaption")).toBeNull();
  });

  it("chromeless 也不出「自动生成」提示（同一条让位理由）", () => {
    render(<ChartBlock spec={spec({ auto_charted: true })} chromeless />);
    expect(screen.queryByTestId("auto-chart-subcaption")).toBeNull();
  });
});


/** Synthetic 12-month, 6-brand series has 72 rows but inlines only the first 50, without top_n. */
const MONTHS = [
  "2025年9月", "2025年10月", "2025年11月", "2025年12月", "2026年1月", "2026年2月",
  "2026年3月", "2026年4月", "2026年5月", "2026年6月", "2026年7月", "2026年8月",
];
const BRANDS = ["Brand A", "Brand B", "Brand C", "Brand D", "Brand E", "Brand F"];
const ALL_ROWS = MONTHS.flatMap((m, i) => BRANDS.map((b, j) => ({ 每月: m, 中文品牌: b, 销售金额: 1e7 + i * 10 + j })));

function legacyLine(over: Partial<ChartSpec> = {}): ChartSpec {
  return {
    kind: "line",
    title: "Sample monthly brand sales",
    row_count: 72,
    data_artifact_id: "art_monthly_example",
    columns: [
      { key: "每月", header: "每月", type: "date" },
      { key: "中文品牌", header: "中文品牌", type: "categorical" },
      { key: "销售金额", header: "销售金额", type: "numeric" },
    ],
    rows: ALL_ROWS.slice(0, 50),
    ...over,
  };
}

function previewOf(rows: Array<Record<string, unknown>>, columns = ["每月", "中文品牌", "销售金额"]) {
  return {
    family: "table",
    mode: "rows",
    text: null,
    columns,
    // File API numbers are strings (CSV parsing) — `ensureFullRows` normalizes; provide actual shape here.
    rows: rows.map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, typeof v === "number" ? String(v) : v]))),
    total_rows: rows.length,
    truncated: false,
  };
}
function drawnValues(): Array<Array<number | null>> {
  return JSON.parse(screen.getByTestId("chartjs").getAttribute("data-values") ?? "[]");
}

describe("存量图补全行：图上画的必须是全部行，否则就得说", () => {
  it("★★ 补不到全量时照实说「只画了 50 / 共 72」——不能再一声不吭", () => {
    render(<ChartBlock spec={legacyLine({ data_artifact_id: undefined })} />);
    // The first 50 synthetic rows cover nine months, with the final point in May.
    expect(drawnLabels()).toHaveLength(9);
    expect(screen.getByTestId("chart-truncated-subcaption")).toHaveTextContent("Showing top 50 of 72");
  });

  it("★★ 去文件补回 72 行：图画满 12 个月 × 6 个品牌，提示消失", async () => {
    fetchFilePreview.mockResolvedValue(previewOf(ALL_ROWS));
    render(<ChartBlock spec={legacyLine()} />);
    expect(fetchFilePreview).toHaveBeenCalledWith("art_monthly_example", expect.objectContaining({ mode: "rows" }));
    await waitFor(() => expect(drawnLabels()).toHaveLength(12));
    expect(drawnLabels().at(-1)).toContain("2026");
    expect(drawnSeries()).toHaveLength(6);
    expect(screen.queryByTestId("chart-truncated-subcaption")).toBeNull();
  });

  it("补失败（分享报告 403 / 文件被清理）就保持原样并说出来", async () => {
    fetchFilePreview.mockRejectedValue(new Error("403"));
    render(<ChartBlock spec={legacyLine()} />);
    await waitFor(() => expect(fetchFilePreview).toHaveBeenCalled());
    expect(screen.getByTestId("chart-truncated-subcaption")).toHaveTextContent("Showing top 50 of 72");
  });

  it("★ 后端有意截过（打了 `top_n`）就不补——补了反而画出几百根柱子", () => {
    const rows = Array.from({ length: 30 }, (_, i) => ({ 门店: `S${i}`, 金额: 100 - i }));
    render(<ChartBlock spec={spec({ kind: "bar", data_artifact_id: "art_rank", rows })} />);
    expect(fetchFilePreview).not.toHaveBeenCalled();
    expect(screen.getByTestId("chart-truncated-subcaption")).toHaveTextContent("Showing top 30 of 98");
  });

  it("导出面不许现取数据：不补，但照实说", () => {
    render(<ChartBlock spec={legacyLine()} surface="export" />);
    expect(fetchFilePreview).not.toHaveBeenCalled();
    expect(screen.getByTestId("chart-truncated-subcaption")).toHaveTextContent("Showing top 50 of 72");
  });

  it("检视层（chromeless）不补——层自己补，层里少行可能是用户选的 Top-N", () => {
    render(<ChartBlock spec={legacyLine()} chromeless />);
    expect(fetchFilePreview).not.toHaveBeenCalled();
  });

  it("表格不补——它有「展开全部」", () => {
    render(<ChartBlock spec={legacyLine({ kind: "table" })} />);
    expect(fetchFilePreview).not.toHaveBeenCalled();
  });

  it("★★ `add_chart` 带了 `limit`：文件比这张图多 ⇒ 不认，不能把 60 天的图画成 365 天", async () => {
    // Review reproduction: old spec row_count=60 (limit applied), rows=50; file holds full-year 365 rows.
    const days = Array.from({ length: 365 }, (_, i) => ({ 日期: `D${String(i).padStart(3, "0")}`, 金额: 1000 + i }));
    const limited: ChartSpec = {
      kind: "line",
      title: "前60天金额趋势",
      row_count: 60,
      data_artifact_id: "art_limit",
      columns: [
        { key: "日期", header: "日期", type: "date" },
        { key: "金额", header: "金额", type: "numeric" },
      ],
      rows: days.slice(0, 50),
    };
    fetchFilePreview.mockResolvedValue(previewOf(days, ["日期", "金额"]));
    render(<ChartBlock spec={limited} />);
    await waitFor(() => expect(fetchFilePreview).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(drawnLabels()).toHaveLength(50);
    expect(screen.getByTestId("chart-truncated-subcaption")).toHaveTextContent("Showing top 50 of 60");
  });

  it("★★ `sort_by` 重排过的图：文件是原序 ⇒ 前 N 行对不上，不认", async () => {
    const reordered = legacyLine({ rows: [...ALL_ROWS].reverse().slice(0, 50) });
    fetchFilePreview.mockResolvedValue(previewOf(ALL_ROWS));
    render(<ChartBlock spec={reordered} />);
    await waitFor(() => expect(fetchFilePreview).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByTestId("chart-truncated-subcaption")).toHaveTextContent("Showing top 50 of 72");
  });

  it("★ 百分号文本列（`15.37%`）补回来仍是数，不会整列变空", async () => {
    // Backend converted entire `15.37%` column to 15.37 during creation; fetched rows must parse identically.
    const rates = MONTHS.flatMap((m, i) => BRANDS.map((b, j) => ({ 每月: m, 中文品牌: b, 退款率: 10 + i + j / 10 })));
    const spec = legacyLine({
      data_artifact_id: "art_pct",
      columns: [
        { key: "每月", header: "每月", type: "date" },
        { key: "中文品牌", header: "中文品牌", type: "categorical" },
        { key: "退款率", header: "退款率", type: "numeric", unit: "%" },
      ],
      rows: rates.slice(0, 50),
    });
    fetchFilePreview.mockResolvedValue(
      previewOf(rates.map((r) => ({ ...r, 退款率: `${r.退款率.toFixed(1)}%` })), ["每月", "中文品牌", "退款率"]),
    );
    render(<ChartBlock spec={spec} />);
    await waitFor(() => expect(drawnLabels()).toHaveLength(12));
    const values = drawnValues().flat();
    expect(values.filter((v) => typeof v === "number" && v > 0)).toHaveLength(72);
  });

  it("★ 补完之后同一个 spec 重渲不换对象——下游 memo 与检视层的 effect 都按引用判", async () => {
    fetchFilePreview.mockResolvedValue(previewOf(ALL_ROWS));
    const s = legacyLine();
    const { rerender } = render(<ChartBlock spec={s} />);
    await waitFor(() => expect(drawnLabels()).toHaveLength(12));
    rerender(<ChartBlock spec={s} />);
    rerender(<ChartBlock spec={s} />);
    const last = chartDataSeen.slice(-2);
    expect(last[0]).toBe(last[1]);
  });

  it("★ 同一个文件画成另一列：请求身份不同，要重拉，不能沿用上一张的行", async () => {
    const qty = MONTHS.flatMap((m, i) => BRANDS.map((b, j) => ({ 每月: m, 中文品牌: b, 销售数量: 100 + i * 10 + j })));
    fetchFilePreview.mockImplementation(async () => previewOf(
      ALL_ROWS.map((r, k) => ({ ...r, 销售数量: qty[k]!.销售数量 })),
      ["每月", "中文品牌", "销售金额", "销售数量"],
    ));
    const amount = legacyLine();
    const count = legacyLine({
      columns: [
        { key: "每月", header: "每月", type: "date" },
        { key: "中文品牌", header: "中文品牌", type: "categorical" },
        { key: "销售数量", header: "销售数量", type: "numeric" },
      ],
      rows: qty.slice(0, 50),
    });
    const { rerender } = render(<ChartBlock spec={amount} />);
    await waitFor(() => expect(drawnLabels()).toHaveLength(12));
    rerender(<ChartBlock spec={count} />);
    await waitFor(() => expect(fetchFilePreview).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(drawnValues().flat().filter((v) => typeof v === "number" && v >= 100 && v < 300)).toHaveLength(72));
  });

  it("同一张图挂两处（对话 + 报告）只拉一次", async () => {
    fetchFilePreview.mockResolvedValue(previewOf(ALL_ROWS));
    render(
      <>
        <ChartBlock spec={legacyLine()} />
        <ChartBlock spec={legacyLine()} />
      </>,
    );
    await waitFor(() => expect(screen.getAllByTestId("chartjs").every((el) => JSON.parse(el.getAttribute("data-labels") ?? "[]").length === 12)).toBe(true));
    expect(fetchFilePreview).toHaveBeenCalledTimes(1);
  });

  it("重挂载时已拉到的全量首帧就用——不先画半截图再闪成全量", async () => {
    fetchFilePreview.mockResolvedValue(previewOf(ALL_ROWS));
    const first = render(<ChartBlock spec={legacyLine()} />);
    await waitFor(() => expect(drawnLabels()).toHaveLength(12));
    first.unmount();
    render(<ChartBlock spec={legacyLine()} />);
    // No waiting: first frame should contain 12 months and no truncation notice
    expect(drawnLabels()).toHaveLength(12);
    expect(screen.queryByTestId("chart-truncated-subcaption")).toBeNull();
    expect(fetchFilePreview).toHaveBeenCalledTimes(1);
  });

  it("已拉到的全量即使被挤出请求缓存，重挂载也不再拉第二次", async () => {
    // `cache` (requests) and `resolved` (results) evict independently at limits; request may be gone while result remains —
    // use received result directly instead of refetching merely because request cache lacks it (supplementary review reproduction).
    fetchFilePreview.mockResolvedValueOnce(previewOf(ALL_ROWS));
    const first = render(<ChartBlock spec={legacyLine()} />);
    await waitFor(() => expect(drawnLabels()).toHaveLength(12));
    first.unmount();
    // 64 never-resolving charts evict it from request cache
    fetchFilePreview.mockImplementation(() => new Promise(() => {}));
    const others = render(
      <>
        {Array.from({ length: 64 }, (_, i) => (
          <ChartBlock key={i} spec={legacyLine({ data_artifact_id: `art_pending_${i}` })} />
        ))}
      </>,
    );
    await new Promise((r) => setTimeout(r, 0));
    others.unmount();
    const before = fetchFilePreview.mock.calls.length;
    render(<ChartBlock spec={legacyLine()} />);
    expect(drawnLabels()).toHaveLength(12);
    await new Promise((r) => setTimeout(r, 0));
    expect(fetchFilePreview.mock.calls.length - before).toBe(0);
  });

  it("自己那次拉失败了、同一张图在别处后来拉成了——重渲时用成功的那份", async () => {
    fetchFilePreview.mockRejectedValueOnce(new Error("503")).mockResolvedValue(previewOf(ALL_ROWS));
    const a = render(<ChartBlock spec={legacyLine()} />);
    await waitFor(() => expect(fetchFilePreview).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 0));
    expect(drawnLabels()).toHaveLength(9);
    // Same chart mounts elsewhere and successfully fetches
    const b = render(<ChartBlock spec={legacyLine()} />, { container: document.body.appendChild(document.createElement("div")) });
    await waitFor(() => expect(fetchFilePreview).toHaveBeenCalledTimes(2));
    b.unmount();
    // A rerenders with new spec object: must not keep drawing its own failed nine-month result
    a.rerender(<ChartBlock spec={legacyLine()} />);
    await waitFor(() => expect(drawnLabels()).toHaveLength(12));
  });

  it("存量表格被用户切成柱状：不补（柱状要后端先取 top-N），照实说只画了 50 行", () => {
    const rows = Array.from({ length: 50 }, (_, i) => ({ 门店: `S${i}`, 金额: 200 - i }));
    render(<ChartBlock spec={spec({ kind: "table", top_n: undefined, row_count: 120, data_artifact_id: "art_tbl", rows })} />);
    fireEvent.click(screen.getByTitle("switcher.auto"));
    fireEvent.click(screen.getByText("kind.bar"));
    expect(fetchFilePreview).not.toHaveBeenCalled();
    expect(screen.getByTestId("chart-truncated-subcaption")).toHaveTextContent("Showing top 50 of 120");
  });

  it("新图（后端已带全行）什么都不做", () => {
    render(<ChartBlock spec={legacyLine({ rows: ALL_ROWS })} />);
    expect(fetchFilePreview).not.toHaveBeenCalled();
    expect(screen.queryByTestId("chart-truncated-subcaption")).toBeNull();
  });

  it("★★ 系列被后端截过的图**不去补行** —— 否则 59 条线原样全回来，后端那道闸白做", () => {
    // Top 15 of 59 provinces: rows 708→180, no `top_n`, artifact present ⇒ old criterion fetched.
    // Fetch restores all 59 provinces, reverting chart to ribbons with legend filling canvas.
    render(
      <ChartBlock
        spec={legacyLine({
          row_count: 708,
          rows: ALL_ROWS.slice(0, 12),
          series_shown: 15,
          series_total: 59,
        })}
      />,
    );
    expect(fetchFilePreview).not.toHaveBeenCalled();
  });
});

describe("chart series-cap subcaption", () => {
  function capped(over: Partial<ChartSpec> = {}): ChartSpec {
    return legacyLine({ row_count: 708, rows: ALL_ROWS.slice(0, 12), series_shown: 15, series_total: 59, ...over });
  }

  it("按**系列**的量词说，不说成行数", () => {
    render(<ChartBlock spec={capped()} />);
    expect(screen.getByTestId("chart-series-subcaption")).toHaveTextContent("Top 15 of 59 series");
  });

  it("★ 不再叠加一句按行数的「仅显示前 N 项」 —— 少掉的行就是被截掉的系列，同一件事说两遍且量词是错的", () => {
    render(<ChartBlock spec={capped()} />);
    expect(screen.queryByTestId("chart-truncated-subcaption")).toBeNull();
  });

  it("没截系列就不出（存量图四个字段全空 ⇒ 逐字走老路径）", () => {
    render(<ChartBlock spec={legacyLine({ rows: ALL_ROWS })} />);
    expect(screen.queryByTestId("chart-series-subcaption")).toBeNull();
  });

  it("★ 只画了 15 条里的 15 条时不出 —— `series_total == series_shown` 不是截断", () => {
    render(<ChartBlock spec={capped({ series_shown: 15, series_total: 15, row_count: 12 })} />);
    expect(screen.queryByTestId("chart-series-subcaption")).toBeNull();
  });

  it("★★ 检视层（chromeless）里一律不出 —— 沿用 `top_n` 那条让位规则", () => {
    render(<ChartBlock spec={capped()} chromeless />);
    expect(screen.queryByTestId("chart-series-subcaption")).toBeNull();
  });

  it("★★ 降采样按**时间点**的量词说，且不再叠加「仅显示前 N 项」", () => {
    // Downsampling removes sampled instants, preserving entire time span. Row-count phrasing implies omitted tail,
    // Sparse points across the full period do not imply that recent months are absent.
    render(<ChartBlock spec={legacyLine({ row_count: 4500, rows: ALL_ROWS.slice(0, 12), points_shown: 805, points_total: 900 })} />);
    expect(screen.getByTestId("chart-downsampled-subcaption")).toHaveTextContent("900 pts down to 805");
    expect(screen.queryByTestId("chart-truncated-subcaption")).toBeNull();
  });

  it("★ 既截了系列又降了采样 ⇒ 两句都出，各说各的", () => {
    render(
      <ChartBlock
        spec={legacyLine({ row_count: 4500, rows: ALL_ROWS.slice(0, 12), series_shown: 15, series_total: 59, points_shown: 805, points_total: 900 })}
      />,
    );
    expect(screen.getByTestId("chart-downsampled-subcaption")).toBeInTheDocument();
    expect(screen.getByTestId("chart-series-subcaption")).toBeInTheDocument();
  });

  it("没降采样就不出（存量图这两个字段为空 ⇒ 逐字走老路径）", () => {
    render(<ChartBlock spec={legacyLine({ rows: ALL_ROWS })} />);
    expect(screen.queryByTestId("chart-downsampled-subcaption")).toBeNull();
  });

  it("★ `points_total == points_shown` 不是降采样 —— 与系列那侧同一条边界", () => {
    render(<ChartBlock spec={legacyLine({ row_count: 12, rows: ALL_ROWS.slice(0, 12), points_shown: 12, points_total: 12 })} />);
    expect(screen.queryByTestId("chart-downsampled-subcaption")).toBeNull();
  });

  it("★★ 降采样过的图也不去补行 —— 补回来就把稀疏化整个撤销了", () => {
    render(<ChartBlock spec={legacyLine({ row_count: 4500, rows: ALL_ROWS.slice(0, 12), points_shown: 805, points_total: 900 })} />);
    expect(fetchFilePreview).not.toHaveBeenCalled();
  });

  it("★★ 行也被另一刀截过（`top_n` 有值）时两句都要出 —— 让位只在行损失全来自系列截断时成立", () => {
    // Re-review measured: when `limit` truncates before series gate truncates again, row disclosure disappeared,
    // hiding 708→400 loss entirely from UI, leaving only series disclosure.
    render(<ChartBlock spec={capped({ top_n: 400 })} />);
    expect(screen.getByTestId("chart-series-subcaption")).toBeInTheDocument();
    expect(screen.getByTestId("chart-truncated-subcaption")).toBeInTheDocument();
  });
});

describe("Sankey rows that are not a flow (readability audit item 1)", () => {
  const funnel = (): ChartSpec => ({
    kind: "sankey",
    title: "Member Funnel",
    row_count: 3,
    columns: [
      { key: "stage", header: "stage", type: "categorical" },
      { key: "value", header: "value", type: "numeric" },
      { key: "label", header: "label", type: "categorical" },
    ],
    rows: [
      { stage: "Traffic", value: 1000000, label: "100%" },
      { stage: "Member Traffic", value: 20000, label: "2%" },
      { stage: "Member Revenue (¥)", value: 300000000.25, label: "50% of total" },
    ],
  });

  it("shows the exact table with the reason, never width-compared bands", () => {
    render(<ChartBlock spec={funnel()} sectionId="sec_funnel" />);
    expect(screen.getByTestId("chart-not-a-flow")).toBeInTheDocument();
    expect(screen.queryByTestId("chartjs")).toBeNull();
    expect(screen.getByRole("region", { name: "Member Funnel" })).toHaveTextContent("300,000,000");
  });

  it("is a plain table, not the fallback frame the export page treats as a render failure", () => {
    const { container } = render(<ChartBlock spec={funnel()} sectionId="sec_funnel" surface="export" />);
    expect(container.querySelector(".v2-chart-fallback-wrap")).toBeNull();
    expect(container.querySelector("[data-export-effective-kind='table']")).not.toBeNull();
  });

  it("negative control: a conserving flow still reaches Chart.js", () => {
    render(
      <ChartBlock
        spec={{
          kind: "sankey",
          title: "Checkout",
          row_count: 3,
          columns: [
            { key: "from", header: "from", type: "categorical" },
            { key: "to", header: "to", type: "categorical" },
            { key: "v", header: "v", type: "numeric" },
          ],
          rows: [
            { from: "Visit", to: "Cart", v: 1000 },
            { from: "Visit", to: "Leave", v: 9000 },
            { from: "Cart", to: "Order", v: 600 },
          ],
        }}
      />,
    );
    expect(screen.queryByTestId("chart-not-a-flow")).toBeNull();
    expect(screen.getByTestId("chartjs")).toBeInTheDocument();
  });
});

describe("long horizontal bars in the export frame", () => {
  const hbar = (): ChartSpec => ({
    kind: "hbar",
    title: "各品类营业额",
    row_count: 111,
    columns: [
      { key: "c", header: "品类", type: "categorical" },
      { key: "v", header: "营业额", type: "numeric" },
    ],
    // Ascending: the first 32 rows are the 32 **smallest** — the export must not keep them.
    rows: Array.from({ length: 50 }, (_, i) => ({ c: `C${i}`, v: i + 1 })),
  });

  it("draws the rows with the largest values that fit and says how they were chosen", () => {
    render(<ChartBlock spec={hbar()} surface="export" />);
    expect(drawnLabels()).toHaveLength(32);
    expect(drawnLabels()[0]).toBe("C18");
    expect(screen.getByTestId("chart-truncated-subcaption")).toHaveTextContent("Top 32 of 111 by |营业额|");
  });

  it("screens keep every row (they grow and scroll)", () => {
    render(<ChartBlock spec={hbar()} surface="canvas" />);
    expect(drawnLabels()).toHaveLength(50);
  });
});
