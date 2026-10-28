import { apiJson } from "@/lib/api-client";
import type { WorkspaceI18nLifecycle } from "@/components/settings/workspace-i18n";
import { performLiteAction, type LiteActionReceipt, type LiteServerState } from "@/lib/config-copilot-lite";
import type { SettingsCanvasTarget } from "@/lib/settings-canvas-target";
import type { ConfigLiteProposePayload } from "@/lib/types";

export interface WorkspaceSettingsLifecycle extends WorkspaceI18nLifecycle {
  revision?: number;
}

export function settingsFormId(target: SettingsCanvasTarget) {
  return target.page === "space_general" ? "workspace_metadata" : "workspace_i18n";
}

export interface WorkspaceSettingsTargetProjection {
  form_id: "workspace_i18n" | "workspace_metadata";
  user_id: string;
  team_id: string;
  workspace_id: string;
  edit_generation: number;
}

export class WorkspaceSettingsStageError extends Error {
  constructor(public readonly code: "page_not_ready" | "target_mismatch" | "local_changes" | "baseline_changed" | "proposal_not_pending") {
    super(code);
  }
}

function assertReady(target: SettingsCanvasTarget, page: WorkspaceSettingsLifecycle): void {
  if (!page.savedValues || !Number.isSafeInteger(page.editGeneration) || page.editGeneration < 0)
    throw new WorkspaceSettingsStageError("page_not_ready");
  if (page.workspaceId !== target.workspaceId) throw new WorkspaceSettingsStageError("target_mismatch");
  if (page.pendingWrites || page.unconfirmedFields.length) throw new WorkspaceSettingsStageError("local_changes");
}

/** Copy once at send time; this is a restriction that the server must authorize. */
export function workspaceSettingsClientInfo(target: SettingsCanvasTarget, page: WorkspaceSettingsLifecycle): Record<string, string> {
  assertReady(target, page);
  return {
    configuration_settings_target: JSON.stringify({
      form_id: settingsFormId(target),
      user_id: target.userId,
      team_id: target.teamId,
      workspace_id: target.workspaceId,
      edit_generation: page.editGeneration,
      saved_ready: true,
      pending_writes: 0,
      unconfirmed_fields: [],
    }),
  };
}

interface WorkspaceSettingsReview extends LiteActionReceipt {
  turn_id: string;
  rows: ConfigLiteProposePayload["rows"];
  notes: ConfigLiteProposePayload["notes"];
  settings_target: WorkspaceSettingsTargetProjection | null;
  workspace_name: string | null;
}

export interface StagedWorkspaceSettingsSuggestion {
  target: Readonly<SettingsCanvasTarget>;
  editGeneration: number;
  proposal: ConfigLiteProposePayload;
  status: LiteServerState;
}

/** Recheck newer local input immediately before using the existing guarded write service. */
export async function performWorkspaceSettingsSuggestionAction(
  staged: StagedWorkspaceSettingsSuggestion,
  readLifecycle: () => WorkspaceSettingsLifecycle | null,
  op: "apply" | "undo",
): Promise<LiteActionReceipt | null> {
  if (staged.status === "applied" && op !== "undo") throw new WorkspaceSettingsStageError("proposal_not_pending");
  const page = readLifecycle();
  if (!page) throw new WorkspaceSettingsStageError("page_not_ready");
  assertReady(staged.target, page);
  if (page.editGeneration !== staged.editGeneration) throw new WorkspaceSettingsStageError("local_changes");
  for (const row of staged.proposal.rows) {
    if (staged.target.page === "space_general" && (op !== "apply" || row.baseline_revision !== page.revision))
      throw new WorkspaceSettingsStageError("baseline_changed");
    const matches =
      op === "undo"
        ? page.savedValues![row.field] === row.to
        : row.from_unset
          ? !Object.prototype.hasOwnProperty.call(page.savedValues!, row.field)
          : page.savedValues![row.field] === row.from;
    if (!matches) throw new WorkspaceSettingsStageError("baseline_changed");
  }
  // A confirmed server write remains real after disposal, but its old controller cannot refresh a reopened page.
  return performLiteAction(staged.proposal, op, (receipt) => readLifecycle() !== null
    && receipt.owner_user_id === staged.target.userId && receipt.team_id === staged.target.teamId
    && receipt.workspace_id === staged.target.workspaceId);
}

/** GET only. Preview is separate data; never invoke the page's manual update callback. */
export async function stageWorkspaceSettingsSuggestion(
  proposalId: string,
  target: SettingsCanvasTarget,
  readLifecycle: () => WorkspaceSettingsLifecycle | null,
  admission: "original" | "saved_review" = "original",
): Promise<StagedWorkspaceSettingsSuggestion> {
  const originalTarget = { ...target };
  const before = readLifecycle();
  if (!before) throw new WorkspaceSettingsStageError("page_not_ready");
  assertReady(originalTarget, before);
  const generation = before.editGeneration;
  const review = await apiJson<WorkspaceSettingsReview>(`/api/v1/config-proposals/${encodeURIComponent(proposalId)}/settings-review`);
  const page = readLifecycle();
  if (!page) throw new WorkspaceSettingsStageError("page_not_ready");
  assertReady(originalTarget, page);
  const binding = review.settings_target;
  if (
    review.proposal_id !== proposalId ||
    review.form_id !== settingsFormId(originalTarget) ||
    review.owner_user_id !== originalTarget.userId ||
    review.team_id !== originalTarget.teamId ||
    review.workspace_id !== originalTarget.workspaceId ||
    (binding !== null && (binding.form_id !== settingsFormId(originalTarget) ||
    binding.user_id !== originalTarget.userId ||
    binding.team_id !== originalTarget.teamId ||
    binding.workspace_id !== originalTarget.workspaceId))
  ) {
    throw new WorkspaceSettingsStageError("target_mismatch");
  }
  if (page.editGeneration !== generation || (admission === "original" && binding !== null && binding.edit_generation !== page.editGeneration)) throw new WorkspaceSettingsStageError("local_changes");
  const undoReview = admission === "saved_review" && review.status === "applied" && originalTarget.page === "workspace_i18n";
  if (review.status !== "proposed" && !undoReview) throw new WorkspaceSettingsStageError("proposal_not_pending");
  for (const row of review.rows) {
    if (
      !(originalTarget.page === "space_general" ? ["name", "description"] : ["week_start_day", "default_data_language"]).includes(row.field)
      || (originalTarget.page === "space_general" && row.baseline_revision !== page.revision) ||
      (undoReview ? page.savedValues![row.field] !== row.to
        : row.from_unset ? Object.prototype.hasOwnProperty.call(page.savedValues!, row.field) : page.savedValues![row.field] !== row.from)
    ) {
      throw new WorkspaceSettingsStageError("baseline_changed");
    }
  }
  return {
    target: Object.freeze(originalTarget),
    editGeneration: page.editGeneration,
    status: undoReview ? "applied" : "proposed",
    proposal: {
      proposal_id: review.proposal_id,
      form_id: review.form_id,
      turn_id: review.turn_id,
      team_id: review.team_id,
      workspace_id: review.workspace_id,
      workspace_name: review.workspace_name,
      rows: review.rows,
      notes: review.notes,
      ...(binding ? { settings_target: binding } : {}),
    },
  };
}
