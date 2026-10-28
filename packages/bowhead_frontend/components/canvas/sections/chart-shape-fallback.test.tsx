/**
 * Shape mismatch ≠ system failure (chart-config-ux ⑦).
 *
 * Corpus measurements on 116: 10 inspection chart choices × 1873 real charts; **26.9% of combinations** use fallback tables
 * (dual_axis/combo/scatter each 64–66%). The builder's internal messages are accurate
 * (`dual_axis requires x + ≥2 numeric`), but users receive the same
 * "图表配置生成失败，先保留原始数据" — describing a chart choice unsuitable for the data
 * as a broken system.
 *
 * These cases pin that boundary: **describe user-solvable problems in terms users can act on.**
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChartBlock } from "./chart-block";
import type { ChartSpec } from "@/lib/chart/types";

const { fetchFilePreview } = vi.hoisted(() => ({ fetchFilePreview: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchFilePreview,
}));

vi.mock("next-intl", () => ({
  useLocale: () => "zh-CN",
  // Return key + values verbatim to assert which message and parameters were used,
  // without asserting Chinese originals (messages owns those; copy edits should not fail this test).
  useTranslations: () => (key: string, values?: Record<string, unknown>) =>
    values ? `${key}(${JSON.stringify(values)})` : key,
}));

afterEach(() => cleanup());

function spec(over: Partial<ChartSpec>): ChartSpec {
  return {
    kind: "bar",
    title: "t",
    columns: [
      { key: "m", header: "月份", type: "categorical" },
      { key: "amt", header: "营业额", type: "numeric" },
    ],
    rows: [{ m: "1月", amt: 100 }, { m: "2月", amt: 90 }],
    row_count: 2,
    ...over,
  } as ChartSpec;
}

const noteText = () => document.querySelector(".v2-chart-fallback-note")?.textContent ?? "";

describe("形状不匹配的说法", () => {
  it("★★ 单数值列选 dual_axis：说「还差 1 个数值列」，不说「配置生成失败」", () => {
    render(<ChartBlock spec={spec({ kind: "dual_axis" })} />);
    const note = noteText();
    expect(note).toContain("fallback.shape_mismatch");
    expect(note).toContain("need_numeric");
    expect(note).toMatch(/need_numeric.*n.*1/);
    expect(note).not.toContain("config_error");
  });

  it("★ 纯数值表选 pie：说差的是分类/时间列", () => {
    const s = spec({
      kind: "pie",
      columns: [
        { key: "a", header: "A", type: "numeric" },
        { key: "b", header: "B", type: "numeric" },
      ],
      rows: [{ a: 1, b: 2 }],
    });
    const note = (render(<ChartBlock spec={s} />), noteText());
    expect(note).toContain("need_dims");
    expect(note).not.toContain("need_numeric");
  });

  it("★ 形状够了就不出这条——正对照，否则上面两条在任何情况下都绿", () => {
    // Chart.js cannot render in jsdom; a 1cat+1num bar still uses the fallback table,
    // but **the reason is render_error**. Assert the shape branch was not taken, rather than no fallback table.
    render(<ChartBlock spec={spec({ kind: "bar" })} />);
    const note = noteText();
    expect(note).toContain("fallback.render_error");
    expect(note).not.toContain("shape_mismatch");
  });

  it("★ 兜底表仍然把数据渲出来（说法变了，数据不能没）", () => {
    render(<ChartBlock spec={spec({ kind: "dual_axis" })} />);
    expect(screen.getByText("营业额")).toBeInTheDocument();
    expect(screen.getByText("1月")).toBeInTheDocument();
  });

  it("★ 图种名走 i18n，不把内部 kind 串直接甩给用户", () => {
    render(<ChartBlock spec={spec({ kind: "dual_axis" })} />);
    // Real messages include `kind.dual_axis`; this mock returns the key verbatim,
    // asserting a lookup occurred rather than concatenating a bare kind.
    expect(noteText()).toContain("kind.dual_axis");
  });
});

describe("兜底表读原始数据文件", () => {
  // 116 s_18c8b86f3003: fallback table uses chart title as filename without MIME ⇒ viewer cannot identify type,
  // although preview API returns rows, the UI shows "此文件暂无可渲染预览".
  it("★★ 预览回了行就按表格渲出来，不因标题没有 .csv 后缀而说「暂无可渲染预览」", async () => {
    fetchFilePreview.mockResolvedValue({
      family: "spreadsheet",
      mode: "rows",
      text: null,
      columns: ["营业额 / 元"],
      rows: [{ "营业额 / 元": "9104527316.48" }],
      total_rows: 1,
      shown_rows: 1,
    });
    render(<ChartBlock spec={spec({ kind: "hbar", title: "门店营业额排名 Top15", data_artifact_id: "art_total" })} />);
    await waitFor(() => expect(screen.getByText("9104527316.48")).toBeInTheDocument());
    expect(fetchFilePreview).toHaveBeenCalledWith("art_total", expect.anything());
    expect(document.body.textContent).not.toContain("no_preview");
  });
});
