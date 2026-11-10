// @vitest-environment node
/**
 * Chat byline times come from server timestamps only (dogfood N3 on 116): the live archived snapshot and refreshed
 * hydration resolve to the same values, and the browser clock never stamps a byline.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { _resetAllTurnsForTesting, applyTurnEnded, applyTurnStarted, getTurnSnapshot } from "./turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "./turn-store-dispatch";
import { parseServerTimeMs, resolveReplyTimeMs, resolveUserMessageTimeMs } from "./turn-user-time";
import type { TurnRunnerState } from "./types";
import { snapshotArchivedTurn } from "./use-turn-runner";

beforeEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});
afterEach(() => {
  vi.useRealTimers();
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

describe("resolveUserMessageTimeMs", () => {
  it("prefers the first turn_started, then the persisted turn start, and otherwise shows nothing", () => {
    const first = new Date("2026-09-24T07:46:02Z");
    expect(resolveUserMessageTimeMs({ firstTurnStartedAt: first, persistedStartedAtMs: 1 })).toBe(first.getTime());
    expect(resolveUserMessageTimeMs({ firstTurnStartedAt: null, persistedStartedAtMs: 1 })).toBe(1);
    expect(resolveUserMessageTimeMs({ persistedStartedAtMs: Number.NaN })).toBeNull();
    expect(resolveUserMessageTimeMs({})).toBeNull();
  });
});

describe("resolveReplyTimeMs", () => {
  it("shows nothing while the turn runs, then the root done time, then the persisted end", () => {
    const ended = new Date("2026-11-08T02:49:21Z");
    expect(resolveReplyTimeMs({ running: true, terminalEndedAt: ended, persistedEndedAtMs: 1 })).toBeNull();
    expect(resolveReplyTimeMs({ running: false, terminalEndedAt: ended, persistedEndedAtMs: 1 })).toBe(ended.getTime());
    expect(resolveReplyTimeMs({ running: false, terminalEndedAt: null, persistedEndedAtMs: 1 })).toBe(1);
    expect(resolveReplyTimeMs({ running: false })).toBeNull();
  });
});

describe("parseServerTimeMs", () => {
  it("reads an ISO server time and rejects missing or invalid values", () => {
    expect(parseServerTimeMs("2026-11-08T02:41:53+00:00")).toBe(Date.parse("2026-11-08T02:41:53Z"));
    expect(parseServerTimeMs(null)).toBeNull();
    expect(parseServerTimeMs("not a time")).toBeNull();
  });
});

describe("live archived snapshot", () => {
  it("keeps the first turn_started for the user byline, not a resumed generation's start", () => {
    const first = "2026-09-24T07:46:02Z";
    applyTurnStarted({ turnId: "t_live", startedAt: first });
    // After a clarification resume, runner.startedAt points at the new generation's start.
    const resumedStart = Date.parse("2026-09-24T07:51:00Z");
    const snap = snapshotArchivedTurn({
      turnId: "t_live",
      user: "Top 5 in the last 30 days",
      answer: "Conclusion",
      userAttachments: [],
      traceSteps: [],
      startedAt: resumedStart,
    } as unknown as TurnRunnerState);

    expect(snap?.userSentAt).toBe(Date.parse(first));
  });

  it("never stamps a byline from the browser clock when the server times are unknown", () => {
    // Mutation: restore `archivedAt: Date.now()` or the `submittedAtMs: s.startedAt` fallback → this fails.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-08T02:50:30Z"));
    const localSendTime = Date.parse("2026-11-08T02:50:00Z");
    const snap = snapshotArchivedTurn({
      turnId: "t_early",
      user: "hi",
      answer: "partial",
      userAttachments: [],
      traceSteps: [],
      startedAt: localSendTime,
    } as unknown as TurnRunnerState);

    expect(snap?.userSentAt).toBeNull();
    expect(snap?.endedAt).toBeNull();
  });

  it("carries the root done's server time, not the moment the next message archived it", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-08T02:47:00Z"));
    applyTurnStarted({ turnId: "t_done", startedAt: "2026-11-08T02:40:10Z" });
    applyTurnEnded({ turnId: "t_done", endedAt: "2026-11-08T02:41:53Z" });
    const snap = snapshotArchivedTurn({
      turnId: "t_done",
      user: "q",
      answer: "a",
      userAttachments: [],
      traceSteps: [],
      startedAt: Date.parse("2026-11-08T02:40:10Z"),
    } as unknown as TurnRunnerState);

    expect(snap?.endedAt).toBe(Date.parse("2026-11-08T02:41:53Z"));
  });

  it("keeps the hydrated session times of a reloaded last turn when it is archived", () => {
    const snap = snapshotArchivedTurn({
      turnId: "t_hydrated",
      user: "q",
      answer: "a",
      userAttachments: [],
      traceSteps: [],
      startedAt: 5,
      persistedStartedAt: Date.parse("2026-11-08T02:48:08Z"),
      persistedEndedAt: Date.parse("2026-11-08T02:49:21Z"),
    } as unknown as TurnRunnerState);

    expect(snap?.userSentAt).toBe(Date.parse("2026-11-08T02:48:08Z"));
    expect(snap?.endedAt).toBe(Date.parse("2026-11-08T02:49:21Z"));
  });
});

describe("turn store end time from the shared done branch", () => {
  it("records the root done time, ignores a helper's done and clears it when the turn resumes", () => {
    const T = "t_resume";
    dispatchToTurnStore("done", { turn_id: T, seq: 2, status: "clarification_pending", ended_at: "2026-11-08T02:48:20Z" }, "live", T);
    expect(getTurnSnapshot(T)?.endedAt?.toISOString()).toBe("2026-11-08T02:48:20.000Z");

    dispatchToTurnStore("done", { turn_id: T, seq: 3, status: "completed", parent_agent_id: "sa_1", ended_at: "2026-11-08T02:48:25Z" }, "live", T);
    expect(getTurnSnapshot(T)?.endedAt?.toISOString()).toBe("2026-11-08T02:48:20.000Z");

    // A resumed generation is running again: no stale pause time until its own done.
    dispatchToTurnStore("turn.resumed", { turn_id: T, seq: 4, execution_generation: 2 }, "live", T);
    expect(getTurnSnapshot(T)?.endedAt).toBeNull();

    dispatchToTurnStore("done", { turn_id: T, seq: 6, status: "completed", ended_at: "2026-11-08T02:49:21Z" }, "live", T);
    expect(getTurnSnapshot(T)?.endedAt?.toISOString()).toBe("2026-11-08T02:49:21.000Z");
  });
});
