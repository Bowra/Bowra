/**
 * chat-redesign p0-followup F3 (audit P1-4 / R13) — stagger collapse
 * 5s user interaction guard.
 *
 * Pins: TurnContainer's `turnComplete` 600ms grace-then-collapse useEffect
 * is gated on a `lastInteractionAt` ref. Passive hover / pointer movement /
 * wheel delays the fold for a quiet window; pointerdown, focus, or a real
 * disclosure scroll records explicit inspection across archive remount.
 *
 * Strategy: fake timers; seed a real card+step; advance 600ms and assert
 * either StepSummary appears (no interaction) or stays hidden (interaction
 * within window).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render } from "@testing-library/react";

import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyCardFinalized,
  applyFinalAnswer,
  applyApprovalRequired,
  applyStepDone,
  applyStepStarted,
} from "@/lib/turn-store";
import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

import { TurnContainer } from "./TurnContainer";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  vi.useRealTimers();
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

function archivedTurn(over: Partial<ArchivedTurn> = {}): ArchivedTurn {
  return {
    turnId: "arch-1",
    user: "查询",
    userAttachments: [],
    answer: "最终回答",
    traceSteps: [],
    peersDispatched: [],
    errorReason: null,
    archivedAt: Date.now(),
    ...over,
  };
}

/** Seed a card with a single completed step so StepListCard renders + the
 *  collapse path is reachable. */
function seedCompletedStep(turnId: string): void {
  applyCardCreated({ turnId });
  applyStepStarted({
    turnId,
    stepId: "s1",
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询销售",
    startedAt: new Date(),
  });
  applyStepDone({
    turnId,
    stepId: "s1",
    state: "done",
    summary: "234 行 × 12 列",
    durationMs: 1200,
  });
  applyCardFinalized({ turnId, finalState: "completed" });
}

