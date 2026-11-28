/**
 * Owner draft trial (docs/sprints/agent-page-redesign-2026-11-24/trial-tech-design.md §6–§7): the rules the
 * Agent page and the chat share. Pure, so each branch is a table row in its test; the components bring the
 * requests and the words.
 *
 * - Which release candidate a trial message names: reuse the current one when it covers the saved draft (or is
 *   in review, or is under test, decision TD1), otherwise prepare one from the saved draft (`replace_candidate`
 *   for a stale prepared one).
 * - The save rule before trying (or sending): only 工作说明 unsaved → save without asking; other fields unsaved →
 *   ask, naming them. Shared with AI 优化, which uses the same rule with its own button words.
 * - The trial reference in the input document and the per-answer draft label.
 */

import type { AgentVersionWorkbench } from "@/lib/agents-api";
import { parseInputDocument, type InputDocument, type ReferenceNode } from "@/lib/composer-input-document";
import { randomUuid } from "@/lib/random-id";

/** The frozen draft a trial message runs, as the composer sends it. */
export interface TrialReference {
  agentId: string;
  /** The Agent's name shown in the band and the locked chip. */
  label: string;
  candidateId: string;
  /** The candidate's revision (`AgentResourceRef.version`). */
  revisionRef: string;
}

/** Which frozen draft answered (the `trial` member of an Agent attribution). */
export interface TrialAttribution {
  candidateId: string;
  candidateKind: "draft" | "submitted";
  frozenAt: string;
}

const ACTIVE_TEST = new Set(["queued", "running", "finalizing"]);

export type TrialCandidatePlan =
  /** Use the current candidate. `underTest`: a test runs on it while the draft moved on (TD1). */
  | { kind: "reuse"; candidateId: string; revisionRef: string; candidateKind: "draft" | "submitted"; underTest: boolean }
  /** Freeze the saved draft; `replace` withdraws the stale prepared candidate in the same request. */
  | { kind: "prepare"; replace: { candidate_id: string; candidate_row_version: number; candidate_hash: string } | null }
  /** The saved draft equals the released version: nothing to try. */
  | { kind: "nothing" }
  /** The draft was made on an older release and has to be updated before it can be frozen. */
  | { kind: "rebase" };

export function trialCandidatePlan(workbench: AgentVersionWorkbench): TrialCandidatePlan {
  const candidate = workbench.candidate_identity;
  // In review the trial runs the submitted version, which is what the reviewer sees; edits made after the
  // submission need a withdrawal before they can be tried.
  if (candidate?.state === "pending") {
    return { kind: "reuse", candidateId: candidate.candidate_id, revisionRef: candidate.revision_id, candidateKind: "submitted", underTest: false };
  }
  if (candidate?.state === "prepared") {
    if (!workbench.draft_ahead_of_candidate) {
      return { kind: "reuse", candidateId: candidate.candidate_id, revisionRef: candidate.revision_id, candidateKind: "draft", underTest: false };
    }
    // TD1: replacing the candidate would stop its running test, so the trial keeps using it.
    if (workbench.active_execution_summary && ACTIVE_TEST.has(workbench.active_execution_summary.status)) {
      return { kind: "reuse", candidateId: candidate.candidate_id, revisionRef: candidate.revision_id, candidateKind: "draft", underTest: true };
    }
    if (workbench.draft_rebase_state === "required") return { kind: "rebase" };
    return {
      kind: "prepare",
      replace: { candidate_id: candidate.candidate_id, candidate_row_version: candidate.row_version, candidate_hash: candidate.candidate_hash },
    };
  }
  if (workbench.draft_rebase_state === "required") return { kind: "rebase" };
  if (!workbench.next_draft_identity) return { kind: "nothing" };
  return { kind: "prepare", replace: null };
}

/**
 * Configure fields grouped as the Owner names them. `instructions` is 工作说明: the only field saved without
 * asking, because editing it is what a trial is for.
 */
