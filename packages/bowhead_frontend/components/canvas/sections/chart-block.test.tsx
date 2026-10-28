import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChartBlock, canRenderCanvasCharts } from "./chart-block";
import { ChartMenuRootContext } from "./chart-menu-root";
import type { ChartSpec } from "@/lib/chart/types";
import { linkedFocusFixture } from "../../../e2e/_chart-linked-focus-fixture";
import { timeWindowFixture } from "../../../e2e/_chart-time-window-fixture";
import {
  CANVAS_FOCUS_DATA_EVENT,
  type CanvasFocusDataDetail,
} from "@/lib/canvas-workspace-focus";

vi.mock("next-intl", () => ({
  // Chart component localizes x-axis time buckets / granularity column names (2026-09-01) —
  // component really calls useLocale(); missing mock throws throughout file.
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string, values?: Record<string, unknown>) => {
    if (key === "showing") return `Showing ${values?.start}-${values?.end} of ${values?.total}`;
    if (key === "expand_all") return `Show all ${values?.total} rows`;
    if (key === "expand_truncated") return `Loaded first ${values?.limit} rows`;
    const labels: Record<string, string> = {
      download_csv: "Download CSV",
      download_png: "Download PNG",
      open_data: "Open Data",
      more_actions: "More actions",
      expanding: "Loading",
      sort_needs_expand: "Expand to sort",
      "kind.bar": "Bar",
      "kind.hbar": "Horizontal Bar",
      "kind.pie": "Pie",
      "kind.table": "Table",
      "kind.kpi": "KPI",
    };
    return labels[key] ?? key;
  },
}));


/** After quiet-by-default behavior, action buttons live in `⋯`; open menu before clicking them.
 *  (Chart-kind switcher stays outside, persistently visible; see `table-actions.tsx` comment.) */
function openMoreActions() {
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
}

const __dirname = dirname(fileURLToPath(import.meta.url));

afterEach(() => cleanup());

