/**
 * Table virtual scrolling (chart-config-ux batch 5).
 *
 * ⚠ jsdom has **no layout**: scroll container height is always 0, preventing `useVirtualizer` from computing viewport.
 * This exposes the worst failure: without computable visible items, virtualization renders an **empty** table.
 * Thus these cases focus less on DOM savings (which require measurement in a real browser),
 * and more on **rows never disappearing under any circumstances**.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChartTable } from "./chart-table";
import type { ChartSpec } from "@/lib/chart/types";

vi.mock("next-intl", () => ({
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string, values?: Record<string, unknown>) => {
    if (key === "showing") return `显示 ${values?.start}-${values?.end} / 共 ${values?.total}`;
    if (key === "expand_all") return `展开全部 ${values?.total} 行`;
    return key;
  },
}));

vi.mock("@/lib/chart/view-transforms", async (orig) => {
  const mod = await orig<typeof import("@/lib/chart/view-transforms")>();
  return { ...mod, ensureFullRows: vi.fn(async (s: ChartSpec) => ({ spec: s, truncated: false, ok: true })) };
});


/**
 * Add fake layout to jsdom so `useVirtualizer` can actually compute viewport.
 *
 * jsdom performs no layout: all rects are 0 and `ResizeObserver` is absent, so virtualizer never produces
 * visible items — the premise of fail-open case above, but also meaning **not one line of virtualization branch
 * executes in CI**. Feed container height VIEWPORT_H and row height ROW_H here,
 * enabling the virtualization branch within unit tests.
 *
 * ⚠ This **fake layout** checks pure computations such as windowing, spacer height, and aria annotations only;
 * real-browser DOM savings are not measured here (evidence: sprint document §5.3 A/B).
 */
const VIEWPORT_H = 400;
const ROW_H = 36;

function installFakeLayout() {
  class FakeResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);

  const rect = (h: number, top = 0) =>
    ({ width: 800, height: h, top, left: 0, right: 800, bottom: top + h, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;

  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.classList.contains("v2-chart-table-scroll")) return rect(VIEWPORT_H);
    if (this.tagName === "TR") return rect(ROW_H);
    return rect(0);
  });
  for (const [prop, value] of [
    ["clientHeight", VIEWPORT_H],
    ["offsetHeight", VIEWPORT_H],
  ] as const) {
    Object.defineProperty(HTMLElement.prototype, prop, { configurable: true, value });
    patchedProps.push(prop);
  }
}

/** `defineProperty` is outside `vi.restoreAllMocks()`; undo it ourselves — prototype residue leaks into
 *  later cases in this file (especially fail-open without layout, whose premise is **no** height). */
const patchedProps: string[] = [];
function uninstallFakeLayout() {
  for (const prop of patchedProps.splice(0)) {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>)[prop];
  }
}

