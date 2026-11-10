// @vitest-environment node
/**
 * Inline tool call confirmation in the canonical turn store: the request attaches to its
 * waiting step, the outcome comes only from `confirmation_resolved` (or the turn's own
 * terminal events), and live dispatch and replay of the same persisted rows agree.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { stepToStepView } from "@/components/chat/step-list/step-view";

import {
  _resetAllTurnsForTesting,
  getPendingConfirmationsSnapshot,
  getTurnSnapshot,
  hasAnyActiveStep,
  knownConfirmationOutcome,
  type TurnState,
} from "./turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "./turn-store-dispatch";
import { replayTurnEventRows } from "./turn-store-replay";

const T = "t_confirm";

function stepStarted(seq: number, stepId = "step_call_1", extra: Record<string, unknown> = {}) {
  return {
    event_type: "step_started",
    payload: {
      seq,
      card_id: T,
      step_id: stepId,
      step_type: "tool_run",
      tool_name: "teammcp.cloudflare.migrate_pages",
      header_zh: "Team tool",
      started_at: "2026-11-08T10:00:00Z",
      call_id: stepId.replace(/^step_/, ""),
      ...extra,
    },
  };
}

function requested(seq: number, overrides: Record<string, unknown> = {}) {
  return {
    event_type: "confirmation_requested",
    payload: {
      seq,
      card_id: T,
      step_id: "step_call_1",
      call_id: "call_1",
      confirmation_id: "ttc_one",
      kind: "team_mcp",
      requester_user_id: "u_owner",
      title: "migrate_pages_to_workers_guide",
      source_label: "Cloudflare Docs",
      category: "unknown",
      choices: ["reject", "approve", "approve_session"],
      expires_at: "2026-11-08T10:03:00Z",
      timeout_seconds: 180,
      parent_agent_id: null,
      ...overrides,
    },
  };
}

function resolved(seq: number, outcome: string, overrides: Record<string, unknown> = {}) {
  return {
    event_type: "confirmation_resolved",
    payload: {
      seq,
      card_id: T,
      step_id: "step_call_1",
      confirmation_id: "ttc_one",
      outcome,
      decided_at: "2026-11-08T10:01:00Z",
      ...overrides,
    },
  };
}

type Row = { event_type: string; payload: Record<string, unknown> };

function live(rows: Row[]): void {
  for (const row of rows) dispatchToTurnStore(row.event_type, { ...row.payload }, "live", T);
}

function confirmationOf(stepId = "step_call_1") {
  return getTurnSnapshot(T)?.steps.find((step) => step.stepId === stepId)?.confirmation;
}

/** receivedAt is the local receipt time and the only field allowed to differ between paths. */
function withoutReceipt(state: TurnState | undefined) {
  return {
    ...state,
    steps: state?.steps.map((step) =>
      step.confirmation ? { ...step, confirmation: { ...step.confirmation, receivedAt: 0 } } : step,
    ),
  };
}

beforeEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

afterEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

describe("confirmation_requested", () => {
  it("attaches the request to its waiting step", () => {
    live([stepStarted(1), requested(2)]);
    expect(confirmationOf()).toMatchObject({
      id: "ttc_one",
      kind: "team_mcp",
      requesterUserId: "u_owner",
      title: "migrate_pages_to_workers_guide",
      sourceLabel: "Cloudflare Docs",
      category: "unknown",
      choices: ["reject", "approve", "approve_session"],
      expiresAt: "2026-11-08T10:03:00Z",
      timeoutSeconds: 180,
    });
    expect(confirmationOf()?.outcome).toBeUndefined();
    expect(getTurnSnapshot(T)?.steps).toHaveLength(1);
  });

  it("creates a placeholder row for an unknown step, which the late step_started replaces in place", () => {
    live([requested(2)]);
    const placeholder = getTurnSnapshot(T)!.steps[0]!;
    expect(placeholder).toMatchObject({ stepId: "step_call_1", state: "active", label: "migrate_pages_to_workers_guide" });
    expect(getTurnSnapshot(T)!.beats).toEqual([{ kind: "step", stepId: "step_call_1" }]);

    live([stepStarted(1)]);
    const state = getTurnSnapshot(T)!;
    expect(state.steps).toHaveLength(1);
    expect(state.steps[0]!.toolName).toBe("teammcp.cloudflare.migrate_pages");
    expect(state.steps[0]!.confirmation?.id).toBe("ttc_one");
    expect(state.beats).toEqual([{ kind: "step", stepId: "step_call_1" }]);
  });

  it("keeps the confirmation when step_started is redelivered", () => {
    live([stepStarted(1), requested(2)]);
    dispatchToTurnStore("step_started", { ...stepStarted(1).payload, seq: undefined }, "live", T);
    expect(confirmationOf()?.id).toBe("ttc_one");
  });

  it("never restarts the countdown or reopens a decision on redelivery", () => {
    live([stepStarted(1), requested(2)]);
    const receivedAt = confirmationOf()!.receivedAt;
    live([resolved(3, "rejected")]);
    // A reconnect replays rows without the dedupe seq in some paths.
    dispatchToTurnStore("confirmation_requested", { ...requested(2).payload, seq: undefined }, "live", T);
    expect(confirmationOf()).toMatchObject({ outcome: "rejected", receivedAt });
  });

  it("claims and ignores a malformed request", () => {
    live([stepStarted(1)]);
    expect(dispatchToTurnStore("confirmation_requested", { card_id: T, step_id: "step_call_1", kind: "nope" }, "live", T)).toBe(true);
    expect(confirmationOf()).toBeUndefined();
  });
});

