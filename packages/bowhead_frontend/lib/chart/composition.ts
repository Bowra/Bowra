import type { ChartLayout, ChartSpec } from "./types";
import { checkKindShape, checkKindUnits } from "./kind-shape";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import { evidencePlanForViewer } from "./presentation-plan";
import { observationViewEligible } from "./linked-focus";
import { sharedScaleAcceptsEdit } from "./shared-scales";

export type ChartEditTarget = { scope: "root" } | { scope: "view"; viewId: string };
export type EditableChartPatch = Partial<Pick<ChartSpec,
  "kind" | "title" | "subtitle" | "show_legend" | "show_data_labels" | "percent_stack" | "layout"
>>;

/** Replace only the arrangement, retaining the current authored reading order. */
export function chartLayoutPreset(spec: ChartSpec, type: "stack" | "grid2" | "split"): ChartLayout {
  if (!validChartComposition(spec) || (type === "split" && spec.views!.length !== 2))
    throw new Error("This figure cannot use the chosen layout");
  const leaves = (node: ChartLayout): ChartLayout[] => "view" in node ? [{ view: node.view }] : node.children.flatMap(leaves);
  return { type, children: leaves(spec.layout!) };
}

/** Independent dataset browsing survives note-only redaction and unrelated prose changes. */
export function chartBrowsingIdentity(spec: ChartSpec): string {
  const native = (chart: ChartSpec) => Object.fromEntries(Object.entries(chart).filter(([key]) => !["title", "subtitle", "source_meta", "presentation_plan", "views"].includes(key)));
  const plan = evidencePlanForViewer(spec.presentation_plan);
  return JSON.stringify([
    native(spec), spec.views?.map((view) => ({ id: view.id, spec: native(view.spec) })),
    plan?.analytical_views, plan?.dataset, plan?.bindings, plan?.view_bindings, plan?.interactions, plan?.scale_groups, plan?.guide_steps?.map((step) => [step.view_id, step.annotation_id]),
  ]);
}

/** Keep one Canvas root and preserve its siblings, layout and provenance for a targeted native edit. */
export function applyChartEdit(spec: ChartSpec, target: ChartEditTarget, patch: EditableChartPatch): ChartSpec {
  if ("presentation_plan" in spec && !evidencePlanForViewer(spec.presentation_plan)) throw new Error("This chart expression requires a supported plan editor");
  const plan = evidencePlanForViewer(spec.presentation_plan);
  if (plan && ("title" in patch || "subtitle" in patch) && (plan.dataset.availability !== "available" || plan.evidence_notes.some((note) => note.availability !== "available")))
    throw new Error("The original chart notes are unavailable; only display settings can be edited");
  const fields = ["kind", "title", "subtitle", "show_legend", "show_data_labels", "percent_stack", "layout"];
  if (Object.keys(patch).some((key) => !fields.includes(key))) throw new Error("Unsupported chart edit field");
  if (("title" in patch && typeof patch.title !== "string") ||
      (patch.subtitle !== undefined && typeof patch.subtitle !== "string") ||
      ["show_legend", "show_data_labels", "percent_stack"].some((key) =>
        patch[key as keyof EditableChartPatch] !== undefined && typeof patch[key as keyof EditableChartPatch] !== "boolean"))
    throw new Error("Invalid chart edit value");
  const child = target.scope === "root" ? spec : spec.views?.find((view) => view.id === target.viewId)?.spec;
  if (!child || (target.scope === "view" && !validChartComposition(spec))) throw new Error("Chart view is unavailable");
  const changed = { ...child, ...patch };
  if ("layout" in patch && (target.scope !== "root" || !validChartComposition(changed)))
    throw new Error("The layout must retain every view exactly once");
  if (target.scope === "view" && plan?.analytical_views?.some((item) => item.view_id === target.viewId) && changed.kind !== child.kind)
    throw new Error("Analytical bounds and roles require the saved native encoding");
  if (target.scope === "view" && plan?.table_trends?.some((item) => item.view_id === target.viewId) && changed.kind !== child.kind)
    throw new Error("Table trends require the saved native Table encoding");
  if (target.scope === "view" && plan?.annotations?.some((item) => item.view_id === target.viewId) && changed.kind !== child.kind)
    throw new Error("Observed annotations require the saved native encoding");
  if (target.scope === "view" && plan?.scale_groups && !sharedScaleAcceptsEdit(plan.scale_groups, target.viewId, changed))
    throw new Error("The shared numeric scale requires the saved chart encoding and percentage shape");
  const binding = target.scope === "view" ? plan?.bindings?.[plan.view_bindings?.[target.viewId] ?? ""] : undefined;
  if (binding?.availability === "available" && binding.kind === "frame" && changed.kind !== child.kind)
    throw new Error("Prepared frames require the saved scatter or Table encoding");
  if (target.scope === "view" && binding?.availability === "available" && binding.kind !== "frame" && !observationViewEligible(changed, binding))
    throw new Error("The linked view requires a supported observation-preserving chart type");
  if (patch.kind && patch.kind !== child.kind) {
    const normalized = normalizeChartForRenderingV1(changed).spec;
    if (checkKindShape(normalized, patch.kind) || checkKindUnits(changed, patch.kind).length)
      throw new Error("These measures cannot use the chosen chart type");
  }
  return target.scope === "root" ? changed : {
    ...spec,
    views: spec.views!.map((view) => view.id === target.viewId ? { ...view, spec: changed } : view),
  };
}

/** Check persisted layouts before recursion, including manually edited payloads. */
export function validChartComposition(spec: ChartSpec): boolean {
  if (!Array.isArray(spec.views) || spec.views.length < (spec.presentation_plan && evidencePlanForViewer(spec.presentation_plan)?.analytical_views ? 1 : 2) || spec.views.length > 6) return false;
  const ids = spec.views.map((view) => view?.id);
  if (ids.some((id) => typeof id !== "string") || new Set(ids).size !== ids.length) return false;
  if (
    spec.views.some(
      (view) =>
        view.kind !== "chart" || !view.spec || view.spec.views || !Array.isArray(view.spec.columns),
    )
  )
    return false;
  const seen = new Set<string>();
  function check(node: ChartLayout | undefined, depth: number): boolean {
    if (!node || typeof node !== "object" || depth > 3) return false;
    if ("view" in node) {
      if (Object.keys(node).length !== 1 || !ids.includes(node.view) || seen.has(node.view)) return false;
      seen.add(node.view);
      return true;
    }
    if (Object.keys(node).sort().join() !== "children,type" || !["stack", "split", "grid2"].includes(node.type) || !Array.isArray(node.children))
      return false;
    if (
      node.children.length < 2 ||
      node.children.length > 6 ||
      (node.type === "split" && node.children.length !== 2)
    )
      return false;
    return node.children.every((child) => check(child, depth + 1));
  }
  return check(spec.layout, 1) && seen.size === ids.length;
}