describe("TurnContainer F3 · stagger collapse 5s interaction guard", () => {
  beforeEach(() => {
    _resetAllTurnsForTesting();
  });

  it("turn done + no interaction → 600ms grace then folds (StepSummary appears)", async () => {
    vi.useFakeTimers();
    seedCompletedStep("t-f3-a");
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-f3-a",
          user: "查询",
          running: false,
        })}
      />,
    );

    // Before any timer fires the full step list shows.
    expect(container.querySelector(".bh-step-list")).toBeTruthy();
    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();

    // Advance through the 600ms grace; the recheck reads the ref → still 0
    // (no interaction) → folds immediately.
    await act(async () => {
      vi.advanceTimersByTime(700);
    });

    // After fold: step list gone, StepSummary present.
    expect(container.querySelector('[data-testid="step-summary"]')).toBeTruthy();
  });

  it("approval pending is not treated as a normal completion fold", async () => {
    vi.useFakeTimers();
    const turnId = "t-f3-approval";
    seedCompletedStep(turnId);
    applyApprovalRequired({ turnId, approvalId: "ap_1" });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId,
          user: "修改指标",
          running: false,
        })}
      />,
    );

    await act(async () => {
      vi.advanceTimersByTime(7000);
    });

    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();
    expect(container.querySelector(".bh-step-list")).toBeTruthy();
  });

  it("mouseenter at 500ms → 600ms grace fires but stays expanded", async () => {
    vi.useFakeTimers();
    seedCompletedStep("t-f3-b");
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-f3-b",
          user: "查询",
          running: false,
        })}
      />,
    );

    // Walk to 500ms then mouseenter the interaction region.
    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    const disclosure = container.querySelector(".bh-step-list") as HTMLElement;
    expect(disclosure).toBeTruthy();
    fireEvent.mouseOver(disclosure);

    // Now advance past the original 600ms — collapse timer fires, reads
    // ref freshly touched, defers fold.
    await act(async () => {
      vi.advanceTimersByTime(200);
    });

    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();
    expect(container.querySelector(".bh-step-list")).toBeTruthy();
  });

  it("after interaction, ~5s of quiet eventually triggers fold", async () => {
    vi.useFakeTimers();
    seedCompletedStep("t-f3-c");
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-f3-c",
          user: "查询",
          running: false,
        })}
      />,
    );

    // Interact almost immediately.
    const disclosure = container.querySelector(".bh-step-list") as HTMLElement;
    fireEvent.mouseOver(disclosure);

    // 600ms grace + retry attempts happen, none should fold.
    await act(async () => {
      vi.advanceTimersByTime(800);
    });
    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();

    // Now wait 6 more seconds of quiet — the rescheduled timer should see
    // a stale interaction (>5s) and fold.
    await act(async () => {
      vi.advanceTimersByTime(6500);
    });
    expect(container.querySelector('[data-testid="step-summary"]')).toBeTruthy();
  });

  it("does not treat interaction with the final answer as process inspection", async () => {
    vi.useFakeTimers();
    const turnId = "t-f3-answer-interaction";
    seedCompletedStep(turnId);
    applyFinalAnswer({ turnId, text: "最终回答" });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId,
          user: "查询",
          answer: "最终回答",
          running: false,
        })}
      />,
    );

    const answerNode = Array.from(container.querySelectorAll("p")).find(
      (node) => node.textContent === "最终回答",
    );
    expect(answerNode).toBeTruthy();
    fireEvent.pointerDown(answerNode!);

    await act(async () => {
      vi.advanceTimersByTime(700);
    });

    expect(container.querySelector('[data-testid="step-summary"]')).toBeTruthy();
  });

  it("treats a page wheel over the running trace as passive across current-to-archive", async () => {
    const turnId = "t-f3-passive-wheel-archive";
    applyCardCreated({ turnId });
    applyStepStarted({
      turnId,
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询销售",
      startedAt: new Date(),
    });
    applyStepDone({
      turnId,
      stepId: "s1",
      state: "done",
      summary: "234 行 × 12 列",
      durationMs: 1200,
    });
    applyFinalAnswer({ turnId, text: "最终回答" });

    const { container, rerender } = render(
      <TurnContainer
        runner={runner({
          turnId,
          user: "查询",
          answer: "最终回答",
          running: true,
        })}
      />,
    );

    // chat-ux-2026-08: running-state disclosure is the 1e band (`.bh-band` is in
    // `eventTargetsDisclosure`'s list). **Guard logic is unchanged**; only its running-state carrier
    // changed: moving capability ≠ losing capability (docs/单元测试规范.md).
    // ⚠ Original `.bh-step-list` target is still guarded by this file's `running: false` cases
    // (hover delays autocollapse; collapse only after 5s silence); do not move those too.
    fireEvent.wheel(container.querySelector('[data-testid="running-band"]') as HTMLElement);

    await act(async () => {
      applyCardFinalized({ turnId, finalState: "completed" });
    });
    rerender(
      <TurnContainer
        runner={runner({
          archivedTurns: [archivedTurn({ turnId })],
        })}
      />,
    );

    const archived = container.querySelector('.bh-bot-block[data-archived="true"]');
    expect(archived?.querySelector('[data-testid="step-summary"]')).toBeTruthy();
    expect(archived?.querySelector(".bh-step-list")).toBeNull();
  });

  it("explicit inspection keeps the completed disclosure expanded at its original position", async () => {
    vi.useFakeTimers();
    const turnId = "t-f3-explicit";
    applyCardCreated({ turnId });
    applyStepStarted({
      turnId,
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询销售",
      startedAt: new Date(),
    });
    applyStepDone({
      turnId,
      stepId: "s1",
      state: "done",
      summary: "234 行 × 12 列",
      durationMs: 1200,
    });
    applyFinalAnswer({ turnId, text: "最终回答" });

    const { container, rerender } = render(
      <TurnContainer
        runner={runner({
          turnId,
          user: "查询",
          answer: "最终回答",
          running: true,
        })}
      />,
    );

    // chat-ux-2026-08: running-state disclosure is the 1e band (`.bh-band` is in
    // `eventTargetsDisclosure`'s list). **Guard logic is unchanged**; only its running-state carrier
    // changed: moving capability ≠ losing capability (docs/单元测试规范.md).
    // ⚠ Original `.bh-step-list` target is still guarded by this file's `running: false` cases
    // (hover delays autocollapse; collapse only after 5s silence); do not move those too.
    const disclosureRegion = container.querySelector('[data-testid="running-band"]') as HTMLElement;
    fireEvent.pointerDown(disclosureRegion);

    await act(async () => {
      applyCardFinalized({ turnId, finalState: "completed" });
    });
    rerender(
      <TurnContainer
        runner={runner({
          turnId,
          user: "查询",
          answer: "最终回答",
          running: false,
        })}
      />,
    );

    await act(async () => {
      vi.advanceTimersByTime(7000);
    });

    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();
    const disclosure = container.querySelector(".bh-step-list");
    const answerNode = Array.from(container.querySelectorAll("p")).find(
      (node) => node.textContent === "最终回答",
    );
    expect(disclosure).toBeTruthy();
    expect(answerNode).toBeTruthy();
    expect(
      disclosure!.compareDocumentPosition(answerNode!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("preserves explicit inspection when done(completed) remounts the turn as archived", async () => {
    vi.useFakeTimers();
    const turnId = "t-f3-explicit-archive";
    applyCardCreated({ turnId });
    applyStepStarted({
      turnId,
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询销售",
      startedAt: new Date(),
    });
    applyStepDone({
      turnId,
      stepId: "s1",
      state: "done",
      summary: "234 行 × 12 列",
      durationMs: 1200,
    });
    applyFinalAnswer({ turnId, text: "最终回答" });

    const { container, rerender } = render(
      <TurnContainer
        runner={runner({
          turnId,
          user: "查询",
          answer: "最终回答",
          running: true,
        })}
      />,
    );

    // chat-ux-2026-08: running-state disclosure is the 1e band (`.bh-band` is in
    // `eventTargetsDisclosure`'s list). **Guard logic is unchanged**; only its running-state carrier
    // changed: moving capability ≠ losing capability (docs/单元测试规范.md).
    // ⚠ Original `.bh-step-list` target is still guarded by this file's `running: false` cases
    // (hover delays autocollapse; collapse only after 5s silence); do not move those too.
    fireEvent.pointerDown(container.querySelector('[data-testid="running-band"]') as HTMLElement);

    await act(async () => {
      applyCardFinalized({ turnId, finalState: "completed" });
    });
    rerender(
      <TurnContainer
        runner={runner({
          archivedTurns: [archivedTurn({ turnId })],
        })}
      />,
    );

    await act(async () => {
      vi.advanceTimersByTime(7000);
    });

    const archived = container.querySelector('.bh-bot-block[data-archived="true"]');
    const disclosure = archived?.querySelector(".bh-step-list");
    const answerNode = Array.from(archived?.querySelectorAll("p") ?? []).find(
      (node) => node.textContent === "最终回答",
    );
    expect(archived?.querySelector('[data-testid="step-summary"]')).toBeNull();
    expect(disclosure).toBeTruthy();
    expect(answerNode).toBeTruthy();
    expect(
      disclosure!.compareDocumentPosition(answerNode!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
