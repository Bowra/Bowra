/**
 * Failed-step UI contract tests — no buttons.
 *
 * History (chat-redesign p0-followup F1): these once pinned retry/skip buttons → POST
 * /retry_step /skip_step. Both endpoints were 202-ack stubs (the backend never reran steps,
 * and FE never listened for ack events); clicks never did anything (users confirmed this). O5 (2026-07-03)
 * removed all retry/skip buttons; Feishu feedback 07.06 #2 (2026-07-07) also removed the dead「知道了」
 * (onAck was always a no-op). These tests retain the buttonless failure explanation
 * without predicting retry activity from a historical transient classification:
 *  · ended tasks preserve the failure reason without current-running guidance
 *  · clicking any visible element never fetches /retry_step or /skip_step
 * If real re-dispatch is wired in later (through entitlement gate), rewrite this file when restoring buttons
 * (see chat-pipeline.md).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyStepStarted,
  applyStepDone,
} from "@/lib/turn-store";
import { _clearAllToasts } from "@/lib/use-toast";
import type { TurnRunnerState } from "@/lib/types";

import { TurnContainer } from "./TurnContainer";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  _clearAllToasts();
  vi.restoreAllMocks();
});

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
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

const WALLCLOCK_REASON = "本轮处理时间已达到上限。可以缩小范围，或拆成几个请求分别处理。";

function seedFailedStep(turnId: string, stepId: string): void {
  applyCardCreated({ turnId });
  applyStepStarted({
    turnId,
    stepId,
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询",
    startedAt: new Date(),
  });
  applyStepDone({
    turnId,
    stepId,
    state: "failed",
    durationMs: 1200,
    // Since 7162e70ee a step's own message prose is never shown; only finite codes explain the failure.
    error: {
      kind: "transient",
      code: "wallclock_exceeded",
      message: "查询超时",
    },
  });
}

describe("TurnContainer failure history", () => {
  beforeEach(() => {
    _resetAllTurnsForTesting();
    _clearAllToasts();
  });

  it.each(["failed", "cancelled", "clarification_pending", "completed"])("preserves the failure without active guidance after %s", (terminalStatus) => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", { status: 200 }),
    );

    seedFailedStep("t-f1", "s1");
    renderIntl(
      <TurnContainer
        runner={runner({
          turnId: "t-f1",
          user: "查询",
          running: false,
          terminalStatus,
        })}
      />,
    );

    expect(screen.getByText(WALLCLOCK_REASON)).toBeTruthy();
    expect(screen.queryByText("查询超时")).toBeNull();
    expect(screen.queryByText(/任务仍在进行/)).toBeNull();
    expect(screen.queryByText(/AI 会自行调整后重试/)).toBeNull();
    // No buttons: dead「知道了」(Feishu 07.06 #2) or fake retry/skip (O5).
    expect(screen.queryByText("知道了")).toBeNull();
    expect(screen.queryByText("重试")).toBeNull();
    expect(screen.queryByText("跳过")).toBeNull();
    // No stub endpoint calls throughout rendering.
    const stubCalls = fetchMock.mock.calls.filter((c) =>
      /retry_step|skip_step/.test(String(c[0])),
    );
    expect(stubCalls.length).toBe(0);
  });

  it("retains the original reason when the current lifecycle is unknown", () => {
    seedFailedStep("t-f1b", "s2");
    renderIntl(
      <TurnContainer
        runner={runner({
          turnId: "t-f1b",
          user: "查询",
          running: false,
        })}
      />,
    );
    expect(screen.getByText(WALLCLOCK_REASON)).toBeTruthy();
    expect(screen.queryByText("查询超时")).toBeNull();
  });
});