describe("ChartBlock canvas fallback", () => {
  it("canonical export ignores an active browser time range and preserves every saved row", () => {
    const root = timeWindowFixture().spec;
    root.views = root.views!.map((view) => ({ ...view, spec: { ...view.spec, kind: "table" } }));
    const { container, rerender } = render(<ChartBlock spec={root} sectionId="sec_root" rootRevision={1} />);
    expect(container.querySelector("[data-chart-time-window]")!.compareDocumentPosition(container.querySelector("[data-chart-group]")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.change(screen.getByLabelText("window_start"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("window_end"), { target: { value: "2" } });
    expect(container.querySelectorAll('[data-chart-view="detail"] tbody tr')).toHaveLength(2);
    expect(container.querySelectorAll('[data-chart-view="comparison"] tbody tr')).toHaveLength(6);
    rerender(<ChartBlock spec={root} sectionId="sec_root" rootRevision={1} surface="export" />);
    expect(container.querySelector('[data-chart-time-window]')).toBeNull();
    expect(container.querySelector('[data-chart-window-scope]')).toBeNull();
    for (const id of ["detail", "relationship", "comparison"]) expect(container.querySelectorAll(`[data-chart-view="${id}"] tbody tr`)).toHaveLength(6);
    expect(root.rows).toHaveLength(6);
  });
  it("requires persisted figure identity and retains sorted typed focus until a revision changes", () => {
    const root = linkedFocusFixture().spec;
    root.views = root.views!.map((view) => ({ ...view, spec: { ...view.spec, kind: "table" } }));
    const { container, rerender } = render(<ChartBlock spec={root} sectionId="sec_root" />);
    const east = () => Array.from(container.querySelectorAll('[data-chart-view="detail"] tbody tr')).find((row) => row.textContent?.includes("East"))!;
    fireEvent.click(east());
    expect(container.querySelector("[data-chart-focus-status]")).toBeNull();
    rerender(<ChartBlock spec={root} sectionId="sec_root" rootRevision={1} />);
    fireEvent.click(container.querySelector('[data-chart-view="detail"] th:nth-child(2)')!);
    fireEvent.click(east());
    expect(east()).toHaveAttribute("aria-selected", "true");
    expect(container.querySelector('[data-chart-view="detail"] th:nth-child(2)')).toHaveAttribute("aria-sort", "descending");
    expect(container.querySelector("[data-chart-focus-status]")).not.toBeNull();
    rerender(<ChartBlock spec={root} sectionId="sec_root" rootRevision={2} />);
    expect(container.querySelector("[data-chart-focus-status]")).toBeNull();
    expect(east()).toHaveAttribute("aria-selected", "false");
  });

  const resultSpec: ChartSpec = {
    kind: "table",
    title: "Channel GMV",
    row_count: 1,
    data_artifact_id: "art_channel_gmv",
    columns: [
      { key: "channel", header: "Channel", type: "categorical" },
      { key: "gmv", header: "GMV", type: "numeric" },
    ],
    rows: [{ channel: "Tmall", gmv: 962.7 }],
  };

  it("routes a child kind change through its stable view identity", () => {
    const onViewKindChange = vi.fn();
    const root: ChartSpec = { ...resultSpec, views: [
      { id: "primary", kind: "chart", spec: resultSpec },
      { id: "details", kind: "chart", spec: { ...resultSpec, title: "Details" } },
    ], layout: { type: "stack", children: [{ view: "primary" }, { view: "details" }] } };
    const { container } = render(<ChartBlock spec={root} sectionId="sec_root" onViewKindChange={onViewKindChange} />);
    fireEvent.click(container.querySelector('[data-chart-view="details"] button[aria-haspopup="menu"]')!);
    fireEvent.click(document.querySelector('[role="menuitemradio"][data-kind="kpi"]')!);
    expect(onViewKindChange).toHaveBeenCalledWith("details", "kpi");
    expect(root.views![0]!.spec.kind).toBe("table");
  });

  it("keeps Report section context on the canonical data action", () => {
    const listener = vi.fn();
    window.addEventListener(CANVAS_FOCUS_DATA_EVENT, listener);
    render(<ChartBlock spec={resultSpec} sectionId="sec_chart" />);

    openMoreActions();
    fireEvent.click(screen.getByRole("menuitem", { name: "Open Data" }));

    const event = listener.mock.calls[0]?.[0] as CustomEvent<CanvasFocusDataDetail>;
    expect(event.detail).toEqual({
      artifactId: "art_channel_gmv",
      entryPoint: "report",
      reportContext: { label: "Channel GMV", sectionId: "sec_chart" },
    });
    window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, listener);
  });

  it("marks the same renderer as Chat when embedded in an answer", () => {
    const listener = vi.fn();
    window.addEventListener(CANVAS_FOCUS_DATA_EVENT, listener);
    render(<ChartBlock spec={resultSpec} surface="chat" />);

    openMoreActions();
    fireEvent.click(screen.getByRole("menuitem", { name: "Open Data" }));

    const event = listener.mock.calls[0]?.[0] as CustomEvent<CanvasFocusDataDetail>;
    expect(event.detail).toEqual({
      artifactId: "art_channel_gmv",
      entryPoint: "chat",
      reportContext: undefined,
    });
    window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, listener);
  });

  it("detects canvas support through HTMLCanvasElement.getContext", () => {
    expect(canRenderCanvasCharts()).toBe(typeof HTMLCanvasElement.prototype.getContext === "function");
  });

  it("uses readable table values for incompatible axes while preserving prepared data and controls", () => {
    const spec: ChartSpec = {
      kind: "dual_axis", title: "Revenue, rate and visits", row_count: 2,
      columns: [
        { key: "month", header: "Month", type: "date" },
        { key: "revenue", header: "Revenue", type: "numeric", unit: "USD", y_axis_id: 0 },
        { key: "rate", header: "Rate", type: "numeric", format: "percent", percent_scale: "fraction", y_axis_id: 1 },
        { key: "visits", header: "Visits", type: "numeric", unit: "people", y_axis_id: 1 },
      ],
      rows: [{ month: "2026-01", revenue: 100.12345, rate: 0.198735, visits: 50 }, { month: "2026-02", revenue: 120, rate: null, visits: 55 }],
    };
    const original = structuredClone(spec);
    const { container } = render(<ChartBlock spec={spec} sectionId="sec_units" />);
    expect(screen.getByTestId("chart-incompatible-units")).toHaveTextContent("incompatible_axis_units");
    expect(screen.getByRole("cell", { name: "100.12" })).toBeInTheDocument();
    // Longer unit text is named once in the header instead of being repeated in each cell.
    expect(screen.getByRole("columnheader", { name: "Visits (people)" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "50" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "19.87%" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "More actions" })).toBeInTheDocument();
    expect(container.querySelector('[data-export-effective-kind="table"]')).toBeInTheDocument();
    expect(container.querySelector("canvas")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "kind.dual_axis" }));
    expect(screen.queryByRole("menuitemradio", { name: "Bar" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitemradio", { name: "Table" })).toBeInTheDocument();
    expect(spec).toEqual(original);
  });

  it("renders table fallback instead of a blank canvas when getContext is unavailable", () => {
    const original = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, "getContext");
    Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
      configurable: true,
      value: undefined,
    });
    try {
      const spec: ChartSpec = {
        kind: "bar",
        title: "Channel GMV",
        row_count: 2,
        columns: [
          { key: "channel", header: "Channel", type: "categorical" },
          { key: "gmv", header: "GMV", type: "numeric" },
        ],
        rows: [
          { channel: "Tmall", gmv: 962.7 },
          { channel: "JD", gmv: 420.1 },
        ],
      };
      const { container } = render(<ChartBlock spec={spec} />);

      expect(screen.getByRole("region", { name: "Channel GMV" })).toBeInTheDocument();
      expect(screen.getByRole("cell", { name: "Tmall" })).toBeInTheDocument();
      expect(container.querySelector(".v2-chart-canvas-wrap canvas")).toBeNull();
    } finally {
      if (original) {
        Object.defineProperty(HTMLCanvasElement.prototype, "getContext", original);
      } else {
        delete (HTMLCanvasElement.prototype as { getContext?: unknown }).getContext;
      }
    }
  });

  it("routes chart kind changes to the persistent editor callback when controlled", () => {
    const spec: ChartSpec = {
      kind: "table",
      title: "Channel GMV",
      row_count: 1,
      columns: [
        { key: "channel", header: "Channel", type: "categorical" },
        { key: "gmv", header: "GMV", type: "numeric" },
      ],
      rows: [{ channel: "Tmall", gmv: 962.7 }],
    };
    const onKindChange = vi.fn();
    render(<ChartBlock spec={spec} sectionId="sec_chart" onKindChange={onKindChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Horizontal Bar" }));

    expect(onKindChange).toHaveBeenCalledWith("hbar");
    expect(screen.getByRole("button", { name: "Table" })).toBeInTheDocument();
  });

  it("registers a restorable transient override outside edit mode", async () => {
    const spec: ChartSpec = {
      kind: "table",
      title: "Channel GMV",
      row_count: 1,
      columns: [
        { key: "channel", header: "Channel", type: "categorical" },
        { key: "gmv", header: "GMV", type: "numeric" },
      ],
      rows: [{ channel: "Tmall", gmv: 962.7 }],
    };
    const restoreRef: { current: (() => void) | null } = { current: null };
    const registerPresentationOverride = vi.fn((_sectionId: string, callback: () => void) => {
      restoreRef.current = callback;
      return () => { restoreRef.current = null; };
    });
    const { CanvasContextProvider } = await import("@/lib/canvas-pm-adapter/canvas-context");
    render(
      <CanvasContextProvider
        sections={[]}
        registerPresentationOverride={registerPresentationOverride}
      >
        <ChartBlock spec={spec} sectionId="sec_chart" />
      </CanvasContextProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "KPI" }));
    await waitFor(() => expect(registerPresentationOverride).toHaveBeenCalledWith(
      "sec_chart",
      expect.any(Function),
    ));
    expect(screen.getByRole("button", { name: "KPI" })).toBeInTheDocument();

    restoreRef.current?.();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Table" })).toBeInTheDocument();
    });
  });
});


