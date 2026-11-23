/**
 * Turn visibility FR12: the finished line counts what the Turn produced, read from the stored answer
 * (`lead.final.payload.outputs`).
 *
 * A live Turn remounts as `ArchivedTurnView` on done(completed), and every earlier Turn of a session renders
 * there too. On 116 (1.7.63) that view never received the counts, so the line read
 * "执行已结束 · 调用 1 个智能体 · 2 分 41 秒" live and for every earlier Turn, and showed "1 份演示文稿" only for
 * the newest Turn after a reload. Mutation: drop `outputs` from the archived StepSummary → this test fails.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyCardFinalized,
  applyFinalAnswer,
  applyStepDone,
  applyStepStarted,
} from "@/lib/turn-store";
import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

import { TurnContainer } from "./TurnContainer";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
});

const FINAL_PAYLOAD = { outputs: [{ kind: "presentation", count: 1 }] };

function runner(over: Partial<TurnRunnerState>): TurnRunnerState {
  return {
    user: null,
    intent: null,
    complexity: null,
    chips: [],
    answer: "",
    artifact: null,
    source: null,
    summary: null,
    traceSteps: [],
    turnId: null,
    sessionId: null,
    sessionTitle: null,
    running: false,
    started: false,
    messages: [],
    userAttachments: [],
    thinking: "",
    thinkingLength: 0,
    lastThinkingHintAttachedAt: "",
    clarifications: [],
    createToolOpen: null,
    subagentCreatorOpen: null,
    solidifyOpen: null,
    plan: null,
    ...over,
  };
}

function seedFinishedTurn(turnId: string): void {
  applyCardCreated({ turnId });
  applyStepStarted({ turnId, stepId: "s1", stepType: "tool_run", toolName: "python_sandbox", headerZh: "运行代码", startedAt: new Date() });
  applyStepDone({ turnId, stepId: "s1", state: "done", summary: "运行成功", durationMs: 1200 });
  applyFinalAnswer({ turnId, text: "PPT 已生成。" });
  applyCardFinalized({ turnId, finalState: "completed" });
}

describe("Turn visibility FR12: an archived Turn's finished line", () => {
  it("names what the Turn produced, as the stored answer counts it", () => {
    seedFinishedTurn("t-out");
    const archived = {
      turnId: "t-out",
      terminalStatus: "completed",
      user: "做一份 PPT",
      userAttachments: [],
      answer: "PPT 已生成。",
      traceSteps: [],
      peersDispatched: [],
      errorReason: null,
      endedAt: Date.now(),
      finalPayload: FINAL_PAYLOAD,
    } as ArchivedTurn;
    const { container } = renderIntl(<TurnContainer runner={runner({ archivedTurns: [archived] } as Partial<TurnRunnerState>)} />);
    const summary = container.querySelector('[data-testid="step-summary"]');
    expect(summary?.textContent).toContain("1 份演示文稿");
  });
});
