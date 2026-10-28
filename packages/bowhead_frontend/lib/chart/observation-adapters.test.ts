import { describe, expect, it } from "vitest";
import { observationAdaptersFixture } from "../../e2e/_chart-observation-adapters-fixture";
import { chartThemeBaseForMode } from "../chart-theme";
import { buildChartJSConfig } from "./build-config";
import { evidencePlanForViewer } from "./presentation-plan";
import { observationViewEligible, type FocusBinding } from "./linked-focus";
import { applyTimeWindow, timeWindowDomain } from "./time-window";
import { applyChartEdit } from "./composition";
import { selectedContextForView } from "./selected-context";

describe("original observations through native transforms", () => {
  const root = observationAdaptersFixture().spec;
  const binding = evidencePlanForViewer(root.presentation_plan)!.bindings!["time.fixture"] as FocusBinding;
  const trend = root.views![0]!.spec;
  const render = (spec = trend, descriptor = binding, scopeSource?: typeof trend) =>
    buildChartJSConfig(spec, chartThemeBaseForMode("light"), { focusBinding: { descriptor, datasetDigest: "a".repeat(64), scopeSource } });

  it("retains typed source tuples, tiny values, zero and missingness after pivot and date sorting", () => {
    const config = render();
    expect(config.data.labels).toEqual(["2026-01", "2026-02"]);
    expect(config.data.datasets.map((dataset) => [dataset.label, dataset.data])).toEqual([
      ["East", [52.12345, 0]],
      ["West", [0.000012345, null]],
    ]);
    expect(config.markBindings![0]![0]!.observations[0]).toEqual({
      datasetDigest: "a".repeat(64),
      keyTuple: ["East", "2026-01"],
      measureKey: "revenue",
      value: 52.12345,
    });
    expect(config.markBindings![1]![1]!.observations[0]!.value).toBeNull();
    expect(config.markBindings![1]![0]!.focusTuple).toEqual(["2026-01"]);
    expect(render(trend, { ...binding, kind: "entity", dimension_keys: ["region"] }).markBindings![1]![0]!.focusTuple).toEqual(["West"]);
    expect(render(trend, { ...binding, kind: "row", dimension_keys: ["region", "month"] }).markBindings![1]![0]!.focusTuple).toEqual([
      "West",
      "2026-01",
    ]);
  });

  it("retains canonical series and exact origin in a one-period local window", () => {
    const domain = timeWindowDomain(
      root.views!.map((view) => view.spec),
      binding,
    )!;
    expect(domain).toEqual(["2026-01", "2026-02"]);
    const scoped = applyTimeWindow(trend, binding, domain, { start: 1, end: 1 });
    const config = render(scoped, binding, trend);
    expect(config.data.labels).toEqual(["2026-02"]);
    expect(config.data.datasets.map((dataset) => [dataset.label, dataset.data])).toEqual([
      ["East", [0]],
      ["West", [null]],
    ]);
    expect(config.markBindings![0]![0]!.observations[0]!.keyTuple).toEqual(["East", "2026-02"]);
    expect(config.markBindings![1]![0]!.observations[0]!.value).toBeNull();
    expect(config.data.datasets.map((dataset) => dataset.borderColor)).toEqual(render().data.datasets.map((dataset) => dataset.borderColor));
  });

  it("maps scatter after missing filtering and expands entity or period focus into complete raw tuples", () => {
    const scatter = root.views![1]!.spec;
    expect(
      render(scatter)
        .markBindings!.flat()
        .map((mark) => mark.observations[0]!.keyTuple),
    ).toEqual([
      ["East", "2026-02"],
      ["East", "2026-01"],
      ["West", "2026-01"],
    ]);
    const scope = {
      version: 1 as const,
      sectionId: "sec_observations",
      sectionRev: 1,
      viewId: "detail",
      bindingId: "time.fixture",
      datasetDigest: "a".repeat(64),
    };
    expect(selectedContextForView(scope, binding, { bindingId: scope.bindingId, tuple: ["2026-02"] }, root.views![2]!.spec)?.tuples).toEqual([
      ["East", "2026-02"],
      ["West", "2026-02"],
    ]);
    expect(
      selectedContextForView(
        scope,
        { ...binding, kind: "entity", dimension_keys: ["region"] },
        { bindingId: scope.bindingId, tuple: ["West"] },
        root.views![2]!.spec,
      )?.tuples,
    ).toEqual([
      ["West", "2026-01"],
      ["West", "2026-02"],
    ]);
    expect(() => applyChartEdit(root, { scope: "view", viewId: "trend" }, { kind: "dual_axis" })).toThrow();
    expect(applyChartEdit(root, { scope: "view", viewId: "trend" }, { kind: "hbar" }).presentation_plan).toBe(root.presentation_plan);
  });

  it("maps time-wide transposition to original column measures and never selects duplicate aggregates", () => {
    const spec = {
      ...trend,
      columns: [trend.columns[0]!, ...["2026-01", "2026-02", "2026-03"].map((key) => ({ key, header: key, type: "numeric" as const, unit: "USD" }))],
      rows: [
        { region: "East", "2026-01": 52.12345, "2026-02": null, "2026-03": 0 },
        { region: "West", "2026-01": 0.000012345, "2026-02": -5, "2026-03": 2 },
      ],
    };
    const descriptor: FocusBinding = {
      ...binding,
      kind: "entity",
      dimension_keys: ["region"],
      observation_keys: ["region"],
      measure_keys: spec.columns.slice(1).map((column) => column.key),
    };
    const config = render(spec, descriptor);
    expect(config.data.datasets[0]!.data).toEqual([52.12345, null, 0]);
    expect(config.markBindings![0]![1]!.observations).toEqual([
      { datasetDigest: "a".repeat(64), keyTuple: ["East"], measureKey: "2026-02", value: null },
    ]);
    const duplicate = { ...trend, rows: [...trend.rows!, trend.rows![0]!] };
    expect(observationViewEligible(duplicate, binding)).toBe(false);
    expect(
      render(duplicate)
        .markBindings!.flat()
        .every((mark) => !mark.observations.length),
    ).toBe(true);
  });
});
