export interface GuideStep { title: string; view_id: string; annotation_id?: string }
export interface GuideInteraction { preset: "guided_steps" }
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);

export function validGuides(plan: Record<string, unknown>): boolean {
  if (plan.interactions !== undefined && (!Array.isArray(plan.interactions) || !plan.interactions.length || plan.interactions.length > 3)) return false;
  const guides = (Array.isArray(plan.interactions) ? plan.interactions : []).filter((item) => object(item) && item.preset === "guided_steps");
  if (!guides.length && !("guide_steps" in plan)) return true;
  if (guides.length !== 1 || Object.keys(guides[0]).join() !== "preset" || !Array.isArray(plan.guide_steps) || !plan.guide_steps.length || plan.guide_steps.length > 4) return false;
  const unavailable = (object(plan.dataset) && plan.dataset.availability === "unavailable") || (Array.isArray(plan.evidence_notes) && plan.evidence_notes.some((note) => object(note) && note.availability === "unavailable"));
  return plan.guide_steps.every((step) => object(step) && ["title,view_id", "annotation_id,title,view_id"].includes(Object.keys(step).sort().join()) && typeof step.title === "string" && Array.from(step.title).length <= 80 && (unavailable || !!step.title.trim()) && typeof step.view_id === "string" && !!step.view_id && (step.annotation_id === undefined || (Array.isArray(plan.annotations) && plan.annotations.some((item) => object(item) && item.id === step.annotation_id && item.view_id === step.view_id))));
}
