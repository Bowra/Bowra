import { guidedReadingFixture } from "./_chart-guided-reading-fixture";
import type { EvidencePlanV1 } from "../lib/chart/presentation-plan";

export function annotationsFixture(withNote = false) {
  const section = guidedReadingFixture(withNote);
  const plan = section.spec.presentation_plan as EvidencePlanV1;
  const base = { binding_id: "time.fixture", measure_key: "revenue", observation_keys: ["month"], availability: "available" as const };
  plan.annotations = [
    { ...base, id: "observed", view_id: "relationship", kind: "point", tuple: ["2026-01"], encoding: "line", label: "Observed revenue" },
    {
      ...base,
      id: "period",
      view_id: "relationship",
      kind: "interval",
      lower: "2026-01",
      upper: "2026-06",
      encoding: "line",
      label: "Supplied period",
    },
    { ...base, id: "missing", view_id: "detail", kind: "point", tuple: ["2026-03"], encoding: "table", label: "Missing observation" },
  ];
  plan.guide_steps = [
    { title: "Inspect the observed point", view_id: "relationship", annotation_id: "observed" },
    { title: "Read the supplied period", view_id: "relationship", annotation_id: "period" },
    { title: "Inspect the missing value", view_id: "detail", annotation_id: "missing" },
  ];
  plan.annotations.forEach((_, index) => {
    plan.text_dependencies[`/presentation_plan/annotations/${index}/label`] = { dataset: true, note_ids: withNote ? ["methodology"] : [] };
  });
  return section;
}