export type DraftFieldKey = "instructions" | "name" | "description" | "listing" | "resources" | "audience" | "advanced";

export const DRAFT_FIELD_ORDER: readonly DraftFieldKey[] = ["instructions", "name", "description", "listing", "resources", "audience", "advanced"];

/** What happens to unsaved edits before trying (or before a trial message). */
export function draftSaveStep(fields: readonly DraftFieldKey[]): "none" | "silent" | "confirm" {
  if (!fields.length) return "none";
  return fields.every((field) => field === "instructions") ? "silent" : "confirm";
}

/** The fields the confirmation names: everything unsaved except 工作说明, which is saved anyway. */
export function confirmedDraftFields(fields: readonly DraftFieldKey[]): DraftFieldKey[] {
  return DRAFT_FIELD_ORDER.filter((field) => field !== "instructions" && fields.includes(field));
}

export type DraftSaveChoice = "save" | "saved_only" | "cancel";

export type TrialStartResult =
  | { kind: "ready"; candidateId: string; revisionRef: string; candidateKind: "draft" | "submitted"; underTest: boolean }
  | { kind: "cancelled" }
  /** The save failed; the editor shows why. Nothing was frozen or sent. */
  | { kind: "save_failed" }
  /** Unsaved edits need saving, but the page already knows they can't be saved: nothing was asked, saved or frozen. */
  | { kind: "save_blocked" }
  | { kind: "nothing_to_try" }
  | { kind: "rebase_required" }
  /** Freezing the draft failed: `cause` names the failing check. */
  | { kind: "prepare_failed"; cause: unknown };

export interface TrialStartDeps {
  dirtyFields: readonly DraftFieldKey[];
  /** The saved draft equals the released version, so "use only the saved draft" has nothing to try. */
  savedDraftEqualsRelease: boolean;
  /** The page already knows the unsaved edits can't be saved (the bar's 「还不能保存」); omitted means not known. */
  saveBlocked?: boolean;
  /** `withInstructions`: 工作说明 is unsaved too, and is saved together with the fields named. */
  confirmSave: (fields: DraftFieldKey[], options: { offerSavedOnly: boolean; withInstructions: boolean }) => Promise<DraftSaveChoice>;
  save: () => Promise<boolean>;
  /** Read after saving, so the decision sees the new draft. */
  loadWorkbench: () => Promise<AgentVersionWorkbench>;
  loadWorkingCopy: () => Promise<{ id: string; row_version: number } | null>;
  prepare: (request: {
    working_copy_id: string;
    working_copy_row_version: number;
    /**
     * The evaluation Policy the candidate is frozen with, from the workbench read just before (null before the
     * Agent's first freeze or test). Any other value is refused with `agent_eval_policy_version_conflict`.
     */
    expected_eval_policy_revision_id: string | null;
    replace_candidate?: { candidate_id: string; candidate_row_version: number; candidate_hash: string };
  }) => Promise<{ candidate_id: string | null; revision_id: string }>;
}

/**
 * The start flow (and the check before each trial message, C5): save per the rule, then reuse or freeze the
 * candidate. Shared by the release bar, 新的试用 and the trial chat.
 */
