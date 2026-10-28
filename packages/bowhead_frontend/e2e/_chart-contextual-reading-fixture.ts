import { annotationsFixture } from "./_chart-annotations-fixture";
import { preparedFramesFixture } from "./_chart-prepared-frames-fixture";
import type { EvidencePlanV1 } from "../lib/chart/presentation-plan";
import type { FrameBinding } from "../lib/chart/prepared-frames";

export function contextualEvidenceFixture() {
  const section = annotationsFixture(true), plan = section.spec.presentation_plan as EvidencePlanV1;
  plan.version = 2;
  const statement = { role: "coverage", time_key: "month", measure_keys: ["revenue"], text: "Partial reporting period", lower: "2026-01", upper: "2026-06" };
  plan.evidence_notes[0].excerpt = JSON.stringify(statement);
  const alias = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
  plan.evidence_notes[0].source_ref = { id: alias, source_alias: alias, access_state: "linked_accessible", safe_label: "Reporting scope", safe_preview_url: "/api/v1/report-artifacts/note/preview", safe_url: "/api/v1/report-artifacts/note/download" };
  plan.annotations = plan.annotations!.filter((item) => item.id === "period").map((item) => ({ ...item, label: statement.text, note_ids: ["methodology"], role: "coverage" as const }));
  plan.guide_steps = [{ title: "Read the reporting scope", view_id: "relationship", annotation_id: "period" }];
  plan.text_dependencies = { ...plan.text_dependencies, "/presentation_plan/guide_steps/0/title": { note_ids: ["methodology"], dataset: true }, "/title": { note_ids: ["methodology"], dataset: true }, "/presentation_plan/annotations/0/label": { note_ids: ["methodology"], dataset: true } };
  return section;
}

export function trendTableFixture() {
  const section = preparedFramesFixture(), plan = section.spec.presentation_plan as EvidencePlanV1;
  const { availability: _availability, ...binding } = plan.bindings!["frame.fixture"] as FrameBinding;
  binding.eligible_views = ["detail"]; binding.axis_domains = { detail: null }; binding.coverage.views = { detail: { retained_rows: section.spec.rows!.length } };
  delete plan.interactions; delete plan.bindings; delete plan.view_bindings;
  plan.version = 2;
  plan.table_trends = [{ view_id: "detail", binding_id: "frame.fixture", measure_key: "revenue", binding, availability: "available" }];
  section.spec.layout = { type: "stack", children: [{ view: "relationship" }, { view: "detail" }] };
  return section;
}
