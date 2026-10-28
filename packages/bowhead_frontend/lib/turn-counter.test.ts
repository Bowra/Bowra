/**
 * Phase 1.7-D — turn counter unit test (refreshed for p0-followup F5 off-by-one fix).
 *
 * Pins (updated semantics — F5):
 *  · counter = archivedTurnsLength + one unarchived active projection
 *  · durable completion never masquerades as evidence verification
 *  · empty-state (0 archived + no active turn) is 0
 *    (rendering site gates by runner.started || sessionId so this string
 *    never reaches the DOM in that branch)
 *  · F5 critical case — `hasActiveTurnId=true + running=false + no error`
 *    means the turn is DONE and already in archivedTurns; the runner's
 *    stale turnId must NOT count as a +1 (dogfood Q1 showed "turn-02"
 *    for the first completed turn). The expected output for
 *    `archived=1 + turnId set + done` is `turn-01`.
 */

import { describe, expect, it } from "vitest";

import { sessionTurnOrdinals, turnOrdinal } from "./turn-counter";

describe("turnOrdinal", () => {
  it("is 1 for the first running turn", () => {
    // First turn running — archived=0, running=true → 0 + 1 = 1.
    expect(
      turnOrdinal({
        archivedTurnsLength: 0,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: true,
        running: true,
        hasError: false,
      }),
    ).toBe(1);
  });

  it("F5 · 0 archived + 1 active running → turn-01", () => {
    expect(
      turnOrdinal({
        archivedTurnsLength: 0,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: true,
        running: true,
        hasError: false,
      }),
    ).toBe(1);
  });

  it("F5 · 1 archived + 1 active running → turn-02", () => {
    // Q2 running with Q1 already archived.
    expect(
      turnOrdinal({
        archivedTurnsLength: 1,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: true,
        running: true,
        hasError: false,
      }),
    ).toBe(2);
  });

  it("F5 · 1 archived + 0 active (done state) → turn-01", () => {
    // Right after Q1 completes — Q1 is in archivedTurns, runner.turnId
    // remains set (stale), running=false. Pre-F5 calc returned "turn-02"
    // (the bug). New semantics: 1 + 0 = 1.
    expect(
      turnOrdinal({
        archivedTurnsLength: 1,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: false,
        running: false,
        hasError: false,
        terminalStatus: "completed",
      }),
    ).toBe(1);
  });

  it("F5 · 1 archived + 0 active (done state) sans turnId → turn-01", () => {
    // Between turns once startTurn cleared turnId — same display.
    expect(
      turnOrdinal({
        archivedTurnsLength: 1,
        hasActiveTurnId: false,
        hasUnarchivedActiveTurn: false,
        running: false,
        hasError: false,
        terminalStatus: "completed",
      }),
    ).toBe(1);
  });

  it("zero-pads single-digit counters", () => {
    expect(
      turnOrdinal({
        archivedTurnsLength: 2,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: true,
        running: true,
        hasError: false,
      }),
    ).toBe(3);
  });

  it("two-digit counters stay unpadded", () => {
    expect(
      turnOrdinal({
        archivedTurnsLength: 11,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: true,
        running: true,
        hasError: false,
      }),
    ).toBe(12);
  });

  it("post-completion count matches archived length without claiming verification", () => {
    expect(
      turnOrdinal({
        archivedTurnsLength: 3,
        hasActiveTurnId: false,
        hasUnarchivedActiveTurn: false,
        running: false,
        hasError: false,
        terminalStatus: "completed",
      }),
    ).toBe(3);
  });

  it("drops 'verified' suffix when hasError=true and counts +1 (turn not "
    + "archived on error)", () => {
    // done(error) handler does NOT archive — so the failed turn still
    // contributes its own +1 to the counter.
    expect(
      turnOrdinal({
        archivedTurnsLength: 0,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: true,
        running: false,
        hasError: true,
      }),
    ).toBe(1);
  });

  it("error after one archived turn → turn-02 (active errored turn counts)", () => {
    expect(
      turnOrdinal({
        archivedTurnsLength: 1,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: true,
        running: false,
        hasError: true,
      }),
    ).toBe(2);
  });

  it("renders 'turn-00' when there are no turns yet", () => {
    expect(
      turnOrdinal({
        archivedTurnsLength: 0,
        hasActiveTurnId: false,
        hasUnarchivedActiveTurn: false,
        running: false,
        hasError: false,
      }),
    ).toBe(0);
  });

  it("F5 · latest completed turn stays in the active projection", () => {
    expect(
      turnOrdinal({
        archivedTurnsLength: 0,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: true,
        running: false,
        hasError: false,
        terminalStatus: "completed",
      }),
    ).toBe(1);
  });

  it("same-turn clarification resume does not count an active turn already present in hydration", () => {
    expect(
      turnOrdinal({
        archivedTurnsLength: 10,
        archivedTurnIds: [
          "t_01", "t_02", "t_03", "t_04", "t_05",
          "t_06", "t_07", "t_08", "t_09", "t_10",
        ],
        hasActiveTurnId: true,
        activeTurnId: "t_10",
        hasUnarchivedActiveTurn: true,
        running: true,
        hasError: false,
      }),
    ).toBe(10);
  });

  it("does not verify a cancelled terminal without an errorReason", () => {
    expect(
      turnOrdinal({
        archivedTurnsLength: 0,
        hasActiveTurnId: true,
        hasUnarchivedActiveTurn: true,
        running: false,
        hasError: false,
        terminalStatus: "cancelled",
      }),
    ).toBe(1);
  });
});

