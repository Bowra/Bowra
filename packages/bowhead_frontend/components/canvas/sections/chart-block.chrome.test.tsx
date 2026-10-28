/**
 * Chart frame by placement (Chart Embed Spec D4) and the shared exact-value disclosure (D5).
 *
 * Real ChartBlock / TableActions; only the react-chartjs-2 canvas is stubbed (jsdom has no 2d context).
 */

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChartSpec } from "@/lib/chart/types";

vi.mock("next-intl", () => ({
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string) => {
    const labels: Record<string, string> = {
      more_actions: "More actions",
      exact_values: "View exact values",
      open: "Inspect",
      "kind.bar": "Bar",
      "kind.hbar": "Horizontal Bar",
      "kind.pie": "Pie",
      "kind.table": "Table",
      "kind.waterfall": "Waterfall",
    };
    return labels[key] ?? key;
  },
}));

const rendered = vi.hoisted(() => ({ datasets: [] as Array<{ data: unknown[]; datalabels?: { display?: (context: { dataIndex: number }) => boolean | string } }> }));

vi.mock("react-chartjs-2", async () => {
  const { forwardRef } = await import("react");
  return { Chart: forwardRef(function ChartStub({ data }: { data: { datasets: typeof rendered.datasets } }) { rendered.datasets = data.datasets; return <div data-testid="chart-stub" />; }) };
});

import { ChartBlock } from "./chart-block";

const spec: ChartSpec = {
  kind: "hbar",
  title: "领域覆盖",
  row_count: 3,
  columns: [
    { key: "domain", header: "领域", type: "categorical" },
    { key: "entries", header: "管理条目", type: "numeric" },
  ],
  rows: [
    { domain: "财务", entries: 12 },
    { domain: "人员管理", entries: 7 },
    { domain: "运营", entries: 4 },
  ],
};

afterEach(() => cleanup());