describe("confirmation_resolved", () => {
  it.each(["approved", "approved_session", "rejected", "expired", "unavailable"])("records %s with its decision time", (outcome) => {
    live([stepStarted(1), requested(2), resolved(3, outcome)]);
    expect(confirmationOf()).toMatchObject({ outcome, decidedAt: "2026-11-08T10:01:00Z" });
  });

  it("keeps the first decision", () => {
    live([stepStarted(1), requested(2), resolved(3, "approved"), resolved(4, "expired")]);
    expect(confirmationOf()?.outcome).toBe("approved");
  });

  it("drops a resolution for a confirmation it never saw", () => {
    live([stepStarted(1), resolved(3, "approved")]);
    expect(confirmationOf()).toBeUndefined();
  });
});

describe("finalization without a decision", () => {
  it("records stopped when the turn was cancelled", () => {
    live([stepStarted(1), requested(2)]);
    dispatchToTurnStore("done", { seq: 3, turn_id: T, status: "cancelled" }, "live", T);
    expect(confirmationOf()?.outcome).toBe("stopped");
    expect(getTurnSnapshot(T)?.steps[0]?.state).toBe("cancelled");
  });

  it("records ended when the turn finished another way", () => {
    live([stepStarted(1), requested(2), { event_type: "step_list_card_finalized", payload: { seq: 3, card_id: T, state: "failed", step_count: 1 } }]);
    expect(confirmationOf()?.outcome).toBe("ended");
  });

  it("leaves a decided confirmation as decided", () => {
    live([stepStarted(1), requested(2), resolved(3, "approved")]);
    dispatchToTurnStore("done", { seq: 4, turn_id: T, status: "cancelled" }, "live", T);
    expect(confirmationOf()?.outcome).toBe("approved");
  });

  it("marks a still-unresolved confirmation on an already finished step", () => {
    live([stepStarted(1), requested(2)]);
    dispatchToTurnStore("step_done", { seq: 3, card_id: T, step_id: "step_call_1", step_type: "tool_run", state: "success", duration_ms: 5 }, "live", T);
    dispatchToTurnStore("done", { seq: 4, turn_id: T, status: "completed" }, "live", T);
    expect(confirmationOf()?.outcome).toBe("ended");
  });
});

describe("live and replay agree", () => {
  const ROWS: Row[] = [
    { event_type: "step_list_card_created", payload: { seq: 1, card_id: T } },
    stepStarted(2),
    requested(3),
    stepStarted(4, "step_call_2"),
    requested(5, { step_id: "step_call_2", call_id: "call_2", confirmation_id: "esc_two", kind: "email_send", category: null, source_label: null, choices: ["reject", "approve"] }),
    resolved(6, "approved_session"),
    { event_type: "done", payload: { seq: 7, turn_id: T, status: "cancelled" } },
  ];

  it("produces the same state from live dispatch and from persisted rows", () => {
    live(ROWS);
    const liveState = withoutReceipt(getTurnSnapshot(T));
    expect(liveState.steps?.map((step) => step.confirmation?.outcome)).toEqual(["approved_session", "stopped"]);

    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    replayTurnEventRows(
      T,
      ROWS.map((row) => ({ seq: row.payload.seq as number, event_type: row.event_type, payload: row.payload, occurred_at: "" })),
    );
    expect(withoutReceipt(getTurnSnapshot(T))).toEqual(liveState);
  });

  it("replays rows that omit card_id the same way", () => {
    const rows = ROWS.map((row) => {
      if (!row.event_type.startsWith("confirmation_")) return row;
      const { card_id: _omit, ...payload } = row.payload;
      void _omit;
      return { ...row, payload };
    });
    live(ROWS);
    const liveState = withoutReceipt(getTurnSnapshot(T));
    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    replayTurnEventRows(T, rows.map((row) => ({ seq: row.payload.seq as number, event_type: row.event_type, payload: row.payload, occurred_at: "" })));
    expect(withoutReceipt(getTurnSnapshot(T))).toEqual(liveState);
  });
});

describe("read helpers", () => {
  it("lists pending confirmations with a stable identity and reports active steps", () => {
    expect(getPendingConfirmationsSnapshot()).toEqual([]);
    expect(hasAnyActiveStep()).toBe(false);
    live([stepStarted(1), requested(2)]);
    const first = getPendingConfirmationsSnapshot();
    expect(first.map((item) => [item.turnId, item.stepId, item.confirmation.id])).toEqual([[T, "step_call_1", "ttc_one"]]);
    expect(getPendingConfirmationsSnapshot()).toBe(first);
    expect(hasAnyActiveStep()).toBe(true);
    expect(knownConfirmationOutcome("ttc_one")).toBeUndefined();

    live([resolved(3, "rejected")]);
    expect(getPendingConfirmationsSnapshot()).toEqual([]);
    expect(knownConfirmationOutcome("ttc_one")).toBe("rejected");
  });

  it("projects the confirmation into the step view", () => {
    live([stepStarted(1), requested(2)]);
    const step = getTurnSnapshot(T)!.steps[0]!;
    expect(stepToStepView(step, {}).confirmation).toBe(step.confirmation);
    live([stepStarted(3, "step_plain")]);
    expect("confirmation" in stepToStepView(getTurnSnapshot(T)!.steps[1]!, {})).toBe(false);
  });
});
