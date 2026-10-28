import { isNetworkFailure } from "@/lib/transport-error";
/**
 * Failed plan-card actions → user-facing copy (`plan_card.action_errors.*` keys).
 *
 * Choose copy solely by status and error code, never show raw API text: backend detail is developer-facing English
 * (「revise failed: …」「unknown agents in replanned steps: …」); callers expose status and request ID
 * separately via「复制诊断」.
 */

export type PlanAction =
  | "skip"
  | "revise_preview"
  | "revise_confirm"
  | "replan_preview"
  | "replan_confirm"
  | "amend"
  | "edit";

export type PlanActionErrorKey =
  | "model_unconfigured"
  | "preview_conflict"
  | "agent_choice_required"
  | "agent_choice_stale"
  | "network"
  | "too_frequent"
  | "busy"
  | "stale"
  | "not_found"
  | "revise_unusable"
  | "replan_unusable"
  | "preview_outdated"
  | "amend_unusable"
  | "edit_invalid"
  | "skip_failed"
  | "revise_failed"
  | "replan_failed"
  | "amend_failed"
  | "edit_failed";

const FAILED: Record<PlanAction, PlanActionErrorKey> = {
  skip: "skip_failed",
  revise_preview: "revise_failed",
  revise_confirm: "revise_failed",
  replan_preview: "replan_failed",
  replan_confirm: "replan_failed",
  amend: "amend_failed",
  edit: "edit_failed",
};

const UNPROCESSABLE: Record<PlanAction, PlanActionErrorKey> = {
  skip: "skip_failed",
  revise_preview: "revise_unusable",
  revise_confirm: "preview_outdated",
  replan_preview: "replan_unusable",
  replan_confirm: "preview_outdated",
  amend: "amend_unusable",
  edit: "edit_invalid",
};

export function planActionErrorKey(error: unknown, action: PlanAction): PlanActionErrorKey {
  const record = error && typeof error === "object" ? (error as { status?: unknown; code?: unknown }) : {};
  const status = typeof record.status === "number" ? record.status : undefined;
  // Team has no model: retries and refresh cannot help; contact an administrator.
  if (record.code === "team_model_unconfigured") return "model_unconfigured";
  // The agent selected for a step while editing: either multiple candidates require user choice, or the selected agent changed.
  if (record.code === "plan_agent_choice_required") return "agent_choice_required";
  if (record.code === "plan_agent_choice_stale") return "agent_choice_stale";
  if (status === undefined) {
    // Locally detected a plan changed elsewhere (returned version does not match).
    if (record.code === "plan_not_approvable") return "stale";
    // Only recognized browser transport failures establish a connection problem.
    return isNetworkFailure(error) ? "network" : FAILED[action];
  }
  if (status === 429) return "too_frequent";
  if (status === 409) {
    if (record.code !== "plan_busy_or_conflict") return "stale";
    // Confirmation conflict: preview holds an old version, so reconfirming unchanged will conflict again; discard and restart the preview.
    return action === "revise_confirm" || action === "replan_confirm" ? "preview_conflict" : "busy";
  }
  if (status === 404) return "not_found";
  if (status === 422) return UNPROCESSABLE[action];
  return FAILED[action];
}