describe("ChartBlock chrome", () => {
  it("composed views keep existing kind and action controls for each view", () => {
    const composed: ChartSpec = {
      ...spec,
      title: "Coverage comparison",
      views: [
        { id: "rank", kind: "chart", spec },
        { id: "details", kind: "chart", spec: { ...spec, kind: "table", title: "Exact values" } },
      ],
      layout: { type: "stack", children: [{ view: "rank" }, { view: "details" }] },
    };
    const { container } = render(<ChartBlock spec={composed} />);
    expect(container.querySelectorAll("[data-chart-view]")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "More actions" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Horizontal Bar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Table" })).toBeInTheDocument();
  });
  it("report (default) keeps its own title row, kind switch and ⋯, without an exact-value disclosure", () => {
    const { container } = render(<ChartBlock spec={spec} />);
    const figure = container.querySelector("figure")!;
    expect(figure).toHaveAttribute("data-chart-chrome", "report");
    expect(figure.querySelector(".v2-chart-header")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Horizontal Bar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "More actions" })).toBeInTheDocument();
    expect(screen.queryByText("View exact values")).toBeNull();
  });

  it("embed draws no title row, switch or ⋯; the host title names the figure; exact values are offered", () => {
    const { container } = render(
      <>
        <h2 id="host-title">领域覆盖</h2>
        <ChartBlock spec={spec} chrome="embed" labelledBy="host-title" />
      </>,
    );
    const figure = container.querySelector("figure")!;
    expect(figure).toHaveAttribute("data-chart-chrome", "embed");
    expect(figure).toHaveAttribute("data-chart-placement", "embed");
    expect(figure.querySelector(".v2-chart-header")).toBeNull();
    expect(screen.getByRole("figure", { name: "领域覆盖" })).toBe(figure);
    expect(figure).not.toHaveAttribute("aria-label");
    expect(screen.queryByRole("button", { name: "Horizontal Bar" })).toBeNull();
    expect(screen.queryByRole("button", { name: "More actions" })).toBeNull();
    // Rows sized by count (D3): the plot opts out of the fixed box minimum.
    expect(container.querySelector(".v2-chart-canvas-wrap")).toHaveClass("is-tall", "is-fit");
    expect(screen.getByText("View exact values").closest("summary")).not.toBeNull();
  });

  it("embed hosts opt in to the switch and actions, rendered into their slot", () => {
    const slot = document.createElement("div");
    document.body.appendChild(slot);
    const { container } = render(<ChartBlock spec={spec} chrome="embed" kindSwitch actions headerSlot={slot} />);
    expect(container.querySelector(".v2-chart-header")).toBeNull();
    expect(slot.querySelector("button[aria-label='More actions']")).not.toBeNull();
    expect(slot.textContent).toContain("Horizontal Bar");
    slot.remove();
  });

  it("the exact-value table mounts only when opened, borderless, with full category names", () => {
    const { container } = render(
      <ChartBlock spec={spec} chrome="embed" exactValueLabels={{ 人员管理: "人员管理与组织" }} />,
    );
    expect(container.querySelector(".v2-chart-exact .v2-chart-table")).toBeNull();
    const details = container.querySelector("details.v2-chart-exact") as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    const table = container.querySelector(".v2-chart-exact .v2-chart-table")!;
    expect(table).toHaveAttribute("data-chart-chrome", "none");
    expect(table.textContent).toContain("人员管理与组织");
    expect(table.textContent).toContain("12");
    // The table has no header row of its own (no title, no actions).
    expect(table.querySelector(".v2-chart-header")).toBeNull();
  });

  it("full names are keyed by the category value, not the row position", () => {
    // Rows drawn in a different order than the host built its names: a positional mapping would rename 运营 to 财务.
    const reordered: ChartSpec = { ...spec, rows: [...(spec.rows ?? [])].reverse() };
    const { container } = render(
      <ChartBlock spec={reordered} chrome="embed" exactValueLabels={{ 人员管理: "人员管理与组织", 财务: "财务与会计" }} />,
    );
    const details = container.querySelector("details.v2-chart-exact") as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    const cells = Array.from(container.querySelectorAll(".v2-chart-exact tbody tr")).map((tr) => tr.textContent);
    expect(cells).toEqual(["运营4", "人员管理与组织7", "财务与会计12"]);
  });

  it("embed table form: no own header or frame, no extra exact-value table, host-enabled switch stays usable", () => {
    const slot = document.createElement("div");
    document.body.appendChild(slot);
    const { container } = render(
      <ChartBlock spec={{ ...spec, kind: "table" }} chrome="embed" kindSwitch headerSlot={slot} />,
    );
    expect(container.querySelector(".v2-chart-header")).toBeNull();
    expect(container.querySelector(".v2-chart-table")).toHaveAttribute("data-chart-chrome", "none");
    expect(screen.queryByText("View exact values")).toBeNull();
    expect(slot.querySelector("button")?.textContent).toContain("Table");
    slot.remove();
  });

  it("embed KPI form: no own title row or card frame", () => {
    const kpi: ChartSpec = {
      kind: "kpi", title: "利润", row_count: 1,
      columns: [{ key: "p", header: "利润", type: "numeric" }],
      rows: [{ p: 12 }],
    };
    const { container } = render(<ChartBlock spec={kpi} chrome="embed" />);
    expect(container.querySelector(".v2-chart-kpi")).toHaveClass("is-embedded");
    expect(container.querySelector(".v2-chart-kpi-title-row")).toBeNull();
    expect(screen.queryByText("View exact values")).toBeNull();
  });

  it("embed fallback (shape the kind cannot draw): one table, no frame-in-frame, no duplicate exact values", () => {
    const { container } = render(<ChartBlock spec={{ ...spec, kind: "dual_axis" }} chrome="embed" />);
    expect(container.querySelector(".v2-chart-fallback-wrap")).not.toBeNull();
    expect(container.querySelectorAll(".v2-chart-table")).toHaveLength(1);
    expect(container.querySelector(".v2-chart-header")).toBeNull();
    expect(screen.queryByText("View exact values")).toBeNull();
  });

  it("none (= chromeless) is plot only: no title row, no disclosure", () => {
    const { container } = render(<ChartBlock spec={spec} chromeless />);
    expect(container.querySelector("figure")).toHaveAttribute("data-chart-chrome", "none");
    expect(container.querySelector(".v2-chart-header")).toBeNull();
    expect(screen.queryByText("View exact values")).toBeNull();
  });

  it("the kind switch uses a line chevron, not the ▾ character", () => {
    render(<ChartBlock spec={spec} />);
    const button = screen.getByRole("button", { name: "Horizontal Bar" });
    expect(button.textContent).not.toContain("▾");
    expect(button.querySelector("svg")).not.toBeNull();
  });
});


it("keeps multi-series touch reading focused on series identity, while retaining authored numeric labels", () => {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("max-width") || query.includes("coarse"), media: query, addEventListener() {}, removeEventListener() {} }));
  const line: ChartSpec = { kind: "line", title: "Revenue and cost", row_count: 4,
    columns: [{ key: "month", header: "Month", type: "date" }, { key: "revenue", header: "Revenue", type: "numeric" }, { key: "cost", header: "Cost", type: "numeric" }],
    rows: Array.from({ length: 4 }, (_, i) => ({ month: `2026-0${i + 1}`, revenue: 50 + i, cost: 30 + i })),
  };
  try {
    const result = render(<ChartBlock spec={line} />);
    expect(rendered.datasets).toHaveLength(2);
    expect(rendered.datasets.map((dataset) => dataset.data.length)).toEqual([4, 4]);
    expect(rendered.datasets.map((dataset) => dataset.datalabels?.display?.({ dataIndex: 0 }))).toEqual([false, false]);
    result.rerender(<ChartBlock spec={{ ...line, show_data_labels: true }} />);
    expect(rendered.datasets.every((dataset) => Boolean(dataset.datalabels?.display?.({ dataIndex: 0 })))).toBe(true);
  } finally { vi.unstubAllGlobals(); }
});
