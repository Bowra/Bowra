import { describe, expect, it, vi } from "vitest";
import { buildChartJSConfig } from "./build-config";
import { chartThemeBaseForMode } from "../chart-theme";
import { linkedFocusFixture } from "../../e2e/_chart-linked-focus-fixture";
import { evidencePlanForViewer } from "./presentation-plan";
import { applyChartEdit } from "./composition";
import { tupleKey, type FocusBinding } from "./linked-focus";
import { redactViewerSourceIdentitiesInValue } from "../canvas-pm-adapter/citation-types";
import { ensureFullRows } from "./view-transforms";
import { needsRowCompletion } from "./complete-chart-rows";
import { fetchFilePreview } from "../api";

vi.mock("../api", () => ({ fetchFilePreview: vi.fn() }));

describe("native typed focus", () => {
  it("maps grouped and identity scatter points after missing-value filtering", () => {
    const spec = linkedFocusFixture().spec;
    const binding = evidencePlanForViewer(spec.presentation_plan)!.bindings!["entity.fixture"] as FocusBinding;
    for (const count of [3, 8]) {
      const child = { ...spec.views![0]!.spec, rows: spec.rows!.slice(0, count) };
      const config = buildChartJSConfig(child, chartThemeBaseForMode("light"), { focusBinding: { descriptor: binding, datasetDigest: "a".repeat(64) } });
      const marks = config.markBindings!.flat();
      expect(marks).toHaveLength(count === 3 ? 3 : 7);
      expect(marks.map((mark) => mark.focusTuple[0])).not.toContain("Missing");
      expect(marks[0]!.observations.map((observation) => [observation.measureKey, observation.value])).toEqual([["revenue", 52.12345], ["cost", 30.16789]]);
      expect(marks.find((mark) => mark.focusTuple[0] === "North")?.observations[0]?.value).toBe(count === 3 ? undefined : 60);
      expect(marks.every((mark) => mark.representation === "original")).toBe(true);
    }
    expect(tupleKey([1, null, ""])).not.toBe(tupleKey(["1", null, ""]));
  });
  it("retains sorted category identity and original decimals; static exports have no focus marks", () => {
    const spec = linkedFocusFixture().spec;
    const binding = evidencePlanForViewer(spec.presentation_plan)!.bindings!["entity.fixture"] as FocusBinding;
    const child = spec.views![1]!.spec;
    const options = { focusBinding: { descriptor: binding, datasetDigest: "a".repeat(64) } };
    const config = buildChartJSConfig(child, chartThemeBaseForMode("dark"), options);
    expect(config.markBindings![0]![0]!.focusTuple).toEqual(["Other region"]);
    expect(config.markBindings![0]!.at(-1)!.observations[0]!.value).toBe(52.12345);
    const exported = buildChartJSConfig(child, chartThemeBaseForMode("light"), { ...options, surface: "export" });
    expect(exported.markBindings).toBeUndefined();
  });
  it("keeps one saved root for valid child edits and rejects grain-changing kinds", () => {
    const root = linkedFocusFixture().spec;
    const plan = evidencePlanForViewer(root.presentation_plan)!;
    expect(evidencePlanForViewer({ ...plan, view_bindings: { comparison: "entity.fixture", detail: "entity.fixture", relationship: "entity.fixture" } })).not.toBeNull();
    const edited = applyChartEdit(root, { scope: "view", viewId: "comparison" }, { kind: "bar" });
    expect(edited.presentation_plan).toBe(root.presentation_plan);
    expect(edited.views![0]).toBe(root.views![0]);
    expect(() => applyChartEdit(root, { scope: "view", viewId: "detail" }, { kind: "kpi" })).toThrow();
    const malformed = structuredClone(root.presentation_plan) as Record<string, unknown>;
    malformed.interactions = [{ preset: "linked_focus", from_view: "detail", to_views: ["detail"], binding_id: "entity.fixture" }];
    expect(evidencePlanForViewer(malformed)).toBeNull();
  });
  it("accepts valid view IDs that also name JavaScript prototype properties", () => {
    const value = JSON.parse(JSON.stringify(linkedFocusFixture().spec.presentation_plan).replaceAll("relationship", "constructor"));
    expect(evidencePlanForViewer(value)).not.toBeNull();
  });
  it("revokes descriptors immediately and cannot fetch or silently broaden planned populations", async () => {
    const root = linkedFocusFixture().spec;
    const plan = evidencePlanForViewer(root.presentation_plan)!;
    const redacted = redactViewerSourceIdentitiesInValue(root, new Set([plan.dataset.artifact_id]));
    expect(evidencePlanForViewer(redacted.presentation_plan)!.bindings!["entity.fixture"]).toEqual({ kind: "entity", availability: "unavailable" });
    expect(redacted.rows).toBe(root.rows);
    const partial = { ...root, row_count: 100, rows: root.rows!.slice(0, 2) };
    expect(needsRowCompletion(partial)).toBe(false);
    expect((await ensureFullRows(partial)).ok).toBe(false);
    expect(fetchFilePreview).not.toHaveBeenCalled();
  });
});