// ─── Chart-kind dropdown: useMenu selection menu (shared components "图层" section, item 5) ───────────
describe("图种切换下拉", () => {
  const spec: ChartSpec = {
    kind: "table",
    title: "Channel GMV",
    row_count: 1,
    columns: [
      { key: "channel", header: "Channel", type: "categorical" },
      { key: "gmv", header: "GMV", type: "numeric" },
    ],
    rows: [{ channel: "Tmall", gmv: 962.7 }],
  };

  it("菜单挂在 body 上；当前图种是选中的 menuitemradio，打开时焦点落在它上面", async () => {
    const { container } = render(<ChartBlock spec={spec} sectionId="sec_chart" />);
    fireEvent.click(screen.getByRole("button", { name: "Table" }));

    const menu = await screen.findByRole("menu", { name: "Table" });
    expect(container.contains(menu)).toBe(false);
    const current = screen.getByRole("menuitemradio", { name: "Table" });
    expect(current).toHaveAttribute("aria-checked", "true");
    const others = screen.getAllByRole("menuitemradio").filter((item) => item !== current);
    expect(others.length).toBeGreaterThan(0);
    for (const item of others) expect(item).toHaveAttribute("aria-checked", "false");
    await waitFor(() => expect(document.activeElement).toBe(current));
  });

  it("图表菜单容器由上层指定时挂进去（设置页预览区带着自己的配色变量）", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    try {
      render(
        <ChartMenuRootContext.Provider value={host}>
          <ChartBlock spec={spec} sectionId="sec_chart" />
        </ChartMenuRootContext.Provider>,
      );
      fireEvent.click(screen.getByRole("button", { name: "Table" }));
      const menu = await screen.findByRole("menu", { name: "Table" });
      expect(host.contains(menu)).toBe(true);
    } finally {
      cleanup();
      host.remove();
    }
  });

  it("菜单开着时动作条带 is-open，关掉后去掉；Esc 回焦到触发按钮", async () => {
    const { container } = render(<ChartBlock spec={spec} sectionId="sec_chart" />);
    const actions = container.querySelector(".v2-chart-actions");
    const trigger = screen.getByRole("button", { name: "Table" });
    expect(actions).not.toHaveClass("is-open");

    fireEvent.click(trigger, { detail: 0 });
    const menu = await screen.findByRole("menu", { name: "Table" });
    await waitFor(() => expect(actions).toHaveClass("is-open"));

    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(actions).not.toHaveClass("is-open");
  });
});


