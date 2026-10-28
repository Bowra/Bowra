/**
 * chat-redesign p0-followup F2 — fast-path Lead<3s should not render
 * a step list card outer when steps are empty.
 *
 * PRD §4.1.7: a simple query with Lead<3s should have no step list card. Backend
 * (turn_runner.py:889) unconditionally emits `step_list_card_created` on
 * Lead start, so an in-flight refactor will eventually gate that — until
 * then this FE-side guard ensures the user-visible surface (US-1) does
 * not show a hollow scaffolding row for a turn that ended with prose only.
 *
 * Streaming turns (steps still in flight) keep the card path so the active
 * rail shows up.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render } from "@testing-library/react";

import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyCardFinalized,
  applyFinalAnswer,
  applyPeerName,
  applyPeerTerminal,
  applyStepStarted,
} from "@/lib/turn-store";
import type { TurnRunnerState } from "@/lib/types";

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

describe("TurnContainer F2 · fast-path empty step list gate", () => {
  beforeEach(() => _resetAllTurnsForTesting());

  it("turn done + empty steps → no .bh-step-list and no StepSummary", () => {
    // Simulate backend's unconditional step_list_card_created emit, then
    // a done(completed) without any actual steps.
    applyCardCreated({ turnId: "t-fastpath" });
    applyCardFinalized({ turnId: "t-fastpath", finalState: "completed" });

    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-fastpath",
          user: "你好",
          answer: "你好，很高兴见到你。",
          running: false,
        })}
      />,
    );

    expect(container.querySelector(".bh-step-list")).toBeNull();
    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();
  });

  it("turn done + empty steps → BotByline + direct answer still render", () => {
    applyCardCreated({ turnId: "t-direct" });
    applyCardFinalized({ turnId: "t-direct", finalState: "completed" });

    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-direct",
          user: "你好",
          answer: "嗨!",
          running: false,
        })}
      />,
    );

    // BotByline mounted (Phase 1.7-B; identity row).
    expect(container.querySelector('[data-testid="bot-byline"]')).toBeTruthy();
    // Direct answer prose visible.
    expect(container.textContent).toContain("嗨!");
  });

  it("peer-only turn with zero child steps folds before the answer after the normal grace", async () => {
    vi.useFakeTimers();
    const turnId = "t-peer-only";
    applyCardCreated({ turnId });
    applyPeerName(turnId, "peer_1", "compute", "检查营收异常", "数据专家");
    applyPeerTerminal({
      turnId,
      peerId: "peer_1",
      status: "succeeded",
      durationMs: 950,
    });
    applyCardFinalized({ turnId, finalState: "completed" });

    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId,
          user: "检查营收异常",
          answer: "结论：未发现异常。",
          running: false,
        })}
      />,
    );

    const ledger = container.querySelector(".bh-step-list");
    const answer = Array.from(container.querySelectorAll("p")).find(
      (node) => node.textContent === "结论：未发现异常。",
    );
    expect(ledger).toBeTruthy();
    expect(container.textContent).toContain("数据专家");
    expect(answer).toBeTruthy();
    expect(
      Boolean(ledger!.compareDocumentPosition(answer!) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();

    await act(async () => {
      vi.advanceTimersByTime(700);
    });

    const summary = container.querySelector('[data-testid="step-summary"]');
    expect(summary).toBeTruthy();
    expect(summary?.textContent).toContain("execution_done");
    expect(
      Boolean(summary!.compareDocumentPosition(answer!) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
  });

  it("F5/archived peer-only turn restores the folded disclosure before the answer", () => {
    const turnId = "t-peer-only-archived";
    applyCardCreated({ turnId });
    applyPeerName(turnId, "peer_1", "compute", "检查营收异常", "数据专家");
    applyPeerTerminal({
      turnId,
      peerId: "peer_1",
      status: "succeeded",
      durationMs: 950,
    });
    applyFinalAnswer({ turnId, text: "归档结论：未发现异常。" });
    applyCardFinalized({ turnId, finalState: "completed" });

    const { container } = render(
      <TurnContainer
        runner={runner({
          archivedTurns: [{
            turnId,
            user: "检查营收异常",
            userAttachments: [],
            answer: "归档结论：未发现异常。",
            traceSteps: [],
            peersDispatched: [],
            errorReason: null,
            archivedAt: Date.now(),
          }],
        })}
      />,
    );

    const archived = container.querySelector('.bh-bot-block[data-archived="true"]');
    const summary = archived?.querySelector('[data-testid="step-summary"]');
    const answer = Array.from(archived?.querySelectorAll("p") ?? []).find(
      (node) => node.textContent === "归档结论：未发现异常。",
    );
    expect(summary).toBeTruthy();
    expect(summary?.textContent).toContain("execution_done");
    expect(answer).toBeTruthy();
    expect(
      Boolean(summary!.compareDocumentPosition(answer!) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
    expect(archived?.querySelector(".bh-step-list")).toBeNull();
  });

  it("streaming turn (running=true) WITH steps still shows step list", () => {
    applyCardCreated({ turnId: "t-stream" });
    applyStepStarted({
      turnId: "t-stream",
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询",
      startedAt: new Date(),
    });

    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-stream",
          user: "查一下销售",
          running: true,
        })}
      />,
    );

    // chat-ux-2026-08 · 1e scrolling band: the band is the primary running surface, rather than the whole expanded trace.
    // ⚠ Do **not** weaken this assertion to merely require the band; that would lose access to steps while running.
    //   Pin both the band's presence and one-click access to the full list.
    expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();
    expect(container.querySelector(".bh-step-list")).toBeNull();
    fireEvent.click(
      container.querySelector('[data-testid="running-band-bar"]') as HTMLElement,
    );
    expect(container.querySelector(".bh-step-list")).toBeTruthy();
  });

  it("streaming turn (running=true) with no steps yet keeps card mount path "
    + "(placeholder for incoming steps)", () => {
    applyCardCreated({ turnId: "t-stream-empty" });
    // No steps emitted yet; running=true → don't kill the card path.
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-stream-empty",
          user: "查一下销售",
          running: true,
        })}
      />,
    );

    // Do not show a StepSummary saying "0 步完成".
    // ⚠ Do **not** add a `.bh-step-list` null assertion here: when `showWorking` is true, the entire
    //   ternary returns null early, so that assertion always passes even without the guard (verified experimentally).
    //   The next case's fixture truly pins the empty-shell guard (streaming answer → showWorking false).
    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();
    // ★The opening gap's liveness signal moved from AssistantWorkingIndicator to the band's preparing skeleton
    //   (design §2-A: unrelated "处理中" / "思考中" / "开始处理" copy merges into "正在准备").
    //   **Both assertions must remain paired**: band present **and** old indicator absent. Removing the latter previously
    //   let simultaneous opening band + old spinner, with two role=status elements, pass every check until review.
    expect(
      container.querySelector('[data-testid="running-band"]')?.getAttribute("data-phase"),
    ).toBe("preparing");
    expect(container.querySelector('[data-testid="assistant-working-indicator"]')).toBeNull();
  });

  // ★Positive control: without it, the previous `assistant-working-indicator` null assertion always passes if
  //   the component is deleted entirely. Mutation: delete AssistantWorkingIndicator → this case must fail.
  it("positive control: with the band suppressed, the legacy working indicator is back", () => {
    // ⚠ Do not use pending clarification to suppress the band: it also suppresses showWorking,
    //   so neither renders and this positive control is useless (the first version made this mistake).
    //   Use a user's request for band details instead: it hides only the band, not the indicator.
    applyCardCreated({ turnId: "t-stream-empty-pc" });
    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-stream-empty-pc",
          user: "查一下销售",
          running: true,
        })}
      />,
    );
    expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();
    fireEvent.click(
      container.querySelector('[data-testid="running-band-bar"]') as HTMLElement,
    );
    // Yielding means the **three rows** give way to the full trace (retain the disclosure row as the collapse entry since 2026-08-15).
    expect(container.querySelector(".bh-band__now")).toBeNull();
    expect(container.querySelector('[data-testid="assistant-working-indicator"]')).toBeTruthy();
  });

  // chat-ux-2026-08 (invariant inventory §2.6): destination of the relocated empty-shell guard.
  //
  // This guard previously lived on `StepListCardConnected` itself (**a second independent data path**:
  // it read store via its own `useTurn(turnId)`, ignoring caller-supplied stepViews). That component was removed;
  // the no-beat branch now uses the same `TurnTraceRegion` as the primary branch. The guard moved to
  // `BeatStream.noBeatHasTraceFacts`, with its criterion unchanged verbatim:
  //   cardCreated && (steps.length > 0 || hasPeerDisclosureFacts)
  //
  // Deliberately choose a streaming-answer fixture: only it forces `showWorking` false,
  // truly reaching the guard. Mutation verified: hardcode the guard to true → this case fails.
  it("streaming answer with zero steps still renders no empty trace shell", () => {
    applyCardCreated({ turnId: "t-stream-answer" });

    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-stream-answer",
          user: "你好",
          answer: "你好，很高兴",
          running: true,
        })}
      />,
    );

    // runner.answer already disabled showWorking; the guard is this row's sole gatekeeper.
    expect(container.querySelector('[data-testid="assistant-working-indicator"]')).toBeNull();
    expect(container.querySelector(".bh-step-list")).toBeNull();
    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();
    // The answer still renders normally (do not let the guard swallow the entire branch).
    expect(container.textContent).toContain("你好，很高兴");
  });

  it("streaming turn with a first step hides the lightweight working indicator", () => {
    applyCardCreated({ turnId: "t-stream-started" });
    applyStepStarted({
      turnId: "t-stream-started",
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询",
      startedAt: new Date(),
    });

    const { container } = render(
      <TurnContainer
        runner={runner({
          turnId: "t-stream-started",
          user: "查一下销售",
          running: true,
        })}
      />,
    );

    expect(container.querySelector('[data-testid="assistant-working-indicator"]')).toBeNull();
    // After the first step arrives, the band enters running state (the band owns the step row).
    expect(
      container.querySelector('[data-testid="running-band"]')?.getAttribute("data-phase"),
    ).toBe("running");
  });
});
