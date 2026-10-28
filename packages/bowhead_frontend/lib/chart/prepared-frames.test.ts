import { defaultChartTheme } from "../chart-theme";
import { describe, expect, it } from "vitest";
import { preparedFramesFixture } from "../../e2e/_chart-prepared-frames-fixture";
import { evidencePlanForViewer } from "./presentation-plan";
import { prepareFrameViews, type FrameBinding } from "./prepared-frames";
import { buildChartJSConfig } from "./build-config";
import { applyChartEdit } from "./composition";
import { redactViewerSourceIdentitiesInValue } from "../canvas-pm-adapter/citation-types";

const theme = { ...defaultChartTheme(), ink: "#1A1916", inkSoft: "#57544D", primary: "#2C5F2D", palette: ["#2C5F2D", "#97BC62"], grid: "#E5E2DA", surface: "#fff" };
describe("native prepared frame semantics", () => {
  it("keeps sparse original cells, stable entity colors and whole-dataset domains", () => {
    const root = preparedFramesFixture().spec;
    const binding = evidencePlanForViewer(root.presentation_plan)!.bindings!["frame.fixture"] as FrameBinding;
    const frames = prepareFrameViews(new Map(root.views!.map((view) => [view.id, view.spec])), binding, binding.eligible_views)!;
    const first = frames.get("relationship")!.get('["2026-01"]')!;
    const last = frames.get("relationship")!.get('["2026-03"]')!;
    expect(first.rows!.map((row) => row.region)).toEqual(["A", "B"]);
    expect(first.rows![0].revenue).toBeNull();
    expect(last.rows).toHaveLength(1);
    expect(last.rows![0]).toBe(root.rows![0]);
    const configs = [first, last].map((spec) => buildChartJSConfig(spec, theme));
    expect(configs[0].data.datasets.find((series) => series.label === "A")!.borderColor).toEqual(configs[1].data.datasets.find((series) => series.label === "A")!.borderColor);
    expect(configs[0].data.datasets.find((series) => series.label === "A")!.data).toEqual([]);
    configs.forEach((config) => { expect(config.options.scales!.x).toMatchObject({ min: -6, max: 14 }); expect(config.options.scales!.y).toMatchObject({ min: 1, max: 8 }); expect(config.options.animation).toBe(false); });
    expect(root.rows).toHaveLength(5);
    expect(frames.get("detail")!.get('["2026-01"]')!.columns).toBe(root.columns);
  });
  it("retains identity-mode point colors when the frame loses entities", () => {
    const root = preparedFramesFixture().spec;
    const binding = evidencePlanForViewer(root.presentation_plan)!.bindings!["frame.fixture"] as FrameBinding;
    binding.entity_values.push("C");
    const frames = prepareFrameViews(new Map(root.views!.map((view) => [view.id, view.spec])), binding, binding.eligible_views)!;
    const second = buildChartJSConfig(frames.get("relationship")!.get('["2026-02"]')!, theme);
    const last = buildChartJSConfig(frames.get("relationship")!.get('["2026-03"]')!, theme);
    expect(second.data.datasets[0].borderColor).toEqual([theme.palette[0], theme.palette[1]]);
    expect(last.data.datasets[0].borderColor).toEqual([theme.palette[0]]);
  });
  it("rejects invented frames, playback settings, duplicate tuples and changed encodings", () => {
    const root = preparedFramesFixture().spec;
    const plan = evidencePlanForViewer(root.presentation_plan)!;
    const binding = plan.bindings!["frame.fixture"] as FrameBinding;
    const damaged = structuredClone(plan);
    (damaged.interactions![0] as unknown as Record<string, unknown>).speed = 10;
    expect(evidencePlanForViewer(damaged)).toBeNull();
    binding.frame_keys[1] = "2026-02-31";
    expect(evidencePlanForViewer(plan)).toBeNull();
    const valid = preparedFramesFixture().spec;
    const descriptor = evidencePlanForViewer(valid.presentation_plan)!.bindings!["frame.fixture"] as FrameBinding;
    const views = new Map(valid.views!.map((view) => [view.id, view.spec]));
    views.get("detail")!.rows![0] = views.get("detail")!.rows![1];
    expect(prepareFrameViews(views, descriptor, descriptor.eligible_views)).toBeNull();
    expect(() => applyChartEdit(preparedFramesFixture().spec, { scope: "view", viewId: "relationship" }, { kind: "line" })).toThrow("saved scatter or Table encoding");
  });
  it("keeps inaccessible frame descriptors closed and cannot enable a player", () => {
    const root = preparedFramesFixture().spec;
    const redacted = redactViewerSourceIdentitiesInValue(root, new Set(["frames.fixture"]));
    expect(evidencePlanForViewer(redacted.presentation_plan)?.bindings!["frame.fixture"]).toEqual({ kind: "frame", availability: "unavailable" });
  });
});
