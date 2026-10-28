import type { ReportSection } from "../lib/types";
import type { ChartSpec } from "../lib/chart/types";
import type { EvidencePlanV1 } from "../lib/chart/presentation-plan";

export function preparedFramesFixture(): Extract<ReportSection, { kind: "chart" }> {
  const columns: ChartSpec["columns"] = [
    { key: "month", header: "Month", type: "date" },
    { key: "region", header: "Region", type: "categorical" },
    { key: "revenue", header: "Revenue", type: "numeric", unit: "USD" },
    { key: "people", header: "People", type: "numeric", unit: "people" },
  ];
  const rows = [
    { month: "2026-03", region: "A", revenue: 12.123456, people: 5 },
    { month: "2026-01", region: "B", revenue: -5, people: 2 },
    { month: "2026-01", region: "A", revenue: null, people: 3 },
    { month: "2026-02", region: "B", revenue: 0.000012345, people: 4 },
    { month: "2026-02", region: "A", revenue: 2, people: 7 },
  ];
  const child: ChartSpec = { kind: "scatter", title: "Revenue and people", data_artifact_id: "art_frames", columns, rows, row_count: rows.length };
  const ids = ["relationship", "detail"];
  const plan: EvidencePlanV1 = {
    version: 1,
    dataset: { artifact_id: "frames.fixture", availability: "available", content_digest: "a".repeat(64), schema_digest: "b".repeat(64) },
    evidence_notes: [], text_dependencies: { "/title": { note_ids: [], dataset: true }, "/views/0/spec/title": { note_ids: [], dataset: true }, "/views/1/spec/title": { note_ids: [], dataset: true } },
    view_bindings: Object.fromEntries(ids.map((id) => [id, "frame.fixture"])),
    interactions: [{ preset: "frame_player", view_ids: ids, binding_id: "frame.fixture", static_frame: "2026-02" }],
    bindings: { "frame.fixture": { kind: "frame", availability: "available", dimension_keys: ["region"], observation_keys: ["month", "region"], measure_keys: ["revenue", "people"], time_key: "month", entity_keys: ["region"], order: "date", frame_keys: ["2026-01", "2026-02", "2026-03"], entity_values: ["A", "B"], eligible_views: ids, axis_domains: { relationship: { encoding: "scatter", domains: { x: [-6, 14], y: [1, 8] } }, detail: null }, coverage: { artifact_rows: { available: rows.length, total: rows.length }, views: Object.fromEntries(ids.map((id) => [id, { retained_rows: rows.length }])) } } },
  };
  return { id: "sec_frames", kind: "chart", rev: 1, spec: { ...child, title: "Prepared monthly relationships", views: [{ id: ids[0], kind: "chart", spec: child }, { id: ids[1], kind: "chart", spec: { ...child, kind: "table", title: "Original frame observations" } }], layout: { type: "split", children: ids.map((view) => ({ view })) }, presentation_plan: plan } };
}