export async function startDraftTrial(deps: TrialStartDeps): Promise<TrialStartResult> {
  const step = draftSaveStep(deps.dirtyFields);
  // Offering to save edits that can't be saved only ends in a failed save; the caller names what blocks it instead.
  if (step !== "none" && deps.saveBlocked) return { kind: "save_blocked" };
  if (step === "silent") {
    if (!await deps.save()) return { kind: "save_failed" };
  } else if (step === "confirm") {
    const choice = await deps.confirmSave(confirmedDraftFields(deps.dirtyFields), {
      offerSavedOnly: !deps.savedDraftEqualsRelease,
      withInstructions: deps.dirtyFields.includes("instructions"),
    });
    if (choice === "cancel") return { kind: "cancelled" };
    if (choice === "save" && !await deps.save()) return { kind: "save_failed" };
  }
  const workbench = await deps.loadWorkbench();
  const plan = trialCandidatePlan(workbench);
  if (plan.kind === "reuse") return { kind: "ready", candidateId: plan.candidateId, revisionRef: plan.revisionRef, candidateKind: plan.candidateKind, underTest: plan.underTest };
  if (plan.kind === "nothing") return { kind: "nothing_to_try" };
  if (plan.kind === "rebase") return { kind: "rebase_required" };
  const workingCopy = await deps.loadWorkingCopy();
  if (!workingCopy) return { kind: "nothing_to_try" };
  try {
    const prepared = await deps.prepare({
      working_copy_id: workingCopy.id,
      working_copy_row_version: workingCopy.row_version,
      expected_eval_policy_revision_id: workbench.current_eval_policy_revision_id ?? null,
      ...(plan.replace ? { replace_candidate: plan.replace } : {}),
    });
    if (!prepared.candidate_id) return { kind: "prepare_failed", cause: new Error("agent_trial_candidate_unavailable") };
    return { kind: "ready", candidateId: prepared.candidate_id, revisionRef: prepared.revision_id, candidateKind: "draft", underTest: false };
  } catch (cause) {
    return { kind: "prepare_failed", cause };
  }
}

/** The trial Agent reference: a Team Agent naming the frozen candidate, without assignment or release. */
export function trialReferenceNode(reference: TrialReference): ReferenceNode {
  return {
    type: "reference",
    node_id: randomUuid(),
    resource: { kind: "agent", source_kind: "team", id: reference.agentId, version: reference.revisionRef, trial_candidate_id: reference.candidateId },
    label: reference.label,
    parameters: {},
  };
}

/** "@Agent <question>": the only shape a trial message takes (the server refuses any other reference). */
export function trialInputDocument(reference: TrialReference, text: string): InputDocument {
  return parseInputDocument({
    schema_version: 1,
    nodes: [trialReferenceNode(reference), { type: "text", text: text.startsWith(" ") ? text : ` ${text}` }],
  });
}

/** Whether a document is a trial message for this reference (one Agent node naming the candidate). */
export function isTrialDocument(document: InputDocument, reference: Pick<TrialReference, "agentId" | "candidateId">): boolean {
  const references = document.nodes.filter((node) => node.type === "reference");
  if (references.length !== 1) return false;
  const resource = references[0].resource;
  return resource.kind === "agent" && resource.id === reference.agentId && "trial_candidate_id" in resource && resource.trial_candidate_id === reference.candidateId;
}

/** Parse the `trial` member of an attribution payload; null outside a trial or when malformed. */
export function readTrialAttribution(value: unknown): TrialAttribution | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  const kind = item.candidate_kind;
  if (typeof item.candidate_id !== "string" || !item.candidate_id || (kind !== "draft" && kind !== "submitted") || typeof item.frozen_at !== "string") return null;
  return { candidateId: item.candidate_id, candidateKind: kind, frozenAt: item.frozen_at };
}

/** A divider goes between two answers whose drafts differ (C5); an answer without attribution never starts one. */
export function trialDraftChanged(previous: TrialAttribution | null, current: TrialAttribution | null): boolean {
  return Boolean(previous && current && previous.candidateId !== current.candidateId);
}

/** Server timestamps without a zone are UTC (SQLite `datetime()`); ISO strings keep theirs. */
export function parseTrialTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const naive = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(value);
  const date = new Date(naive ? `${value.replace(" ", "T")}Z` : value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export type RecentTrial = NonNullable<AgentVersionWorkbench["recent_trials"]>[number];

/** The released version that ended this trial session, if its latest draft was released. */
export function trialEndedRelease(workbench: Pick<AgentVersionWorkbench, "recent_trials"> | null, sessionId: string | null): number | null {
  if (!workbench || !sessionId) return null;
  const row = workbench.recent_trials?.find((item) => item.session_id === sessionId);
  return row?.released_as?.release_sequence ?? null;
}
