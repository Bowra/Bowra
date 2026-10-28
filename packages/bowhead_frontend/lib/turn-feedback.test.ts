/**
 * Chat turn feedback signals (D3 / D5 / D6): pure projections plus the shared live/replay
 * dispatcher path. Live SSE and F5 replay feed the same persisted events through
 * `dispatchToTurnStore`, so one assertion per path proves live == persisted == replayed.
 */
import { beforeEach, describe, expect, it } from "vitest";

import enMessages from "../messages/en-US.json";
import zhMessages from "../messages/zh-CN.json";
import deMessages from "../messages/de-DE.json";
import frMessages from "../messages/fr-FR.json";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "./turn-store-dispatch";
import { replayTurnEventRows } from "./turn-store-replay";
import {
  chooseFailureCardAction,
  isLegacyFailureAnswerReason,
  parseRecoveryAction,
  parseStepOutcome,
  resolveRecoveryAction,
  stepIssueSummary,
} from "./turn-feedback";

const T = "t_feedback";

beforeEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

const ALL = { retry: true, editAndResend: true, newSession: true };

describe("recovery action (D5)", () => {
  it("uses the published action and accepts only the finite vocabulary", () => {
    expect(parseRecoveryAction("new_session")).toBe("new_session");
    expect(parseRecoveryAction("reboot")).toBeUndefined();
    expect(resolveRecoveryAction("edit_and_resend", { schemaVersion: 1, disclosure: "authenticated", retryable: true }))
      .toBe("edit_and_resend");
  });

  it("falls back to retryable for old turns without recovery_action", () => {
    expect(resolveRecoveryAction(undefined, { schemaVersion: 1, disclosure: "authenticated", retryable: true })).toBe("retry");
    expect(resolveRecoveryAction(undefined, { schemaVersion: 1, disclosure: "authenticated", retryable: false })).toBe("copy_diagnostics");
    expect(resolveRecoveryAction(undefined, { schemaVersion: 1, disclosure: "authenticated" })).toBe("edit_and_resend");
    expect(resolveRecoveryAction(undefined, undefined)).toBe("edit_and_resend");
  });

  it("shows exactly one action, degrading when the surface cannot perform it", () => {
    expect(chooseFailureCardAction("retry", ALL)).toBe("retry");
    expect(chooseFailureCardAction("retry", { ...ALL, retry: false })).toBe("edit_and_resend");
    // Replay that only restores the draft is an edit, not a retry.
    expect(chooseFailureCardAction("retry", { ...ALL, retryRestoresDraft: true })).toBe("edit_and_resend");
    expect(chooseFailureCardAction("new_session", ALL)).toBe("new_session");
    expect(chooseFailureCardAction("new_session", { retry: false, editAndResend: false, newSession: false })).toBe("copy_diagnostics");
    expect(chooseFailureCardAction("copy_diagnostics", ALL)).toBe("copy_diagnostics");
    expect(chooseFailureCardAction("edit_and_resend", { ...ALL, editAndResend: false })).toBe("new_session");
    // An older attachment-only question: nothing to edit, but its attachments can start a new session.
    expect(chooseFailureCardAction("retry", { retry: false, editAndResend: false, newSession: true })).toBe("new_session");
    expect(chooseFailureCardAction("edit_and_resend", { ...ALL, editAndResend: false, newSession: false })).toBe("copy_diagnostics");
  });
});

describe("step outcome copy (D6)", () => {
  const done = (outcome: Record<string, unknown>, status = "completed") =>
    parseStepOutcome({ schema_version: 1, ...outcome }, status);

  it("mixed recovered and unresolved steps do not claim complete recovery", () => {
    const outcome = done({ issue_steps: 3, recovered_steps: 2, unresolved_steps: 1, answer_affected: false });
    expect(stepIssueSummary(outcome, 3)).toEqual({ key: "step_issues_n", count: 3, tone: "secondary" });
  });

  it("does not claim 'handled automatically' when no step was structurally recovered", () => {
    const outcome = done({ issue_steps: 2, recovered_steps: 0, unresolved_steps: 2, answer_affected: false });
    expect(stepIssueSummary(outcome, 2)).toEqual({ key: "step_issues_n", count: 2, tone: "secondary" });
  });

  it("warns only when an unresolved issue affected the answer, counting unresolved_steps", () => {
    const outcome = done({ issue_steps: 3, recovered_steps: 2, unresolved_steps: 1, answer_affected: true });
    expect(stepIssueSummary(outcome, 3)).toEqual({ key: "step_issues_affecting_n", count: 1, tone: "warning" });
  });

  it("keeps the neutral local count for old turns and failed turns", () => {
    expect(stepIssueSummary(null, 2)).toEqual({ key: "step_issues_n", count: 2, tone: "secondary" });
    expect(stepIssueSummary(null, 0)).toBeNull();
    const failed = done({ issue_steps: 2, recovered_steps: 0, unresolved_steps: 2, answer_affected: false }, "error");
    expect(stepIssueSummary(failed, 2)).toEqual({ key: "step_issues_n", count: 2, tone: "secondary" });
    expect(stepIssueSummary(done({ issue_steps: 0, recovered_steps: 0, unresolved_steps: 0, answer_affected: false }), 0)).toBeNull();
  });

  it("treats a missing or malformed field as unknown, never zero", () => {
    expect(parseStepOutcome(undefined, "completed")).toBeNull();
    expect(parseStepOutcome({ schema_version: 2, issue_steps: 1, recovered_steps: 0, unresolved_steps: 1, answer_affected: false }, "completed")).toBeNull();
    expect(parseStepOutcome({ schema_version: 1, issue_steps: -1, recovered_steps: 0, unresolved_steps: 1, answer_affected: false }, "completed")).toBeNull();
  });

  it("live dispatch and replay write the same store field from done", () => {
    const step_outcome = { schema_version: 1, issue_steps: 2, recovered_steps: 1, unresolved_steps: 1, answer_affected: true };
    dispatchToTurnStore("done", { card_id: T, turn_id: T, status: "completed", step_outcome, seq: 9 });
    const live = getTurnSnapshot(T)?.stepOutcome;

    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    replayTurnEventRows(T, [{ seq: 9, event_type: "done", payload: { card_id: T, turn_id: T, status: "completed", step_outcome, seq: 9 }, occurred_at: "" }]);
    const replayed = getTurnSnapshot(T)?.stepOutcome;

    expect(live).toEqual({ issueSteps: 2, recoveredSteps: 1, unresolvedSteps: 1, answerAffected: true, turnCompleted: true });
    expect(replayed).toEqual(live);
  });
});

