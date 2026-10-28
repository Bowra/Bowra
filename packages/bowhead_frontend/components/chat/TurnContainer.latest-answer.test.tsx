/**
 * Chat workbench batch 2: always show the latest answer's action bar.
 * Criterion: use the current turn if completed, otherwise the last normally completed archived turn (failed / aborted turns have no action bar).
 * Use the same criterion live and after F5; the F5 shape is a completed current turn preceded by archived turns.
 */
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));

import { latestAnsweredArchivedIndex, TurnContainer } from "./TurnContainer";

afterEach(cleanup);

function archived(turnId: string, status: "completed" | "failed"): ArchivedTurn {
  return {
    turnId,
    terminalStatus: status,
    user: `问题 ${turnId}`,
    userAttachments: [],
    answer: `回答 ${turnId}`,
    traceSteps: [],
    peersDispatched: [],
    errorReason: status === "failed" ? { source: "turn_failed", reasonCode: "lead_internal_error" } : null,
    archivedAt: Date.parse("2026-09-29T02:00:00Z"),
  } as ArchivedTurn;
}

function runner(over: Partial<TurnRunnerState>): TurnRunnerState {
  return {
    user: "当前问题", intent: null, complexity: null, chips: [], answer: "当前回答", artifact: null, source: null,
    summary: null, traceSteps: [], turnId: "t_now", sessionId: "s1", sessionTitle: null,
    running: false, started: true, messages: [], userAttachments: [], thinking: "", thinkingLength: 0,
    lastThinkingHintAttachedAt: "", clarifications: [], createToolOpen: null, subagentCreatorOpen: null,
    solidifyOpen: null, plan: null, errorReason: null, terminalStatus: "completed",
    ...over,
  } as TurnRunnerState;
}

const latestBars = (container: HTMLElement) => container.querySelectorAll('.bh-msg-actions[data-latest="1"]');

it("F5 形态：当前轮已完成、前面有归档轮 ⇒ 只有当前轮这一条常显", () => {
  const { container } = render(
    <TurnContainer
      runner={runner({ archivedTurns: [archived("t1", "completed"), archived("t2", "completed")] })}
      onSend={vi.fn()}
      onComposerPrefill={vi.fn()}
    />,
  );
  expect(container.querySelectorAll(".bh-msg-actions").length).toBe(3);
  const bars = latestBars(container);
  expect(bars).toHaveLength(1);
  expect(bars[0].closest('[data-archived="false"]')).not.toBeNull();
});

it("t1 完成、t2 失败、t3 运行中 ⇒ t1 的操作条常显", () => {
  const { container } = render(
    <TurnContainer
      runner={runner({ running: true, terminalStatus: null, answer: "", archivedTurns: [archived("t1", "completed"), archived("t2", "failed")] })}
      onSend={vi.fn()}
      onComposerPrefill={vi.fn()}
    />,
  );
  const bars = latestBars(container);
  expect(bars).toHaveLength(1);
  expect(latestAnsweredArchivedIndex([archived("t1", "completed"), archived("t2", "failed")], false)).toBe(0);
});

it("判据：当前轮完成时没有归档轮常显；否则取最后一个正常完成的归档轮；全都没完成时没有", () => {
  const list = [archived("t1", "completed"), archived("t2", "completed"), archived("t3", "failed")];
  expect(latestAnsweredArchivedIndex(list, true)).toBe(-1);
  expect(latestAnsweredArchivedIndex(list, false)).toBe(1);
  expect(latestAnsweredArchivedIndex([archived("t1", "failed")], false)).toBe(-1);
});
