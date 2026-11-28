// @vitest-environment node
import { describe, expect, it, vi } from "vitest";

import type { AgentVersionWorkbench } from "@/lib/agents-api";
import {
  confirmedDraftFields,
  draftSaveStep,
  isTrialDocument,
  parseTrialTime,
  readTrialAttribution,
  startDraftTrial,
  trialCandidatePlan,
  trialDraftChanged,
  trialEndedRelease,
  trialInputDocument,
  type TrialStartDeps,
} from "./agent-trial";

const BASE: AgentVersionWorkbench = {
  schema_version: 1,
  current_eval_policy_revision_id: null,
  live_identity: { release_id: "arl_2", release_sequence: 2, revision_id: "arv_2" },
  candidate_identity: null,
  next_draft_identity: { base_release_id: "arl_2", id: "awc_1", row_version: 3 },
  active_plan_identity: null,
  active_snapshot_identity: null,
  active_override_identity: null,
  active_execution_summary: null,
  control_row_version: null,
  display_state: "draft",
  draft_rebase_state: "current",
  readiness: null,
  reason_codes: [],
  stale_subject: null,
  primary_action: { kind: "prepare_candidate", enabled: true, disabled_reason_codes: [] },
  secondary_actions: [],
};

const CANDIDATE = {
  audience_mode: "private" as const,
  candidate_hash: "h1",
  candidate_id: "arc_1",
  revision_id: "arv_3",
  row_version: 4,
  state: "prepared" as const,
};

function workbench(overrides: Partial<AgentVersionWorkbench> = {}): AgentVersionWorkbench {
  return { ...BASE, ...overrides };
}

const RUNNING = {
  can_cancel: true,
  execution_id: "aex_1",
  failure_code: null,
  finished_at: null,
  row_version: 1,
  started_at: null,
  status: "running",
};

describe("trialCandidatePlan", () => {
  it.each([
    ["no candidate yet: freeze the saved draft", workbench(), { kind: "prepare", replace: null }],
    ["a prepared candidate that covers the saved draft is reused", workbench({ candidate_identity: CANDIDATE }), { kind: "reuse", candidateId: "arc_1", revisionRef: "arv_3", candidateKind: "draft", underTest: false }],
    [
      "a stale prepared candidate is replaced",
      workbench({ candidate_identity: CANDIDATE, draft_ahead_of_candidate: true }),
      { kind: "prepare", replace: { candidate_id: "arc_1", candidate_row_version: 4, candidate_hash: "h1" } },
    ],
    [
      "TD1: a candidate under test is kept even when the draft moved on",
      workbench({ candidate_identity: CANDIDATE, draft_ahead_of_candidate: true, active_execution_summary: RUNNING }),
      { kind: "reuse", candidateId: "arc_1", candidateKind: "draft", underTest: true },
    ],
    [
      "in review the submitted version is tried, edits after it are not",
      workbench({ candidate_identity: { ...CANDIDATE, state: "pending" }, draft_ahead_of_candidate: true }),
      { kind: "reuse", candidateId: "arc_1", candidateKind: "submitted", underTest: false },
    ],
    ["nothing to try when the draft equals the release", workbench({ next_draft_identity: null }), { kind: "nothing" }],
    ["a draft behind a newer release has to be updated first", workbench({ draft_rebase_state: "required" }), { kind: "rebase" }],
    [
      "a stale candidate on a draft behind a newer release is not replaced",
      workbench({ candidate_identity: CANDIDATE, draft_ahead_of_candidate: true, draft_rebase_state: "required" }),
      { kind: "rebase" },
    ],
  ] as const)("%s", (_label, value, expected) => {
    expect(trialCandidatePlan(value)).toMatchObject(expected);
  });
});

describe("the save rule", () => {
  it.each([
    [[], "none"],
    [["instructions"], "silent"],
    [["instructions", "audience"], "confirm"],
    [["description"], "confirm"],
  ] as const)("%j → %s", (fields, step) => {
    expect(draftSaveStep(fields)).toBe(step);
  });

  it("names only the other fields, in page order", () => {
    expect(confirmedDraftFields(["audience", "instructions", "description"])).toEqual(["description", "audience"]);
  });
});

