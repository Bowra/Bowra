import { describe, expect, it, vi } from "vitest";
import { annotationsFixture } from "../../e2e/_chart-annotations-fixture";
import { annotationBinding, annotationCaption, annotationMatchesRow, annotationTarget, observationRows, validAnnotations, type ObservationAnnotation } from "./annotations";
import { annotationAtPoint, placeAnnotationLabel } from "./annotation-layout";
import { evidencePlanForViewer } from "./presentation-plan";
import { buildChartJSConfig } from "./build-config";
import { chartThemeBaseForMode } from "../chart-theme";
import { applyChartEdit, chartBrowsingIdentity } from "./composition";

function annotatedView(id = "relationship") {
  const spec = annotationsFixture().spec;
  return {
    ...spec.views!.find((view) => view.id === id)!.spec,
    _export_annotations: evidencePlanForViewer(spec.presentation_plan)!.annotations!.filter(
      (item): item is ObservationAnnotation => item.availability === "available" && item.view_id === id,
    ),
  };
}

describe("native observed annotations", () => {
  it.each(["line", "hbar"] as const)("draws supplied event/coverage scope on the %s time axis and clears hidden targets", (kind) => {
    const original = annotatedView();
    const spec = { ...original, kind, _export_annotations: original._export_annotations.map((item) => ({ ...item, encoding: kind, role: item.kind === "interval" ? "coverage" as const : "event" as const })) };
    const config = buildChartJSConfig(spec, chartThemeBaseForMode("light"));
    const plugin = config.plugins!.find((item) => item.id === "nativeAnnotationScope")!;
    const fillRect = vi.fn(), moveTo = vi.fn(), lineTo = vi.fn();
    let visible = true;
    const chart = {
      chartArea: { left: 0, right: 200, top: 10, bottom: 180 },
      ctx: { save: vi.fn(), restore: vi.fn(), beginPath: vi.fn(), rect: vi.fn(), clip: vi.fn(), setLineDash: vi.fn(), fillRect, moveTo, lineTo, stroke: vi.fn() },
      isDatasetVisible: () => visible,
      getDatasetMeta: () => ({ data: original.rows!.map((_, i) => ({ getProps: () => ({ x: 20 + i * 20, y: 30 + i * 20 }) })) }),
    };
    plugin.beforeDatasetsDraw!(chart as never, {} as never, {} as never);
    expect(fillRect).toHaveBeenCalledTimes(1);
    expect(moveTo).toHaveBeenCalledTimes(1);
    expect(kind === "hbar" ? moveTo.mock.calls[0][0] : moveTo.mock.calls[0][1]).toBe(kind === "hbar" ? 0 : 10);
    expect(kind === "hbar" ? lineTo.mock.calls[0][0] : lineTo.mock.calls[0][1]).toBe(kind === "hbar" ? 200 : 180);
    visible = false; fillRect.mockClear(); moveTo.mockClear();
    plugin.beforeDatasetsDraw!(chart as never, {} as never, {} as never);
    expect(fillRect).not.toHaveBeenCalled(); expect(moveTo).not.toHaveBeenCalled();
  });
  it("resolves pinned original observations without choosing replacement interval endpoints", () => {
    const spec = annotatedView();
    const binding = annotationBinding(spec)!;
    const point = annotationTarget(spec._export_annotations[0]);
    expect(observationRows(point, { ...spec, rows: [...spec.rows!].reverse() }, binding)?.[0].revenue).toBe(52.12345);
    const interval = annotationTarget(spec._export_annotations[1]);
    expect(observationRows(interval, spec, binding)).toHaveLength(6);
    expect(observationRows(interval, { ...spec, rows: spec.rows!.filter((row) => row.month !== "2026-01") }, binding)).toEqual([]);
    expect(observationRows({ ...point, measureKey: "unbound" }, spec, binding)).toEqual([]);
    const table = annotatedView("detail");
    expect(observationRows(annotationTarget(table._export_annotations[0]), table, annotationBinding(table)!)?.[0].revenue).toBeNull();
  });
  it("keeps annotation text within the plot and yields to value labels and fills", () => {
    const bounds = { left: 0, right: 200, top: 0, bottom: 160 };
    const obstacles = { fills: [], labels: [], markers: [], segments: [] };
    expect(placeAnnotationLabel({ x: 100, y: 80 }, 70, bounds, obstacles)).not.toBeNull();
    expect(placeAnnotationLabel({ x: 0, y: 80 }, 70, bounds, obstacles)?.left).toBeGreaterThanOrEqual(0);
    expect(placeAnnotationLabel({ x: 100, y: 80 }, 220, bounds, obstacles)).toBeNull();
    expect(placeAnnotationLabel({ x: 100, y: 80 }, 70, bounds, { ...obstacles, fills: [bounds] })).toBeNull();
    expect(placeAnnotationLabel({ x: 100, y: 80 }, 70, bounds, { ...obstacles, labels: [bounds] })).toBeNull();
  });
  it("keeps a crowded remark activatable and refreshes hit targets when its series disappears", () => {
    const spec = annotatedView();
    spec._export_annotations = [{ ...spec._export_annotations[0], label: "An intentionally long annotation that cannot fit inside this narrow chart" }];
    const config = buildChartJSConfig(spec, chartThemeBaseForMode("light"));
    const plugin = config.plugins!.find((item) => item.id === "nativeObservedAnnotations")!;
    const fillText = vi.fn(), arc = vi.fn();
    let visible = true;
    const chart = {
      data: config.data, options: { plugins: { nativeObservedAnnotations: { activeId: "observed" } } },
      chartArea: { left: 0, right: 120, top: 0, bottom: 300 },
      ctx: { save: vi.fn(), restore: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(), arc, fillText, measureText: (text: string) => ({ width: text.length * 6 }) },
      isDatasetVisible: () => visible,
      getDatasetMeta: () => ({ type: "line", data: spec.rows!.map((_, index) => ({ getProps: () => ({ x: index * 20, y: 130 + index * 20 }) })) }),
    };
    plugin.afterDatasetsDraw!(chart as never, {} as never, {} as never, {} as never);
    expect(fillText).toHaveBeenCalledTimes(1);
    const [label, x, y] = fillText.mock.calls[0];
    expect(label).toBe("1");
    expect(annotationAtPoint(chart as never, x, y)).toBe("observed");
    expect(arc.mock.calls.some(([, , radius]) => radius === 8)).toBe(true);
    visible = false;
    plugin.afterDatasetsDraw!(chart as never, {} as never, {} as never, {} as never);
    expect(annotationAtPoint(chart as never, x, y)).toBeNull();
  });
  it("preserves original values, missingness, intervals and per-measure units", () => {
    const spec = annotatedView();
    expect(annotationCaption(spec._export_annotations[0], spec)).toContain("52.12");
    expect(annotationCaption(spec._export_annotations[1], spec)).toContain("2026-01 – 2026-06");
    const table = annotatedView("detail");
    expect(annotationCaption(table._export_annotations[0], table)).toMatch(/Missing observation.*—/);
    expect(annotationMatchesRow(table._export_annotations[0], { month: "2026-03", revenue: 999 })).toBe(true);
    const filtered = { ...spec, rows: spec.rows!.filter((row) => row.month !== "2026-01") };
    expect(annotationCaption(spec._export_annotations[0], filtered)).toBeNull();
    expect(annotationCaption(spec._export_annotations[0], { ...spec, kind: "table" })).toBeNull();
  });
  it("draws only actual numeric observations and supplied interval endpoints", () => {
    const spec = annotatedView();
    const config = buildChartJSConfig(spec, chartThemeBaseForMode("light"), { surface: "export" });
    const plugin = config.plugins!.find((item) => item.id === "nativeObservedAnnotations")!;
    const arc = vi.fn(),
      fillText = vi.fn();
    const chart = {
      data: config.data, options: {},
      chartArea: { left: 0, right: 900, top: 0, bottom: 300 },
      ctx: {
        save: vi.fn(),
        restore: vi.fn(),
        beginPath: vi.fn(),
        arc,
        stroke: vi.fn(),
        moveTo: vi.fn(),
        lineTo: vi.fn(),
        strokeRect: vi.fn(),
        fillRect: vi.fn(),
        fillText,
        measureText: (text: string) => ({ width: text.length * 5 }),
      },
      isDatasetVisible: () => true,
      getDatasetMeta: () => ({ data: spec.rows!.map((_, index) => ({ getProps: () => ({ x: index * 100 + 20, y: 150 }) })) }),
    };
    plugin.afterDatasetsDraw!(chart as never, {} as never, {} as never, {} as never);
    // A point and an interval may mark the same supplied endpoint; only two distinct observations exist.
    expect(new Set(arc.mock.calls.map(([x, y]) => `${x},${y}`)).size).toBe(2);
    expect(fillText).toHaveBeenCalledTimes(2);
    expect(fillText.mock.calls.map(([text]) => String(text))).toEqual(["1", "2. Supplied period"]);
    expect(annotationCaption(spec._export_annotations[0], spec)).toContain("Observed revenue");
    // Export labels must yield to the installed value-label boxes just like screen remarks.
    Object.assign(chart, { $datalabels: { _labels: [{ $layout: { _visible: true, _box: { _points: () => [
      { x: 0, y: 0 }, { x: 900, y: 0 }, { x: 900, y: 300 }, { x: 0, y: 300 },
    ] } } }] } });
    fillText.mockClear();
    plugin.afterDatasetsDraw!(chart as never, {} as never, {} as never, {} as never);
    expect(fillText).not.toHaveBeenCalled();
    Object.assign(chart, { $datalabels: { _labels: [] } });
    arc.mockClear();
    fillText.mockClear();
    const hidden = { ...chart, isDatasetVisible: () => false };
    plugin.afterDatasetsDraw!(hidden as never, {} as never, {} as never, {} as never);
    expect(arc).not.toHaveBeenCalled();
    expect(fillText).not.toHaveBeenCalled();
  });
  it("keeps source-protected annotations closed and guide targets root-local", () => {
    const spec = annotationsFixture(true).spec;
    const plan = evidencePlanForViewer(spec.presentation_plan)!;
    expect(validAnnotations(plan.annotations)).toBe(true);
    const forged = structuredClone(plan);
    (forged.annotations![0] as ObservationAnnotation & { kind: "point" }).tuple = [true as never];
    expect(evidencePlanForViewer(forged)).toBeNull();
    const target = structuredClone(plan);
    target.guide_steps![0].annotation_id = "missing";
    expect(evidencePlanForViewer(target)).toBeNull();
    const redacted = structuredClone(spec);
    const safe = evidencePlanForViewer(redacted.presentation_plan)!;
    safe.annotations = safe.annotations!.map((item) => ({ id: item.id, view_id: item.view_id, availability: "unavailable" }));
    safe.guide_steps!.forEach((step) => {
      step.title = "";
    });
    safe.evidence_notes = [
      {
        id: "methodology",
        measure_keys: ["revenue"],
        source_ref: { id: "methodology.fixture", access_state: "linked_restricted" },
        availability: "unavailable",
      },
    ];
    expect(evidencePlanForViewer(redacted.presentation_plan)).not.toBeNull();
    expect(chartBrowsingIdentity(redacted)).toBe(chartBrowsingIdentity(spec));
    expect(() => applyChartEdit(spec, { scope: "view", viewId: "relationship" }, { kind: "bar" })).toThrow();
  });
});
