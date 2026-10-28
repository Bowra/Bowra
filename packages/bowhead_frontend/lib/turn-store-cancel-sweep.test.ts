/**
 * Terminal cancellation must stop every unfinished step indicator.
 *
 * Incident: user clicks Stop → backend `_execute_tool` (subagent.py:4089) **intentionally** re-raises
 * `CancelledError` without supplying `tool_result`/`step_done`; frontend only moves steps to terminal state via `applyStepDone`,
 * requiring an exact stepId match ⇒ the step running at Stop (usually the longest,
 * `subagent_spawn`) stays `active` forever, leaving the card spinning.
 *
 * Fix in the frontend: it is the only point covering live (Stop) / disconnection / replay together
 * —backend extra frames cannot fix the latter two (the replayed persisted stream never contained that frame).
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyCardFinalized,
  applyStepDone,
  applyStepStarted,
  getTurnSnapshot,
} from "./turn-store";

const T = "turn-cancel-sweep";

function startStep(stepId: string) {
  applyStepStarted({
    turnId: T,
    stepId,
    stepType: "tool_run",
    toolName: "subagent_spawn",
    headerZh: "派子 agent",
  });
}

describe("applyCardFinalized 收尾未结束的步骤", () => {
  afterEach(() => {
    _resetAllTurnsForTesting();
  });

  it("用户点停止 ⇒ 还在跑的步骤变 cancelled，不再转圈", () => {
    applyCardCreated({ turnId: T });
    startStep("spawn-1");
    expect(getTurnSnapshot(T)?.steps[0].state).toBe("active");

    applyCardFinalized({ turnId: T, finalState: "cancelled" });

    expect(getTurnSnapshot(T)?.steps[0].state).toBe("cancelled");
  });

  it("★ 正对照：轮次是 completed/failed 收的 ⇒ 未跑完的步骤是 failed，不是 cancelled", () => {
    // Without this, a bad implementation blindly marking every unfinished step cancelled would also pass—
    // misrepresenting genuine failures as user stops.
    applyCardCreated({ turnId: T });
    startStep("spawn-1");

    applyCardFinalized({ turnId: T, finalState: "failed" });

    expect(getTurnSnapshot(T)?.steps[0].state).toBe("failed");
  });

  it("已经终态的步骤不被改写（终态单调）", () => {
    applyCardCreated({ turnId: T });
    startStep("spawn-1");
    applyStepDone({ turnId: T, stepId: "spawn-1", state: "done" });

    applyCardFinalized({ turnId: T, finalState: "cancelled" });

    expect(getTurnSnapshot(T)?.steps[0].state).toBe("done");
  });

  it("只收尾未结束的那些，已完成的保持原样", () => {
    applyCardCreated({ turnId: T });
    startStep("done-1");
    applyStepDone({ turnId: T, stepId: "done-1", state: "done" });
    startStep("running-1");

    applyCardFinalized({ turnId: T, finalState: "cancelled" });

    const steps = getTurnSnapshot(T)?.steps ?? [];
    expect(steps.map((s) => s.state)).toEqual(["done", "cancelled"]);
  });
});