function deps(overrides: Partial<TrialStartDeps> = {}): TrialStartDeps {
  return {
    dirtyFields: [],
    savedDraftEqualsRelease: false,
    confirmSave: vi.fn(async () => "save" as const),
    save: vi.fn(async () => true),
    loadWorkbench: vi.fn(async () => workbench()),
    loadWorkingCopy: vi.fn(async () => ({ id: "awc_1", row_version: 5 })),
    prepare: vi.fn(async () => ({ candidate_id: "arc_9", revision_id: "arv_9" })),
    ...overrides,
  };
}

describe("startDraftTrial", () => {
  it("saves only 工作说明 without asking, then freezes the new draft", async () => {
    const value = deps({ dirtyFields: ["instructions"] });
    await expect(startDraftTrial(value)).resolves.toMatchObject({ kind: "ready", candidateId: "arc_9", revisionRef: "arv_9" });
    expect(value.confirmSave).not.toHaveBeenCalled();
    expect(value.save).toHaveBeenCalledOnce();
    expect(value.prepare).toHaveBeenCalledWith({ working_copy_id: "awc_1", working_copy_row_version: 5, expected_eval_policy_revision_id: null });
  });

  it("freezes with the Agent's current evaluation Policy once it has one (F21)", async () => {
    // Mutation: drop `expected_eval_policy_revision_id` → every freeze after the first test answers 409
    // agent_eval_policy_version_conflict.
    const value = deps({ loadWorkbench: vi.fn(async () => workbench({ current_eval_policy_revision_id: "aepr_7" })) });
    await expect(startDraftTrial(value)).resolves.toMatchObject({ kind: "ready" });
    expect(value.prepare).toHaveBeenCalledWith(expect.objectContaining({ expected_eval_policy_revision_id: "aepr_7" }));
  });

  it("asks before saving other fields and offers the saved draft only when it differs from the release", async () => {
    const value = deps({ dirtyFields: ["instructions", "audience"], savedDraftEqualsRelease: true });
    await startDraftTrial(value);
    expect(value.confirmSave).toHaveBeenCalledWith(["audience"], { offerSavedOnly: false, withInstructions: true });
    expect(value.save).toHaveBeenCalledOnce();
  });

  it("tells the confirmation when 工作说明 is not among the unsaved fields", async () => {
    const value = deps({ dirtyFields: ["audience"] });
    await startDraftTrial(value);
    expect(value.confirmSave).toHaveBeenCalledWith(["audience"], { offerSavedOnly: true, withInstructions: false });
  });

  it("offers no save when the page knows it can't be saved, and freezes nothing (F8)", async () => {
    for (const dirtyFields of [["audience"], ["instructions"]] as const) {
      const value = deps({ dirtyFields, saveBlocked: true });
      await expect(startDraftTrial(value)).resolves.toEqual({ kind: "save_blocked" });
      expect(value.confirmSave).not.toHaveBeenCalled();
      expect(value.save).not.toHaveBeenCalled();
      expect(value.prepare).not.toHaveBeenCalled();
    }
    // Nothing unsaved: the saved draft is tried as before.
    await expect(startDraftTrial(deps({ saveBlocked: true }))).resolves.toMatchObject({ kind: "ready" });
  });

  it("tries the saved draft without saving when asked to", async () => {
    const value = deps({ dirtyFields: ["description"], confirmSave: vi.fn(async () => "saved_only" as const) });
    await expect(startDraftTrial(value)).resolves.toMatchObject({ kind: "ready" });
    expect(value.save).not.toHaveBeenCalled();
  });

  it("stops on cancel and on a failed save, before anything is frozen", async () => {
    const cancelled = deps({ dirtyFields: ["name"], confirmSave: vi.fn(async () => "cancel" as const) });
    await expect(startDraftTrial(cancelled)).resolves.toEqual({ kind: "cancelled" });
    const failed = deps({ dirtyFields: ["instructions"], save: vi.fn(async () => false) });
    await expect(startDraftTrial(failed)).resolves.toEqual({ kind: "save_failed" });
    expect(cancelled.prepare).not.toHaveBeenCalled();
    expect(failed.loadWorkbench).not.toHaveBeenCalled();
  });

  it("reuses a covering candidate, including one in review, without preparing", async () => {
    const value = deps({ loadWorkbench: vi.fn(async () => workbench({ candidate_identity: { ...CANDIDATE, state: "pending" } })) });
    await expect(startDraftTrial(value)).resolves.toMatchObject({ kind: "ready", candidateId: "arc_1", candidateKind: "submitted" });
    expect(value.prepare).not.toHaveBeenCalled();
  });

  it("replaces a stale prepared candidate", async () => {
    const value = deps({ loadWorkbench: vi.fn(async () => workbench({ candidate_identity: CANDIDATE, draft_ahead_of_candidate: true })) });
    await startDraftTrial(value);
    expect(value.prepare).toHaveBeenCalledWith({
      working_copy_id: "awc_1",
      working_copy_row_version: 5,
      expected_eval_policy_revision_id: null,
      replace_candidate: { candidate_id: "arc_1", candidate_row_version: 4, candidate_hash: "h1" },
    });
  });

  it("returns the failing check when the draft can't be frozen", async () => {
    const cause = new Error("agent_release_blocked");
    const value = deps({ prepare: vi.fn(async () => { throw cause; }) });
    await expect(startDraftTrial(value)).resolves.toEqual({ kind: "prepare_failed", cause });
  });

  it("has nothing to try when no draft differs from the release", async () => {
    const value = deps({ loadWorkbench: vi.fn(async () => workbench({ next_draft_identity: null })) });
    await expect(startDraftTrial(value)).resolves.toEqual({ kind: "nothing_to_try" });
  });
});

