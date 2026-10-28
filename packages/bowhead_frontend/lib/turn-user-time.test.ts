/**
 * User-message time = submission time (observed on 116: sent 07:46, but the completed bubble changed to 07:53 = completion time).
 * Live archived snapshots and refreshed hydration must resolve to the same value.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { _resetAllTurnsForTesting, applyTurnStarted } from "./turn-store";
import { resolveUserMessageTimeMs } from "./turn-user-time";
import type { TurnRunnerState } from "./types";
import { snapshotArchivedTurn } from "./use-turn-runner";

beforeEach(() => _resetAllTurnsForTesting());
afterEach(() => _resetAllTurnsForTesting());

describe("resolveUserMessageTimeMs", () => {
  it("首条 turn_started 优先，其次提交时间，最后兜底", () => {
    const first = new Date("2026-09-24T07:46:02Z");
    expect(resolveUserMessageTimeMs({ firstTurnStartedAt: first, submittedAtMs: 1, fallbackMs: 2 })).toBe(first.getTime());
    expect(resolveUserMessageTimeMs({ firstTurnStartedAt: null, submittedAtMs: 1, fallbackMs: 2 })).toBe(1);
    expect(resolveUserMessageTimeMs({ submittedAtMs: Number.NaN, fallbackMs: 2 })).toBe(2);
    expect(resolveUserMessageTimeMs({})).toBeNull();
  });
});

describe("live 归档快照的 userSentAt", () => {
  it("★ 取首条 turn_started，而不是完成时刻或续跑那一代的起点", () => {
    const first = "2026-09-24T07:46:02Z";
    applyTurnStarted({ turnId: "t_live", startedAt: first });
    // After clarification resume, runner.startedAt points at the new generation's start.
    const resumedStart = Date.parse("2026-09-24T07:51:00Z");
    const snap = snapshotArchivedTurn({
      turnId: "t_live",
      user: "近30天 Top5",
      answer: "结论",
      userAttachments: [],
      traceSteps: [],
      startedAt: resumedStart,
    } as unknown as TurnRunnerState);

    expect(snap?.userSentAt).toBe(Date.parse(first));
    expect(snap?.userSentAt).not.toBe(resumedStart);
  });

  it("turn_started 尚未到达时退回发送时刻", () => {
    const sentAt = Date.parse("2026-09-24T07:46:00Z");
    const snap = snapshotArchivedTurn({
      turnId: "t_early",
      user: "hi",
      answer: "",
      userAttachments: [],
      traceSteps: [],
      startedAt: sentAt,
    } as unknown as TurnRunnerState);
    expect(snap?.userSentAt).toBe(sentAt);
  });
});
