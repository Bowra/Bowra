import { describe, expect, it } from "vitest";
import { guidedReadingFixture } from "../../e2e/_chart-guided-reading-fixture";
import { evidencePlanForViewer } from "./presentation-plan";
import { validGuides } from "./guided-reading";
import { applyChartEdit, chartBrowsingIdentity } from "./composition";
import { computeCanvasVisualInputSha256 } from "../canvas-visual-identity";
import { chartStyleExportThemeFixture } from "@/__tests__/chart-style-export-fixture";

describe("native source-protected reading steps", () => {
  it("accepts guides alongside existing window/focus and preserves them on a sibling edit", () => {
    const spec = guidedReadingFixture(true).spec;
    expect(evidencePlanForViewer(spec.presentation_plan)?.guide_steps).toHaveLength(3);
    const edited = applyChartEdit(spec, { scope: "view", viewId: "detail" }, { title: "Detailed observations" });
    expect(edited.presentation_plan).toBe(spec.presentation_plan);
    expect(edited.views![0]).toBe(spec.views![0]);
  });
  it("rejects scene overrides and counts Unicode code points", () => {
    const plan = structuredClone(guidedReadingFixture().spec.presentation_plan) as Record<string, unknown>;
    const steps = plan.guide_steps as Array<{ title: string; view_id: string; frame?: string }>;
    steps[0].title = "🦋".repeat(80);
    expect(validGuides(plan)).toBe(true);
    steps[0].title += "🦋";
    expect(validGuides(plan)).toBe(false);
    steps[0].title = "Observe";
    steps[0].frame = "2026-01";
    expect(evidencePlanForViewer(plan)).toBeNull();
  });
  it("admits correctly redacted reading titles but cannot enable an unverified dataset", () => {
    const plan = structuredClone(guidedReadingFixture().spec.presentation_plan);
    const saved = evidencePlanForViewer(plan)!;
    saved.dataset = { artifact_id: saved.dataset.artifact_id, availability: "unavailable" };
    saved.bindings = { "time.fixture": { kind: "time", availability: "unavailable" } };
    saved.guide_steps!.forEach((step) => { step.title = ""; });
    expect(evidencePlanForViewer(plan)?.dataset.availability).toBe("unavailable");
  });
  it("includes ordered saved reading text in visual identity", async () => {
    const spec = guidedReadingFixture().spec;
    const args = { section: guidedReadingFixture(), locale: "en-US" as const, theme: chartStyleExportThemeFixture() };
    const before = await computeCanvasVisualInputSha256(args);
    const changed = structuredClone(spec);
    evidencePlanForViewer(changed.presentation_plan)!.guide_steps!.reverse();
    expect(await computeCanvasVisualInputSha256({ ...args, section: { ...args.section, spec: changed } })).not.toBe(before);
  });
  it("keeps dataset browsing through note-only text redaction but invalidates changed facts or targets", () => {
    const spec = guidedReadingFixture(true).spec;
    const redacted = structuredClone(spec);
    redacted.title = "";
    redacted.views!.forEach((view) => { view.spec.title = ""; });
    const plan = evidencePlanForViewer(redacted.presentation_plan)!;
    plan.evidence_notes = [{ id: "methodology", measure_keys: ["revenue"], source_ref: { id: "methodology.fixture", access_state: "linked_restricted" }, availability: "unavailable" }];
    plan.guide_steps!.forEach((step) => { step.title = ""; });
    expect(evidencePlanForViewer(redacted.presentation_plan)?.dataset.availability).toBe("available");
    expect(chartBrowsingIdentity(redacted)).toBe(chartBrowsingIdentity(spec));
    const changed = structuredClone(spec);
    changed.views![0].spec.rows![0].revenue = 999;
    expect(chartBrowsingIdentity(changed)).not.toBe(chartBrowsingIdentity(spec));
    const target = structuredClone(spec);
    evidencePlanForViewer(target.presentation_plan)!.guide_steps![0].view_id = "detail";
    expect(chartBrowsingIdentity(target)).not.toBe(chartBrowsingIdentity(spec));
  });
});