describe("trial references and attribution", () => {
  const reference = { agentId: "osa_1", label: "门店周报", candidateId: "arc_1", revisionRef: "arv_3" };

  it("builds the only document a trial message may carry", () => {
    const document = trialInputDocument(reference, "本周销售怎么样？");
    expect(document.nodes).toHaveLength(2);
    expect(document.nodes[0]).toMatchObject({
      type: "reference",
      label: "门店周报",
      resource: { kind: "agent", source_kind: "team", id: "osa_1", version: "arv_3", trial_candidate_id: "arc_1" },
      parameters: {},
    });
    expect(document.nodes[1]).toEqual({ type: "text", text: " 本周销售怎么样？" });
    expect(isTrialDocument(document, reference)).toBe(true);
    expect(isTrialDocument(document, { ...reference, candidateId: "arc_2" })).toBe(false);
  });

  it("reads the trial member and ignores malformed ones", () => {
    expect(readTrialAttribution({ candidate_id: "arc_1", candidate_kind: "draft", frozen_at: "2026-11-25T06:32:00Z" })).toEqual({
      candidateId: "arc_1",
      candidateKind: "draft",
      frozenAt: "2026-11-25T06:32:00Z",
    });
    expect(readTrialAttribution({ candidate_id: "arc_1", candidate_kind: "live" })).toBeNull();
    expect(readTrialAttribution(undefined)).toBeNull();
  });

  it("marks a divider only between two attributed answers with different drafts", () => {
    const a = { candidateId: "arc_1", candidateKind: "draft" as const, frozenAt: "x" };
    expect(trialDraftChanged(a, { ...a, candidateId: "arc_2" })).toBe(true);
    expect(trialDraftChanged(a, a)).toBe(false);
    expect(trialDraftChanged(null, a)).toBe(false);
  });

  it("reads zone-less server times as UTC", () => {
    expect(parseTrialTime("2026-11-25 06:32:00")?.toISOString()).toBe("2026-11-25T06:32:00.000Z");
    expect(parseTrialTime("2026-11-25T06:32:00+00:00")?.toISOString()).toBe("2026-11-25T06:32:00.000Z");
    expect(parseTrialTime("nope")).toBeNull();
  });

  it("ends a trial whose latest draft was released", () => {
    const value = {
      recent_trials: [
        { session_id: "s_1", first_question: "q", last_message_at: "t", turn_count: 1, drafts_used: [], released_as: { release_id: "arl_3", release_sequence: 3 } },
        { session_id: "s_2", first_question: "q", last_message_at: "t", turn_count: 1, drafts_used: [], released_as: null },
      ],
    };
    expect(trialEndedRelease(value, "s_1")).toBe(3);
    expect(trialEndedRelease(value, "s_2")).toBeNull();
    expect(trialEndedRelease(value, "s_3")).toBeNull();
  });
});
