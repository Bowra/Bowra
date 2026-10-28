import { describe, expect, it } from "vitest";

import {
  formatTurnDuration,
  resolveHydratedTurnDurationMs,
  resolveLiveTurnDurationMs,
} from "./turn-duration";

describe("turn duration projection", () => {
  it("formats live and hydrated duration with one decimal place", () => {
    expect(formatTurnDuration(4_276)).toBe("4.3s");
    expect(formatTurnDuration(116_000)).toBe("116.0s");
  });

  it("uses a wire duration when the terminal event carries one", () => {
    expect(resolveLiveTurnDurationMs({
      wireDurationMs: 4_276,
      executionStartedAtMs: 100,
      nowMs: 999_999,
    })).toBe(4_276);
  });

  it("derives a synthetic terminal duration from the current execution generation", () => {
    expect(resolveLiveTurnDurationMs({
      wireDurationMs: undefined,
      executionStartedAtMs: 10_000,
      nowMs: 126_000,
    })).toBe(116_000);
  });

  it("repairs F5 duration when the final Stop had no matching persisted done event", () => {
    expect(resolveHydratedTurnDurationMs({
      summaryTotalMs: 4_353,
      terminalStatus: "cancelled",
      lastPersistedDoneStatus: "clarification_pending",
      lastExecutionStartedAt: "2026-08-08T01:02:02.339Z",
      endedAt: "2026-08-08T01:03:58.339Z",
    })).toBe(116_000);
  });

  it("keeps the durable summary when a matching terminal done was persisted", () => {
    expect(resolveHydratedTurnDurationMs({
      summaryTotalMs: 1_009,
      terminalStatus: "cancelled",
      lastPersistedDoneStatus: "cancelled",
      lastExecutionStartedAt: "2026-08-08T00:57:28.000Z",
      endedAt: "2026-08-08T00:57:29.009Z",
    })).toBe(1_009);
  });

  it("treats legacy done error as the durable failed terminal", () => {
    expect(resolveHydratedTurnDurationMs({
      summaryTotalMs: 2_400,
      terminalStatus: "failed",
      lastPersistedDoneStatus: "error",
      lastExecutionStartedAt: "2026-08-08T00:57:00.000Z",
      endedAt: "2026-08-08T00:57:09.000Z",
    })).toBe(2_400);
  });

  it("treats aborted and cancelled as the same stopped terminal", () => {
    expect(resolveHydratedTurnDurationMs({
      summaryTotalMs: 3_100,
      terminalStatus: "aborted",
      lastPersistedDoneStatus: "cancelled",
      lastExecutionStartedAt: "2026-08-08T00:57:00.000Z",
      endedAt: "2026-08-08T00:57:09.000Z",
    })).toBe(3_100);
  });
});
