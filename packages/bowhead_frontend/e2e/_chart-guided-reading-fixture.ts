import { timeWindowFixture } from "./_chart-time-window-fixture";
import type { ReportSection } from "../lib/types";
import type { EvidencePlanV1 } from "../lib/chart/presentation-plan";

export function guidedReadingFixture(withNote = false): Extract<ReportSection, { kind: "chart" }> {
  const section = timeWindowFixture();
  const plan = section.spec.presentation_plan as EvidencePlanV1;
  plan.interactions!.push({ preset: "guided_steps" });
  plan.guide_steps = [
    { title: "Read the overall trend", view_id: "relationship" },
    { title: "Compare the full period", view_id: "comparison" },
    { title: "Inspect the original values", view_id: "detail" },
  ];
  plan.guide_steps.forEach((_, index) => { plan.text_dependencies[`/presentation_plan/guide_steps/${index}/title`] = { note_ids: [], dataset: true }; });
  if (withNote) {
    plan.evidence_notes = [{ id: "methodology", measure_keys: ["revenue"], source_ref: { id: "methodology.fixture", access_state: "linked_accessible" }, availability: "available", excerpt: "Revenue includes returns.", verified_content_digest: "c".repeat(64) }];
    Object.values(plan.text_dependencies).forEach((dependency) => { dependency.note_ids = ["methodology"]; });
  }
  return section;
}
