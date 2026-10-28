/**
 * chat-ux-2026-08 batch F — visibility threshold for solidifying the entire conversation.
 *
 * TurnContainer's comment **declares its own** intent:
 *   “Show when **more than one turn** in the session has completed: for a single-turn session it is equivalent to that turn's Solidify This Workflow,
 *     so an extra action is just noise.”
 * Yet the condition is `priorAnalysisTurnCount > 0` (= at least one turn). Thus a single-turn session displays two
 * almost-equivalent solidify buttons together (observed on 116: one on each side, with different styles).
 *
 * The code did not implement its declared intent—this test locks that intent.
 * Mutation check: change `> 1` back to `> 0`; the first case must fail.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllTurnsForTesting } from "@/lib/turn-store";
import { _clearAllToasts } from "@/lib/use-toast";
import type { TurnRunnerState, ArchivedTurn } from "@/lib/types";

import { TurnContainer } from "./TurnContainer";

beforeEach(() => {
  _resetAllTurnsForTesting();
  _clearAllToasts();
});
afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  _clearAllToasts();
  vi.restoreAllMocks();
});

function archived(n: number): ArchivedTurn[] {
  return Array.from({ length: n }, (_, i) => ({
    turnId: `t_${i}`,
    terminalStatus: "completed",
    user: `问题 ${i}`,
    userAttachments: [],
    answer: `答案 ${i}`,
    traceSteps: [],
    peersDispatched: [],
    errorReason: null,
    archivedAt: 1_700_000_000_000 + i * 60_000,
  })) as ArchivedTurn[];
}

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
  return {
    user: null, intent: null, complexity: null, chips: [], answer: "",
    artifact: null, source: null, summary: null, traceSteps: [],
    turnId: null, sessionId: "s_1", sessionTitle: null,
    running: false, started: true, messages: [], userAttachments: [],
    thinking: "", thinkingLength: 0, lastThinkingHintAttachedAt: "",
    clarifications: [], createToolOpen: null, subagentCreatorOpen: null,
    solidifyOpen: null, plan: null,
    ...over,
  } as TurnRunnerState;
}

function mount(turns: number) {
  return renderIntl(
    <TurnContainer
      runner={runner({ archivedTurns: archived(turns) } as Partial<TurnRunnerState>)}
      onSolidifySession={() => {}}
    />,
  );
}

describe("「固化整段对话」阈值 — 实现它自己声明的意图", () => {
  it("★ 只有一轮时**不**出现（与那轮的「固化此流程」等价，多一个只是噪声）", () => {
    mount(1);
    expect(screen.queryByTestId("solidify-session-chip")).toBeNull();
  });

  it("两轮及以上才出现（此时它才真有独立语义：固化整段而非某一轮）", () => {
    mount(2);
    expect(screen.getByTestId("solidify-session-chip")).toBeTruthy();
  });

  it("零轮不出现", () => {
    mount(0);
    expect(screen.queryByTestId("solidify-session-chip")).toBeNull();
  });

  it("★ 1 归档 + 当前轮已完成 = 真的两轮，要出（阈值不能只数归档轮）", () => {
    // Lock the first implementation's mistake: `priorAnalysisTurnCount` **excludes** the current turn;
    // using it as the threshold mistakes a just-answered second question for a single-turn session.
    renderIntl(
      <TurnContainer
        runner={
          runner({
            turnId: "t_now",
            user: "第二个问题",
            answer: "答案",
            terminalStatus: "completed",
            archivedTurns: archived(1),
          } as Partial<TurnRunnerState>)
        }
        onSolidifySession={() => {}}
      />,
    );
    expect(screen.getByTestId("solidify-session-chip")).toBeTruthy();
  });
});
