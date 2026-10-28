import { describe, expect, it } from "vitest";
import { timeWindowFixture } from "../../e2e/_chart-time-window-fixture";
import { timeWindowDomain, applyTimeWindow } from "./time-window";
import { evidencePlanForViewer } from "./presentation-plan";
import type { FocusBinding } from "./linked-focus";
import { redactViewerSourceIdentitiesInValue } from "../canvas-pm-adapter/citation-types";

describe("verified local time windows", () => {
  it("sorts actual dates, retains missing/negative original cells, and leaves saved scope unchanged", () => {
    const root = timeWindowFixture().spec;
    const binding = evidencePlanForViewer(root.presentation_plan)!.bindings!["time.fixture"] as FocusBinding;
    const domain = timeWindowDomain(root.views!.map((view) => view.spec), binding)!;
    expect(domain).toEqual(["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]);
    const local = applyTimeWindow(root.views![0]!.spec, binding, domain, { start: 1, end: 2 });
    expect(local.rows).toEqual([{ month: "2026-03", revenue: null, cost: 9 }, { month: "2026-02", revenue: -5, cost: 12 }]);
    expect(local.row_count).toBe(2);
    expect(root.rows).toHaveLength(6);
    expect(local.rows![0]).toBe(root.rows![3]);
    expect(applyTimeWindow(root, binding, domain, { start: 4, end: 1 })).toBe(root);
  });
  it("rejects manufactured chronology and erases revoked time descriptors", () => {
    const root = timeWindowFixture().spec;
    const plan = evidencePlanForViewer(root.presentation_plan)!;
    const binding = plan.bindings!["time.fixture"] as FocusBinding;
    for (const values of [["2026-01", "2026-01-01"], ["2026-02-31"], [null], [1, 2]]) {
      expect(timeWindowDomain([{ ...root, rows: values.map((month) => ({ month })) }], binding)).toBeNull();
    }
    expect(timeWindowDomain([{ ...root, columns: [{ key: "year", header: "Year", type: "date" }], rows: [{ year: 2026 }, { year: 2025 }] }], { ...binding, time_key: "year", observation_keys: ["year"], order: "numeric" })).toEqual([2025, 2026]);
    const revoked = redactViewerSourceIdentitiesInValue(root, new Set([plan.dataset.artifact_id]));
    expect(evidencePlanForViewer(revoked.presentation_plan)!.bindings!["time.fixture"]).toEqual({ kind: "time", availability: "unavailable" });
    const malformed = structuredClone(plan);
    malformed.interactions!.push(malformed.interactions![0]!);
    expect(evidencePlanForViewer(malformed)).toBeNull();
  });
});