afterEach(() => {
  cleanup();
  uninstallFakeLayout();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function bigSpec(n: number): ChartSpec {
  return {
    kind: "table",
    title: "大表",
    row_count: n,
    columns: [
      { key: "店", header: "门店", type: "categorical" },
      { key: "城", header: "城市", type: "categorical" },
      { key: "额", header: "营业额", type: "numeric" },
      { key: "比", header: "同比", type: "numeric", format: "percent" },
    ],
    rows: Array.from({ length: n }, (_, i) => ({ 店: `S${i}`, 城: "北京", 额: n - i, 比: 0.1 })),
  };
}

const bodyRows = () => document.querySelectorAll(".v2-chart-table-el tbody tr:not([aria-hidden])").length;

describe("表格虚拟滚动", () => {
  it("★★ 算不出可视区时 fail-open 全渲——绝不能渲成空表", () => {
    // jsdom container height is 0 ⇒ virtualizer cannot produce visible items. Fall back to full rendering.
    // Removing `virtualItems.length > 0` from `useVirtual` fails this case (0 rows rendered).
    render(<ChartTable spec={bigSpec(500)} chromeless />);
    expect(bodyRows()).toBe(500);
  });

  it("行数少于阈值时根本不进虚拟化（少量行直接渲更简单）", () => {
    render(<ChartTable spec={bigSpec(30)} chromeless />);
    expect(bodyRows()).toBe(30);
  });

  it("★ 导出面永不虚拟化，且仍按导出上限截断 + 页脚说明", () => {
    // Export captures one frame; virtualization there would capture only part of the table.
    render(<ChartTable spec={bigSpec(500)} surface="export" />);
    expect(bodyRows()).toBe(25);
    expect(screen.getByText(/共 500/)).toBeInTheDocument();
  });

  it("折叠态（未展开）只渲前 10 行，且没有占位行", () => {
    render(<ChartTable spec={bigSpec(500)} />);
    expect(bodyRows()).toBe(10);
    expect(document.querySelectorAll("tbody tr[aria-hidden]").length).toBe(0);
  });
});

describe("表格虚拟滚动 · 真开起来（假布局）", () => {
  const realRows = () =>
    [...document.querySelectorAll(".v2-chart-table-el tbody tr")].filter((t) => !t.hasAttribute("aria-hidden"));
  const spacers = () => [...document.querySelectorAll(".v2-chart-table-el tbody tr[aria-hidden]")];

  it("★ 5000 行只渲一屏 + 一条底部占位撑出全长", () => {
    installFakeLayout();
    render(<ChartTable spec={bigSpec(5000)} chromeless />);
    const rendered = realRows();
    // 400px viewport / 36px row ≈ 11 rows, plus overscan 12 — far fewer than 5000 is the entire criterion;
    // exact counts vary with virtualizer implementation; fixing them creates brittle assertions.
    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.length).toBeLessThan(60);
    // First viewport: no top spacer; bottom spacer must represent remaining ~4970 rows.
    const pads = spacers();
    expect(pads).toHaveLength(1);
    expect(parseInt((pads[0] as HTMLElement).style.height, 10)).toBeGreaterThan(4000 * ROW_H);
  });

  it("★★ 渲的是**开头**那一屏，不是随便一段——首行必须是第 1 行", () => {
    installFakeLayout();
    render(<ChartTable spec={bigSpec(5000)} chromeless />);
    expect(realRows()[0]!.querySelector("td")!.textContent).toBe("S0");
  });

  it("★ 虚拟化时必须标 aria-rowcount/rowindex——DOM 里只有一屏，读屏软件否则数出来的行数是错的", () => {
    installFakeLayout();
    render(<ChartTable spec={bigSpec(5000)} chromeless />);
    const table = document.querySelector(".v2-chart-table-el")!;
    expect(table.getAttribute("aria-rowcount")).toBe("5001"); // 5000 data rows + header
    expect(table.querySelector("thead tr")!.getAttribute("aria-rowindex")).toBe("1");
    expect(realRows()[0]!.getAttribute("aria-rowindex")).toBe("2");
  });

  it("★ 不虚拟化时不标 aria-rowcount——DOM 本来就是全的，多一份口径就多一处会漂", () => {
    installFakeLayout();
    render(<ChartTable spec={bigSpec(30)} chromeless />);
    expect(document.querySelector(".v2-chart-table-el")!.hasAttribute("aria-rowcount")).toBe(false);
    expect(realRows()).toHaveLength(30);
  });

  it("★ 占位行不进无障碍树，也不许影响列宽（colSpan 跟着列数走）", () => {
    installFakeLayout();
    render(<ChartTable spec={bigSpec(5000)} chromeless />);
    for (const pad of spacers()) {
      expect(pad.getAttribute("aria-hidden")).not.toBeNull();
      expect(pad.querySelector("td")!.getAttribute("colspan")).toBe("4");
    }
  });

  it("★ 导出面：有假布局也只出 25 行整表，没有占位行", () => {
    // Pin the **user-visible result** (PDF contains all 25 rows, rather than virtualization cutting half away),
    // rather than which guard does it — component `!isExport` is a redundant second guard,
    // and removing it alone will not fail this case (see component comment).
    installFakeLayout();
    render(<ChartTable spec={bigSpec(5000)} surface="export" />);
    expect(realRows()).toHaveLength(25);
    expect(spacers()).toHaveLength(0);
  });
});

describe("virtualized tables keep stable column widths", () => {
  it("marks a virtualized table so the report keeps it on fixed layout with a per-column minimum width", () => {
    installFakeLayout();
    const spec = bigSpec(5000);
    render(<ChartTable spec={spec} chromeless />);
    const scroll = document.querySelector(".v2-chart-table-scroll")!;
    expect(scroll).toHaveClass("is-virtual");
    const table = scroll.querySelector<HTMLElement>("table.v2-chart-table-el")!;
    expect(table.style.getPropertyValue("--chart-table-columns")).toBe(String(spec.columns.length));
  });

  it("leaves a fully rendered table unmarked so its columns size by content", () => {
    installFakeLayout();
    render(<ChartTable spec={bigSpec(30)} chromeless />);
    expect(document.querySelector(".v2-chart-table-scroll")).not.toHaveClass("is-virtual");
    const table = document.querySelector<HTMLElement>("table.v2-chart-table-el")!;
    expect(table.style.getPropertyValue("--chart-table-columns")).toBe("");
  });
});