// ─── Batch 3 4.3 (chart-table-ux P1-6): switcher must not omit current chart kind ───────────
// Cause: _compatibleKinds added waterfall/sankey/heatmap candidates, then final
// supported allowlist filtered them out → switching waterfall to table leaves switcher
// without waterfall, **preventing switching back forever**.
describe("批3 4.3 KindSwitcher 候选完整性", () => {
  function openSwitcherKinds(container: HTMLElement): string[] {
    const trigger = container.querySelector("button[aria-haspopup='menu']");
    if (!trigger) return [];
    fireEvent.click(trigger);
    // Menu is mounted on body (MenuFloating portal), not in container.
    return [...document.querySelectorAll("[role='menuitemradio'][data-kind]")].map(
      (el) => el.getAttribute("data-kind") ?? "",
    );
  }

  const waterfallSpec: ChartSpec = {
    kind: "waterfall",
    title: "毛利桥",
    row_count: 3,
    columns: [
      { key: "item", header: "分项", type: "categorical" },
      { key: "delta", header: "增量", type: "numeric" },
    ],
    rows: [
      { item: "价格", delta: 6.1 },
      { item: "销量", delta: -3.4 },
      { item: "成本", delta: -1.8 },
    ],
  };

  it("waterfall 图的切换器里含 waterfall 自己（切走能切回来）", () => {
    const { container } = render(<ChartBlock spec={waterfallSpec} />);
    expect(openSwitcherKinds(container)).toContain("waterfall");
  });

  it("★当前 kind 即便不在启发式候选里也恒在列（负对照：数据形状不匹配的历史 spec）", () => {
    // 2 numeric + 0 category columns → heuristic yields only scatter/table, but persisted chart is heatmap
    const orphan: ChartSpec = {
      kind: "heatmap",
      title: "历史 spec",
      row_count: 2,
      columns: [
        { key: "a", header: "A", type: "numeric" },
        { key: "b", header: "B", type: "numeric" },
      ],
      rows: [{ a: 1, b: 2 }, { a: 3, b: 4 }],
    };
    const { container } = render(<ChartBlock spec={orphan} />);
    expect(openSwitcherKinds(container)).toContain("heatmap");
  });
});


