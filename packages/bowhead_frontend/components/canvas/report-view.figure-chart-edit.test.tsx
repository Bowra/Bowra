/**
 * A chart a narrative shows through a figure anchor is edited like a standalone chart section in report editing:
 * chart settings and the type button change the chart, and the change saves to the chart section. The editor
 * document holds only the anchor, so the edit must not depend on the document diff.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { patchCanvasSection } from "@/lib/canvas-pm-adapter/canvas-patch";
import type { ReportSection } from "@/lib/types";

vi.mock("react-chartjs-2", async () => {
  const { forwardRef } = await import("react");
  return { Chart: forwardRef(function ChartStub() { return <div data-testid="chart-stub" />; }) };
});

vi.mock("@/lib/canvas-pm-adapter/canvas-patch", () => ({
  patchCanvasSection: vi.fn(async () => ({ ok: true, rev: 2 })),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchCanvasStatus: vi.fn(async () => ({ status: "active", effectiveStatus: "active", quarantinedCount: 0 })),
    setCanvasStatus: vi.fn(),
  };
});

import { ReportView } from "./report-view";

vi.stubGlobal("matchMedia", (query: string) => ({
  matches: false,
  media: query,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
}));

const CHART = {
  id: "sec_chart",
  kind: "chart",
  rev: 1,
  spec: {
    kind: "bar",
    title: "各区间质数个数",
    row_count: 3,
    columns: [
      { key: "bin", header: "区间", type: "categorical" },
      { key: "count", header: "质数个数", type: "numeric" },
    ],
    rows: [{ bin: "1-200", count: 46 }, { bin: "201-400", count: 32 }, { bin: "401-600", count: 31 }],
    explicit_presentation: { version: 1, mode: "plot", axes: { x: "bin", y: ["count"], x_scale: "category" }, group_by: [] },
  },
};
const SECTIONS = [
  { id: "sec_n1", kind: "narrative", markdown: "分布见下图。\n\n[figure:sec_chart]\n\n图1：各区间质数个数。", rev: 1 },
  CHART,
] as unknown as ReportSection[];

const inlineFigure = () => waitFor(() => {
  const figure = document.querySelector<HTMLElement>("[data-figure-ref]");
  expect(figure).not.toBeNull();
  return figure!;
});

afterEach(() => {
  cleanup();
  vi.mocked(patchCanvasSection).mockClear();
});

describe("inline report chart editing", () => {
  it("changes the type in chart settings and saves it to the chart section", async () => {
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" canEdit isEditMode />);
    const figure = await inlineFigure();
    await within(figure).findByRole("button", { name: "柱状" });

    // The type button's menu is closed by the editor's focus handling under jsdom, so drive the same edit
    // through chart settings; both call the frame's single edit handler.
    fireEvent.click(await within(figure).findByTitle(/图表设置/));
    const dialog = await screen.findByRole("dialog", { name: "图表设置" });
    const kinds = within(dialog).getByLabelText("类型");
    expect([...kinds.querySelectorAll("option")].map((option) => option.textContent)).toEqual(["折线", "柱状", "横向柱状", "堆叠面积"]);
    fireEvent.change(kinds, { target: { value: "area_stacked" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    // The figure shows the edit before the save returns.
    expect(await within(figure).findByRole("button", { name: "堆叠面积" })).toBeInTheDocument();
    await waitFor(() => expect(patchCanvasSection).toHaveBeenCalledWith(
      "cv_1",
      "sec_chart",
      { spec: expect.objectContaining({ kind: "area_stacked", explicit_presentation: CHART.spec.explicit_presentation }) },
      1,
    ), { timeout: 3000 });
    // Only the chart section is written; the narrative holding the anchor is unchanged.
    expect(vi.mocked(patchCanvasSection).mock.calls.every(([, sectionId]) => sectionId === "sec_chart")).toBe(true);
  });

  it("offers no chart settings and saves nothing while reading", async () => {
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" canEdit isEditMode={false} />);
    const figure = await inlineFigure();
    await within(figure).findByRole("button", { name: "柱状" });
    expect(within(figure).queryByTitle(/图表设置/)).not.toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 900));
    expect(patchCanvasSection).not.toHaveBeenCalled();
  });
});