describe("legacy failure answer suppression (D3)", () => {
  const legacyFailedTurn = [
    { seq: 1, event_type: "final_answer", payload: { card_id: T, turn_id: T, text: "本次分析未能完成. 可能是 LLM 服务繁忙", reason_code: "silent_done_fallback", seq: 1 }, occurred_at: "" },
    { seq: 2, event_type: "turn_failed", payload: { card_id: T, turn_id: T, reason: "lead_internal_error", seq: 2 }, occurred_at: "" },
    { seq: 3, event_type: "done", payload: { card_id: T, turn_id: T, status: "error", reason_code: "lead_internal_error", seq: 3 }, occurred_at: "" },
  ];
  const answerBeats = () => (getTurnSnapshot(T)?.beats ?? []).filter((b) => b.kind === "answer");

  it("recognises only failure reasons and silent_done_fallback", () => {
    expect(isLegacyFailureAnswerReason("silent_done_fallback")).toBe(true);
    expect(isLegacyFailureAnswerReason("wallclock_exceeded")).toBe(true);
    expect(isLegacyFailureAnswerReason("canvas_pointer_reconciled")).toBe(false);
    expect(isLegacyFailureAnswerReason(undefined)).toBe(false);
  });

  it("drops the legacy failure answer live and through replay", () => {
    for (const row of legacyFailedTurn) dispatchToTurnStore(row.event_type, row.payload);
    expect(answerBeats()).toEqual([]);

    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    replayTurnEventRows(T, legacyFailedTurn);
    expect(answerBeats()).toEqual([]);
  });

  it("keeps a normal answer with another reason code, and the empty-result line of a completed turn", () => {
    replayTurnEventRows(T, [
      { seq: 1, event_type: "final_answer", payload: { card_id: T, turn_id: T, text: "Real answer", reason_code: "canvas_pointer_reconciled", seq: 1 }, occurred_at: "" },
      { seq: 2, event_type: "turn_failed", payload: { card_id: T, turn_id: T, reason: "lead_internal_error", seq: 2 }, occurred_at: "" },
    ]);
    expect(answerBeats().map((b) => b.kind === "answer" && b.text)).toEqual(["Real answer"]);

    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    replayTurnEventRows(T, [
      { seq: 1, event_type: "final_answer", payload: { card_id: T, turn_id: T, text: "Nothing displayable", reason_code: "silent_done_fallback", seq: 1 }, occurred_at: "" },
      { seq: 2, event_type: "done", payload: { card_id: T, turn_id: T, status: "completed", seq: 2 }, occurred_at: "" },
    ]);
    expect(answerBeats().map((b) => b.kind === "answer" && b.text)).toEqual(["Nothing displayable"]);
  });
});

describe("copy keys exist in every locale", () => {
  it.each([
    ["zh-CN", zhMessages],
    ["en-US", enMessages],
    ["de-DE", deMessages],
    ["fr-FR", frMessages],
  ] as const)("%s", (_locale, catalog) => {
    expect(catalog.step_list.step_issues_handled_n).toBeTruthy();
    expect(catalog.step_list.step_issues_affecting_n).toBeTruthy();
    expect(catalog.chat.final_fail_new_session).toBeTruthy();
    expect(catalog.chat_card.copy_diagnostics).toBeTruthy();
  });

  it("zh-CN copy uses full-width punctuation", () => {
    expect(zhMessages.step_list.step_issues_handled_n).toBe("{count} 个步骤曾失败，后续调用已成功");
    expect(zhMessages.step_list.step_issues_affecting_n).not.toMatch(/[,.;]/);
  });
});