describe("sessionTurnOrdinals (report history numbering)", () => {
  const base = {
    hasActiveTurnId: true,
    running: false,
    hasError: false,
  };

  it("numbers archived turns in order and gives the active turn the topbar ordinal", () => {
    const input = {
      ...base,
      archivedTurnsLength: 2,
      archivedTurnIds: ["t_1", "t_2"],
      activeTurnId: "t_3",
      hasUnarchivedActiveTurn: true,
      running: true,
    };
    const map = sessionTurnOrdinals(input);
    expect([...map]).toEqual([["t_1", 1], ["t_2", 2], ["t_3", 3]]);
    expect(map.get("t_3")).toBe(turnOrdinal(input));
  });

  it("F5 replay shape (latest completed turn left active) and live shape (it is archived) agree", () => {
    const replay = sessionTurnOrdinals({
      ...base,
      archivedTurnsLength: 2,
      archivedTurnIds: ["t_1", "t_2"],
      activeTurnId: "t_3",
      hasUnarchivedActiveTurn: true,
    });
    const live = sessionTurnOrdinals({
      ...base,
      archivedTurnsLength: 3,
      archivedTurnIds: ["t_1", "t_2", "t_3"],
      activeTurnId: "t_3",
      hasUnarchivedActiveTurn: false,
    });
    expect([...replay]).toEqual([...live]);
  });

  it("anonymous archived turns still take a number; duplicates collapse", () => {
    const map = sessionTurnOrdinals({
      ...base,
      archivedTurnsLength: 4,
      archivedTurnIds: ["t_1", null, "t_1", "t_3"],
      activeTurnId: null,
      hasUnarchivedActiveTurn: false,
    });
    expect([...map]).toEqual([["t_1", 1], ["t_3", 3]]);
  });
});

describe("turnOrdinal (topbar caption, P2 batch 3)", () => {
  it("is the number behind the topbar caption", () => {
    const input = {
      archivedTurnsLength: 2,
      archivedTurnIds: ["t_1", "t_2"],
      hasActiveTurnId: true,
      activeTurnId: "t_3",
      hasUnarchivedActiveTurn: true,
      running: true,
      hasError: false,
    };
    expect(turnOrdinal(input)).toBe(3);
    expect(turnOrdinal({ ...input, archivedTurnIds: ["t_1", "t_2", "t_3"] })).toBe(3);
  });
});