// ─── review R1: hook order + matchMedia branches (formerly uncovered) ──────────────
describe("批3 review 修复守卫", () => {
  it("★per-chart plugin 随主题、图注与参考线文案重建", () => {
    const src = readFileSync(resolve(__dirname, "chart-block.tsx"), "utf8");
    const start = src.indexOf("const chartKey = [");
    const end = src.indexOf("].join(\":\");", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const keySource = src.slice(start, end);
    for (const dependency of [
      "theme.ink",
      "theme.inkSoft",
      "theme.surface",
      "captionText",
      "r.label",
      "r.partial",
      "r.partialNote",
    ]) {
      expect(keySource, `chartKey 漏了 ${dependency}，自定义插件会继续使用旧闭包`)
        .toContain(dependency);
    }
  });

  it("★所有 hook 必须在两个早退之前（config null→非null 时 hook 顺序不能变）", () => {
    // Console probing React "order of Hooks" errors is **intermittent** (errors occur outside
    // act and are unreliable concurrently — measured flakiness). Use deterministic source check instead: extract
    // ChartJsBlock function body, assert final hook call precedes first early return.
    // This gate caught two actual regressions: batch 3 summary useMemo and batch 2 reference-line useMemo.
    const src = readFileSync(
      resolve(__dirname, "chart-block.tsx"),
      "utf8",
    );
    const bodyStart = src.indexOf("function ChartJsBlock(");
    expect(bodyStart).toBeGreaterThan(-1);
    const body = src.slice(bodyStart);
    const firstEarlyReturn = body.indexOf("if (!canvasSupported)");
    expect(firstEarlyReturn).toBeGreaterThan(-1);

    const hookCalls = [...body.matchAll(/\buse(?:State|Effect|Memo|Callback|Ref)\s*\(/g)];
    const afterReturn = hookCalls.filter((m) => m.index! > firstEarlyReturn);
    const context = afterReturn.map((m) => body.slice(Math.max(0, m.index! - 60), m.index! + 20).trim());
    expect(
      afterReturn,
      `早退之后还有 hook 调用（React 会抛 order of Hooks，ErrorBoundary 吞掉后图永久卡兜底表）：\n${context.join("\n---\n")}`,
    ).toHaveLength(0);
  });

  it("★matchMedia 缺失时不得抛（jsdom 本来就没有它——正是这条守卫要防的环境）", () => {
    expect(typeof window.matchMedia).not.toBe("function");
    const errors: unknown[] = [];
    const spy = vi.spyOn(console, "error").mockImplementation((...args) => { errors.push(args[0]); });
    try {
      const spec: ChartSpec = {
        kind: "bar", title: "无 matchMedia", row_count: 2,
        columns: [
          { key: "c", header: "类目", type: "categorical" },
          { key: "v", header: "值", type: "numeric" },
        ],
        rows: [{ c: "A", v: 1 }, { c: "B", v: 2 }],
      };
      // jsdom Chart.js lacks 2d context and naturally falls back to table — unrelated to matchMedia,
      // so assert only **absence of matchMedia errors**, not fallback table.
      expect(() => render(<ChartBlock spec={spec} />)).not.toThrow();
    } finally {
      spy.mockRestore();
    }
    expect(errors.map(String).filter((e) => e.includes("matchMedia"))).toEqual([]);
  });

  it("matchMedia 存在且匹配移动端时走 stub（Safari 旧 API 也不抛）", () => {
    const listeners: Array<() => void> = [];
    const queries: string[] = [];
    const legacyMq = {
      matches: true,
      addListener: (cb: () => void) => listeners.push(cb),
      removeListener: () => {},
    };
    vi.stubGlobal("matchMedia", (query: string) => {
      queries.push(query);
      return legacyMq as unknown as MediaQueryList;
    });
    try {
      const spec: ChartSpec = {
        kind: "bar", title: "移动端", row_count: 2,
        columns: [
          { key: "c", header: "类目", type: "categorical" },
          { key: "v", header: "值", type: "numeric" },
        ],
        rows: [{ c: "A", v: 1 }, { c: "B", v: 2 }],
      };
      expect(() => render(<ChartBlock spec={spec} />)).not.toThrow();
      // The shared menus also subscribe to their phone bottom-sheet query; only the chart's own queries matter here.
      const MENU_SHEET_QUERY = "(max-width: 639.98px)";
      expect(queries.filter((query) => query !== MENU_SHEET_QUERY)).toEqual([
        "(hover: hover)",
        "(pointer: fine)",
        "(pointer: coarse)",
        "(prefers-reduced-motion: reduce)",
        "(forced-colors: active)",
      ]);
      expect(listeners).toHaveLength(queries.length); // All use Safari legacy addListener branch
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("★导出面一个动作按钮都不出，但标题必须留着（116 dogfood）", () => {
    // 2026-08-15 116: successful V2 exports had PDF charts topped with "Line ▾ / Open Data /
    // Download PNG / Download CSV", four buttons unclickable on paper (also always present in golden PNG).
    // Cause: criterion originally applied only to inspect **one** action's `hidden`.
    //
    // ⚠ Omit `data_artifact_id`: providing it takes ArtifactPreview path (measured in jsdom),
    // never reaching header. Verify `surface` **passes through** to action bar here.
    const spec: ChartSpec = {
      kind: "bar", title: "导出面图题", row_count: 2,
      columns: [
        { key: "c", header: "类目", type: "categorical" },
        { key: "v", header: "值", type: "numeric" },
      ],
      rows: [{ c: "A", v: 1 }, { c: "B", v: 2 }],
    };

    // Interactive surface: action bar exists (positive control — otherwise assertions below could merely reflect
    // this component being unable to render buttons in test environment)
    const onScreen = render(<ChartBlock spec={spec} />);
    expect(onScreen.container.querySelector(".v2-chart-actions")).not.toBeNull();
    expect(onScreen.container.querySelectorAll(".v2-chart-action-btn").length).toBeGreaterThan(0);
    onScreen.unmount();

    // Export surface: no actions, title remains
    const exported = render(<ChartBlock spec={spec} surface="export" />);
    expect(exported.container.querySelector(".v2-chart-actions")).toBeNull();
    expect(exported.container.querySelectorAll(".v2-chart-action-btn").length).toBe(0);
    expect(exported.container.querySelector(".v2-chart-title")?.textContent).toBe("导出面图题");
  });
});
