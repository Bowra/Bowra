import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTurnRunner } from "./use-turn-runner";
import { _resetAllTurnsForTesting } from "./turn-store";

describe("useTurnRunner mock completed lifecycle parity", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    _resetAllTurnsForTesting();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    _resetAllTurnsForTesting();
  });

  it("archives a completed scenario instead of retaining an impossible live-done turn", async () => {
    const hook = renderHook(() =>
      useTurnRunner({
        mode: "mock",
        scenarioId: "peer",
        replayKey: 0,
      }),
    );

    expect(hook.result.current.user).toBe("帮我分析下 Q3 营收的同比变化");
    expect(hook.result.current.archivedTurns).toHaveLength(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.user).toBeNull();
    expect(hook.result.current.answer).toBe("");
    expect(hook.result.current.traceSteps).toHaveLength(0);
    expect(hook.result.current.archivedTurns).toHaveLength(1);
    expect(hook.result.current.archivedTurns?.[0]).toMatchObject({
      turnId: "t_rho_peer_1",
      user: "帮我分析下 Q3 营收的同比变化",
      answer: "Q3 营收同比 -8.4%；主要拖累集中在江苏外套。",
    });
  });
});
