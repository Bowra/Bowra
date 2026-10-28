import type { ReportSection } from "../lib/types";
import type { ChartSpec } from "../lib/chart/types";
import type { EvidencePlanV1 } from "../lib/chart/presentation-plan";

export function observationAdaptersFixture(): Extract<ReportSection, { kind: "chart" }> {
  const columns: ChartSpec["columns"] = [
    { key: "region", header: "Region", type: "categorical" },
    { key: "month", header: "Month", type: "date" },
    { key: "revenue", header: "Revenue", type: "numeric", unit: "USD" },
    { key: "cost", header: "Cost", type: "numeric", unit: "USD" },
  ];
  const rows = [
    { region: "East", month: "2026-02", revenue: 0, cost: 5 },
    { region: "West", month: "2026-01", revenue: 0.000012345, cost: 0.05 },
    { region: "East", month: "2026-01", revenue: 52.12345, cost: 30.16789 },
    { region: "West", month: "2026-02", revenue: null, cost: -5 },
  ];
  const child: ChartSpec = {
    kind: "line",
    title: "Revenue by region",
    row_count: 4,
    columns: columns.slice(0, 3),
    rows,
    data_artifact_id: "art_observations",
  };
  const ids = ["trend", "relationship", "detail"];
  const plan: EvidencePlanV1 = {
    version: 1,
    dataset: { artifact_id: "data.fixture", availability: "available", content_digest: "a".repeat(64), schema_digest: "b".repeat(64) },
    evidence_notes: [],
    text_dependencies: {},
    view_bindings: Object.fromEntries(ids.map((id) => [id, "time.fixture"])),
    bindings: {
      "time.fixture": {
        kind: "time",
        availability: "available",
        dimension_keys: ["month"],
        observation_keys: ["region", "month"],
        measure_keys: ["revenue", "cost"],
        time_key: "month",
        order: "date",
        eligible_views: ids,
        coverage: { artifact_rows: { available: 4, total: 4 }, views: Object.fromEntries(ids.map((id) => [id, { retained_rows: 4 }])) },
      },
    },
    interactions: [
      { preset: "linked_focus", from_view: "trend", to_views: ["relationship", "detail"], binding_id: "time.fixture" },
      { preset: "linked_focus", from_view: "detail", to_views: ["trend", "relationship"], binding_id: "time.fixture" },
      { preset: "time_window", view_ids: ids, binding_id: "time.fixture" },
    ],
  };
  return {
    id: "sec_observations",
    kind: "chart",
    rev: 1,
    spec: {
      ...child,
      title: "Regional observations",
      presentation_plan: plan,
      views: [
        { id: "trend", kind: "chart", spec: child },
        { id: "relationship", kind: "chart", spec: { ...child, columns, kind: "scatter", title: "Revenue and cost" } },
        { id: "detail", kind: "chart", spec: { ...child, columns, kind: "table", title: "Original observations" } },
      ],
      layout: { type: "stack", children: [{ type: "split", children: [{ view: "trend" }, { view: "relationship" }] }, { view: "detail" }] },
    },
  };
}
