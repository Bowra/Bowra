import { timeWindowFixture } from "./_chart-time-window-fixture";
import type { EvidencePlanV1 } from "../lib/chart/presentation-plan";

export function sharedScaleFixture() {
  const fixture = timeWindowFixture();
  fixture.spec.views = fixture.spec.views!.map((view) => view.id === "comparison" ? { ...view, spec: { ...view.spec, kind: "line", columns: fixture.spec.columns } } : view);
  const plan = fixture.spec.presentation_plan as EvidencePlanV1;
  plan.scale_groups = [{ view_ids: ["relationship", "comparison"], axis: "y", measure_keys: ["revenue", "cost"], encoding: "line", domain: [-9.25, 84.25], scope_digest: "d".repeat(64) }];
  return fixture;
}
