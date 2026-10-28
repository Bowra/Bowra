import { linkedFocusFixture } from "./_chart-linked-focus-fixture";
import type { EvidencePlanV1 } from "../lib/chart/presentation-plan";

export function timeWindowFixture() {
  const fixture = linkedFocusFixture();
  const rows = [
    { month: "2026-04", revenue: 40, cost: 25 },
    { month: "2026-01", revenue: 52.12345, cost: 30.16789 },
    { month: "2026-06", revenue: 80, cost: 55 },
    { month: "2026-03", revenue: null, cost: 9 },
    { month: "2026-02", revenue: -5, cost: 12 },
    { month: "2026-05", revenue: 0.000012345, cost: 45 },
  ];
  const columns = [{ key: "month", header: "Month", type: "date" as const }, ...fixture.spec.columns.slice(1)];
  const bid = "time.fixture";
  const plan = fixture.spec.presentation_plan as EvidencePlanV1;
  plan.view_bindings = { relationship: bid, comparison: bid, detail: bid };
  plan.interactions = [
    { preset: "time_window", view_ids: ["relationship", "detail"], binding_id: bid },
    { preset: "linked_focus", from_view: "detail", to_views: ["relationship", "comparison"], binding_id: bid },
  ];
  plan.bindings = { [bid]: { kind: "time", availability: "available", time_key: "month", order: "date", dimension_keys: ["month"], observation_keys: ["month"], measure_keys: ["revenue", "cost"], eligible_views: ["relationship", "comparison", "detail"], coverage: { artifact_rows: { available: rows.length, total: rows.length }, views: Object.fromEntries(["relationship", "comparison", "detail"].map((id) => [id, { retained_rows: rows.length }])) } } };
  fixture.spec = { ...fixture.spec, columns, rows, row_count: rows.length, title: "Saved monthly observations", data_artifact_id: "art_months", views: fixture.spec.views!.map((view) => ({ ...view, spec: { ...view.spec, columns: view.id === "comparison" ? columns.slice(0, 2) : columns, rows, row_count: rows.length, kind: view.id === "relationship" ? "line" : view.spec.kind, title: view.id === "relationship" ? "Revenue and cost over time" : view.id === "comparison" ? "Revenue over the full saved period" : "Monthly observations" } })) };
  return fixture;
}
