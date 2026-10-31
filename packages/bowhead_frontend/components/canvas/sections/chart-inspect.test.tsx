/**
 * Batch 2 §3.1 · inspection layer. Pin **honesty** and accessibility, not pixels:
 *  - Fetch full data on entry (sorting/Top-N from partial data = false Top10);
 *  - Disclose failures to complete, and annotate aggregation scope;
 *  - Top-N other bucket uses null for nonadditive columns (view-transforms; verify actual wiring here);
 *  - Esc close / focus trap (components/ui/dialog DialogShell; verify actual wiring here).
 */

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChartInspect } from "./chart-inspect";
import type { ChartSpec } from "@/lib/chart/types";

vi.mock("next-intl", () => ({
  // Chart component localizes x-axis time buckets / granularity column names (2026-09-01) —
  // component really calls useLocale(); missing mock throws throughout file.
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string, values?: Record<string, unknown>) => {
    if (key === "rows_shown") return `rows ${values?.shown}/${values?.total}`;
    const labels: Record<string, string> = {
      open: "Inspect", close: "Close", tab_chart: "Chart", tab_data: "Data", view: "View",
      sort: "Sort", sort_none: "Default", top_n: "Top-N", top_n_all: "All",
      data_labels: "Labels", ref_line: "RefLine", partial_note: "PARTIAL",
      ref_line_mean: "Mean", load_failed: "LOAD_FAILED", ref_line_partial: "(subset)",
      download_png: "PNG", download_csv: "CSV", open_data: "Data",
      copy_image: "CopyImage", copy_done: "COPIED", followup: "AskAbout",
      copy_failed_unsupported: "COPY_UNSUPPORTED",
      copy_failed_write_failed: "COPY_BLOCKED",
      copy_unavailable_hint: "NEEDS_HTTPS",
      rest_bucket: "其余",
      ref_line_view_partial: "(view)",
      "kind.bar": "Bar", "kind.hbar": "Horizontal Bar", "kind.line": "Line",
      "kind.area_stacked": "Stacked Area", "kind.stacked_bar": "Stacked Bar",
      "kind.dual_axis": "Dual Axis", "kind.combo": "Bar + Line", "kind.pie": "Pie",
      "kind.scatter": "Scatter", "kind.waterfall": "Waterfall", "kind.table": "Table",
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

function spec(rowCount: number, inHand: number): ChartSpec {
  return {
    kind: "bar",
    title: "门店营业额",
    columns: [
      { key: "shop", header: "门店", type: "categorical" },
      { key: "amt", header: "营业额", type: "numeric" },
      { key: "rate", header: "退款率", type: "numeric", percent_scale: "fraction" },
    ],
    rows: Array.from({ length: inHand }, (_, i) => ({ shop: `S${i}`, amt: 100 - i, rate: 0.05 })),
    row_count: rowCount,
    data_artifact_id: "art_x",
  };
}

afterEach(() => cleanup());
beforeEach(() => mockEnsure.mockReset());

describe("ChartInspect", () => {
  it("discloses source and displayed units beside the data table", () => {
    const data: ChartSpec = { kind: "line", title: "Revenue", row_count: 1,
      columns: [{ key: "region", header: "Region", type: "categorical" }, { key: "amount", header: "Revenue", type: "numeric", unit: "万元" }],
      rows: [{ region: "West", amount: 120.5 }] };
    const view = render(<ChartInspect spec={data} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    expect(screen.getByRole("columnheader", { name: "Revenue (元)" })).toBeInTheDocument();
    expect(screen.getByText("1,205,000")).toBeInTheDocument();
    expect(view.baseElement.querySelector("[data-chart-unit-note]")).toBeInTheDocument();
  });
  it("retains distinct source units in the data tab", () => {
    const data: ChartSpec = { kind: "line", title: "Amounts", row_count: 1,
      columns: [{ key: "date", header: "Date", type: "date" },
        { key: "a", header: "Revenue (亿元)", type: "numeric", unit: "亿元" },
        { key: "b", header: "Revenue (万元)", type: "numeric", unit: "万元" },
        { key: "c", header: "Revenue (元)", type: "numeric", unit: "元" }],
      rows: [{ date: "2026-01-02", a: 0.0009, b: 9, c: 90000 }] };
    render(<ChartInspect spec={data} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    expect(screen.getByRole("columnheader", { name: "Revenue (亿元)" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Revenue (万元)" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Revenue (元)" })).toBeInTheDocument();
    expect(screen.getByText("0.0009")).toBeInTheDocument();
  });

  it("★进层先补全量（排序/Top-N 基于半截数据就是假 Top10）", async () => {
    mockEnsure.mockResolvedValue({ spec: spec(98, 98), truncated: false, ok: true });
    render(<ChartInspect spec={spec(98, 50)} onClose={vi.fn()} />);
    await waitFor(() => expect(mockEnsure).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/rows 98\/98/)).toBeInTheDocument());
  });

  it("数据已全 → 不发请求", () => {
    render(<ChartInspect spec={spec(5, 5)} onClose={vi.fn()} />);
    expect(mockEnsure).not.toHaveBeenCalled();
  });

  it("★手里的行已经比补拉能给的还多 → 不发请求，也不许说「加载失败」", async () => {
    // Charts now contain up to 10,000 rows, while `/preview` gives at most `FULL_ROWS_LIMIT` (1000):
    // fetching only yields fewer rows; old code treated fewer returned rows as ok:false, falsely reporting load failure.
    render(<ChartInspect spec={spec(12000, 10000)} onClose={vi.fn()} />);
    await new Promise((r) => setTimeout(r, 0));
    expect(mockEnsure).not.toHaveBeenCalled();
    expect(screen.queryByText(/LOAD_FAILED/)).toBeNull();
    expect(screen.getByText(/rows 10000\/12000/)).toBeInTheDocument();
    // jsdom cannot draw canvas, so chart falls back to 10,000-row table (~40,000 nodes); dialog shell searches panel for
    // focusable elements on every DOM change, taking ~5s alone and >20s fully concurrent in jsdom; allow longer timeout.
  }, 60_000);

  it("视图下拉显示本地化名称，不暴露 hbar 等内部 token", () => {
    render(<ChartInspect spec={spec(5, 5)} onClose={vi.fn()} />);
    const view = screen.getByRole("combobox", { name: "View" });
    const labels = within(view).getAllByRole("option").map((option) => option.textContent);
    expect(labels).toContain("Horizontal Bar");
    expect(labels).toContain("Stacked Area");
    expect(labels).not.toContain("hbar");
    expect(labels).not.toContain("area_stacked");
  });

  it("★补不到时要说出来，并标聚合口径（不假装是全量）", async () => {
    mockEnsure.mockResolvedValue({ spec: spec(98, 50), truncated: true, ok: false });
    render(<ChartInspect spec={spec(98, 50)} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/LOAD_FAILED/)).toBeInTheDocument());
    expect(screen.getByText(/PARTIAL/)).toBeInTheDocument();
  });

  it("★Top-N 的「其余」对比率列置 null（数据页签里显 —，不是 0）", async () => {
    render(<ChartInspect spec={spec(8, 8)} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    fireEvent.change(screen.getByLabelText("Top-N"), { target: { value: "5" } });
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(7); // Header + Top5 + other
    const rest = rows[rows.length - 1]!;
    const cells = within(rest).getAllByRole("cell");
    // Additive columns sum to values; ratio column is dash (neither 0 nor average)
    expect(cells[1]).not.toHaveTextContent("—");
    expect(cells[2]).toHaveTextContent("—");
  });

  it("★★补回来的是**文件原始序**，Top-N 仍须取最大的 N 个（客户实拍的「细看」路径）", async () => {
    // Combined effects: report chart is Top-N sorted descending by backend `_take_top_n`;
    // on inspection entry `ensureFullRows` uses `/preview` (sequential CSV reads, no sorting) to replace baseSpec with
    // **original file order**; applyView Top-N formerly sliced directly, so Top-5 selected
    // first 5 CSV rows. **Deliberately put large stores at file end** here, exposing slicing immediately.
    const fileOrder: ChartSpec = {
      kind: "bar",
      title: "门店营业额",
      columns: [
        { key: "shop", header: "门店", type: "categorical" },
        { key: "amt", header: "营业额", type: "numeric" },
        { key: "rate", header: "退款率", type: "numeric", percent_scale: "fraction" },
      ],
      rows: [
        { shop: "S1", amt: 1, rate: 0.01 },
        { shop: "S2", amt: 2, rate: 0.01 },
        { shop: "S3", amt: 3, rate: 0.01 },
        { shop: "S4", amt: 4, rate: 0.01 },
        { shop: "S5", amt: 5, rate: 0.01 },
        { shop: "旗舰A", amt: 900, rate: 0.01 },
        { shop: "旗舰B", amt: 800, rate: 0.01 },
        { shop: "旗舰C", amt: 700, rate: 0.01 },
      ],
      row_count: 8,
      data_artifact_id: "art_x",
    };
    mockEnsure.mockResolvedValue({ spec: fileOrder, truncated: false, ok: true });
    render(<ChartInspect spec={spec(8, 3)} onClose={vi.fn()} />);
    await waitFor(() => expect(mockEnsure).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    fireEvent.change(screen.getByLabelText("Top-N"), { target: { value: "5" } });
    const names = screen
      .getAllByRole("row")
      .slice(1) // Remove header
      .map((r) => within(r).getAllByRole("cell")[0]!.textContent);
    // Negative control: slicing yields S1…S5 + "其余" (all three flagship stores folded into "其余").
    expect(names).toEqual(["旗舰A", "旗舰B", "旗舰C", "S5", "S4", "其余"]);
  });

  it("Esc 关闭（DialogShell 真接上了）", () => {
    const onClose = vi.fn();
    render(<ChartInspect spec={spec(3, 3)} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // Esc case above cannot prove focus enters/traps Tab; following two actually exercise panel.
  it("焦点真的进层：初始焦点落在关闭按钮", async () => {
    render(<ChartInspect spec={spec(3, 3)} onClose={vi.fn()} />);
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close" }));
    });
  });

  it("Tab 在层内循环", async () => {
    render(<ChartInspect spec={spec(3, 3)} onClose={vi.fn()} />);
    const panel = await screen.findByRole("dialog");
    const focusable = Array.from(
      panel.querySelectorAll<HTMLElement>('button, select, input, [tabindex]:not([tabindex="-1"])'),
    ).filter((el) => el.getAttribute("aria-hidden") !== "true");
    const first = focusable[0]!;
    const last = focusable[focusable.length - 1]!;
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it("★R4-6 Top-N 的「其余」类目走译文，不是哨兵 __rest__", () => {
    render(<ChartInspect spec={spec(8, 8)} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    fireEvent.change(screen.getByLabelText("Top-N"), { target: { value: "5" } });
    expect(screen.queryByText("__rest__")).toBeNull();
    expect(screen.getByRole("cell", { name: "其余" })).toBeInTheDocument();
  });

  it("★R4-4/pie 没有笛卡尔值轴 → 不给参考线开关（勾了没反应是本 sprint 要消灭的）", () => {
    render(<ChartInspect spec={{ ...spec(5, 5), kind: "pie" }} onClose={vi.fn()} />);
    expect(screen.queryByLabelText("RefLine")).toBeNull();
    expect(screen.getByLabelText("Labels")).toBeInTheDocument();
  });

  it("点背板关闭，点面板内不关", () => {
    const onClose = vi.fn();
    const { container } = render(<ChartInspect spec={spec(3, 3)} onClose={onClose} />);
    const panel = screen.getByRole("dialog");
    fireEvent.click(panel);
    expect(onClose).not.toHaveBeenCalled();
    const backdrop = container.ownerDocument.querySelector(".rc-inspect-backdrop")!;
    // Pointer down inside panel (selecting cells), release on backdrop: do not close.
    fireEvent.pointerDown(panel);
    fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.pointerDown(backdrop);
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("系列开关：点掉一个系列后该列不再出现在数据页签", () => {
    render(<ChartInspect spec={spec(3, 3)} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "退款率" }));
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    expect(screen.queryByRole("columnheader", { name: "退款率" })).toBeNull();
    expect(screen.getByRole("columnheader", { name: "营业额" })).toBeInTheDocument();
  });

  it("★裸 HTTP（非安全上下文）：不出「复制为图片」，降级成下载并说明原因", () => {
    // 116 production uses this environment — silently inert buttons are what this sprint eliminates
    render(<ChartInspect spec={spec(3, 3)} onClose={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "CopyImage" })).toBeNull();
    const fallback = screen.getByRole("button", { name: "copy_degraded_download" });
    expect(fallback).toHaveAttribute("title", "NEEDS_HTTPS");
    // ★R8 (real device on 116): fallback button label **must not** duplicate actual "下载 PNG" beside it.
    // Original design called fallback "下载 PNG", making adjacent buttons identical except hover-only
    // title. This assertion blocks that (user approved design change on 2026-08-14).
    // DialogShell portal's invisible focus guards are not visible buttons and excluded from comparison.
    const labels = screen
      .getAllByRole("button")
      .filter((b) => !b.hasAttribute("data-floating-ui-focus-guard"))
      .map((b) => b.textContent?.trim());
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("安全上下文下才出「复制为图片」", () => {
    class FakeClipboardItem {}
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("ClipboardItem", FakeClipboardItem);
    vi.stubGlobal("navigator", { ...navigator, language: navigator.language, languages: navigator.languages, clipboard: { write: vi.fn() } });
    try {
      render(<ChartInspect spec={spec(3, 3)} onClose={vi.fn()} />);
      expect(screen.getByRole("button", { name: "CopyImage" })).toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("★「追问这张图」发事件并关层（改问题回对话，不在检视层里改）", () => {
    const onClose = vi.fn();
    const seen: unknown[] = [];
    const listener = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener("bowhead:chart-followup", listener);
    try {
      render(<ChartInspect spec={spec(3, 3)} captionText="女装贡献超三分之一" onClose={onClose} />);
      fireEvent.click(screen.getByRole("button", { name: "AskAbout" }));
    } finally {
      window.removeEventListener("bowhead:chart-followup", listener);
    }
    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatchObject({
      title: "门店营业额",
      dataArtifactId: "art_x",
      captionText: "女装贡献超三分之一",
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("数据页签只出 CSV 与追问（PNG/复制是图表专属）", () => {
    render(<ChartInspect spec={spec(3, 3)} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    expect(screen.queryByRole("button", { name: "PNG" })).toBeNull();
    expect(screen.getByRole("button", { name: "CSV" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "AskAbout" })).toBeInTheDocument();
  });
});

describe("系列被后端截过时的检视层", () => {
  it("★ 仍然去补全量 —— 层是用户主动点开的出口，「要看全部分组就来这里」是发版说明的承诺", async () => {
    // Main-chart path (`needsRowCompletion`) **does** block it: passive rendering must not restore 59 ribbons.
    // Distinct criteria are intentional: who requested it differs. Before changing, update both comments and release notes.
    mockEnsure.mockResolvedValue({ ok: true, spec: spec(708, 708), truncated: false });
    render(<ChartInspect spec={{ ...spec(708, 180), series_shown: 15, series_total: 59 }} onClose={vi.fn()} />);
    await waitFor(() => expect(mockEnsure).toHaveBeenCalledTimes(1));
  });
});

// ─────────────────────────────────────────────────────────────
// Former ★R8 P2 series-switch automatic-compatible-kind downgrade block retired together with implementation.
//
// That downgrade chain (canBuildKind → preferredKind → kind_degraded notice) merely patched hiding series by
// deleting numeric columns from spec: removed columns made dual_axis/combo violate their
// shape requirements. Consolidation instead sets dataset.hidden without deleting any columns, eliminating trigger;
// Switching off a line changing chart kind goes from **test-guarded** to **structurally impossible**.
//
// Replacements (stricter criteria: state and renderer spec, rather than presence of downgrade notice):
//   components/canvas/sections/chart-series-visibility.test.tsx
//   lib/chart/series-visibility.test.ts
// ─────────────────────────────────────────────────────────────
