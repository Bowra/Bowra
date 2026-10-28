/**
 * Phase 1.7-B — TurnContainer composition test.
 *
 * Pins the DOM contract that 1.7-B asserts (tech-design §4.8):
 *   · BotByline renders ABOVE first StepRow in DOM order
 *   · No `<section className="step-list-card">` chrome
 *   · No legacy `.chat-panel / .tline / .ub / .bub / .ab` classes
 *   · UserMessage no bubble background (plain-text path)
 *   · Live + archived turns render identical bot-block structure
 */

import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, waitFor, within } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyCardFinalized,
  applyFinalAnswer,
  applyNarrativeAppend,
  applyStepDone,
  applyStepStarted,
} from "@/lib/turn-store";
import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

import { clarificationMultiFallbackText, TurnContainer } from "./TurnContainer";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  vi.useRealTimers();
});

function emptyRunner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
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
    terminalStatus: "completed",
    user: "上一次问的问题",
    userAttachments: [],
    answer: "上一次的答案。",
    traceSteps: [],
    peersDispatched: [],
    errorReason: null,
    archivedAt: Date.now(),
    ...over,
  };
}

function seedCompletedTurnWithAnswer(turnId: string, answer: string): void {
  applyCardCreated({ turnId });
  applyStepStarted({
    turnId,
    stepId: "s1",
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询销售",
    startedAt: new Date("2026-05-22T14:32:00Z"),
  });
  applyStepDone({
    turnId,
    stepId: "s1",
    state: "done",
    summary: "234 行",
    durationMs: 1200,
  });
  applyFinalAnswer({ turnId, text: answer });
  applyCardFinalized({ turnId, finalState: "completed" });
}

function expectBefore(first: Element, second: Element): void {
  const position = first.compareDocumentPosition(second);
  expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}

function ResumeDisclosureHarness({
  turnId,
  answer,
}: {
  turnId: string;
  answer: string;
}) {
  const [running, setRunning] = useState(false);
  const [archived, setArchived] = useState(false);
  return (
    <>
      <button
        type="button"
        data-testid="resume-turn"
        onClick={() => {
          dispatchToTurnStore("turn.resumed", {
            turn_id: turnId,
            request_id: "clar_1",
            seq: 99,
          });
          setRunning(true);
        }}
      >
        resume
      </button>
      <button
        type="button"
        data-testid="complete-turn"
        onClick={() => {
          applyCardFinalized({ turnId, finalState: "completed" });
          setArchived(true);
        }}
      >
        complete
      </button>
      <TurnContainer
        runner={
          archived
            ? emptyRunner({
                archivedTurns: [archivedTurn({ turnId, answer })],
              })
            : emptyRunner({
                turnId,
                user: "查一下",
                answer,
                started: true,
                running,
              })
        }
      />
    </>
  );
}

it.each(["", "/bowhead", "/ai/apps/bowhead"])("downloads sent attachments on browser entry %s", async (prefix) => {
  window.__BOWHEAD_BASE_PATH__ = prefix;
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  try {
    const { container } = renderIntl(<TurnContainer runner={emptyRunner({
      turnId: "t-download", user: "Review this file", running: true,
      userAttachments: [{ fileId: "art_sent", filename: "large.csv", mime: "text/csv", size: 20 * 1024 * 1024 }],
    })} />);
    fireEvent.click(container.querySelector(".ub-attachments .v2-chip")!);
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    const anchor = click.mock.contexts[0] as HTMLAnchorElement;
    expect(anchor.getAttribute("href")).toBe(`${prefix}/api/v1/files/art_sent`);
  } finally {
    delete window.__BOWHEAD_BASE_PATH__;
    click.mockRestore();
  }
});

describe("TurnContainer composition (Phase 1.7-B)", () => {
  it("renders a failure banner without verified chrome for a wire-terminal error", () => {
    const rendered = renderIntl(
      <TurnContainer
        runner={emptyRunner({
          turnId: "t_dispatch_failed",
          user: "请开始分析",
          started: true,
          running: false,
          errorReason: {
            source: "turn_failed",
            reasonCode: "lead_internal_error",
          },
        })}
      />,
    );

    expect(rendered.container.querySelector('[data-testid="final-fail"]')).not.toBeNull();
    expect(rendered.container.textContent).not.toContain("verified");
  });

  describe("clarification fallback text", () => {
    it("renders per-question skip as natural language instead of raw sentinel", () => {
      const fallback = clarificationMultiFallbackText(
        {
          values: [
            "主要看哪个区域?: 华中",
            "重点看哪些指标?: __bowhead_question_skip__",
          ],
          answers: [
            { question: "主要看哪个区域?", source: "custom", answer: "华中" },
            { question: "重点看哪些指标?", source: "skip", skipped: true },
          ],
        },
        "请根据上下文自行判断",
      );

      expect(fallback).toContain("主要看哪个区域?: 华中");
      expect(fallback).toContain("重点看哪些指标?: 请根据上下文自行判断");
      expect(fallback).not.toContain("__bowhead_question_skip__");
    });
  });

  describe("no legacy class names", () => {
    it("does not synthesize Debate or Judge from Lead plus one peer", () => {
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-r0-03",
            user: "从多个角度分析",
            answer: "这是分析结果。",
            started: true,
            running: false,
            peersDispatched: [
              {
                peerName: "lead",
                subagentId: "sub_lead",
                dispatchedAt: 1,
                parentCtxSummary: "牵头",
              },
              {
                peerName: "compute",
                subagentId: "sub_compute",
                dispatchedAt: 2,
                parentCtxSummary: "计算",
              },
            ],
          })}
        />,
      );

      expect(container.querySelector(".rho-debate-block")).toBeNull();
      expect(container.querySelector(".rho-judge-chip")).toBeNull();
      expect(container.textContent).not.toContain("judge · done");
    });

    it("does not render .chat-panel", () => {
      const { container } = renderIntl(
        <TurnContainer runner={emptyRunner()} />,
      );
      expect(container.querySelector(".chat-panel")).toBeNull();
    });

    it("does not render .tline", () => {
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t1",
            user: "查一下",
            running: true,
          })}
        />,
      );
      expect(container.querySelector(".tline")).toBeNull();
    });

    it("does not render .ub or .bub", () => {
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t1",
            user: "查一下",
            running: true,
          })}
        />,
      );
      expect(container.querySelector(".ub")).toBeNull();
      expect(container.querySelector(".bub")).toBeNull();
    });

    it("does not render .ab", () => {
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t1",
            user: "查一下",
            running: true,
          })}
        />,
      );
      expect(container.querySelector(".ab")).toBeNull();
    });

    it("does not render `section.step-list-card` wrapper", () => {
      // Drive a current turn with a step beat — StepListCard renders, but
      // the chrome `<section>` wrapper is stripped in 1.7-B.
      applyCardCreated({ turnId: "t-section" });
      applyStepStarted({
        turnId: "t-section",
        stepId: "s1",
        stepType: "tool_run",
        toolName: "bi_query",
        headerZh: "查询",
        startedAt: new Date("2026-05-22T14:32:00Z"),
      });
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-section",
            user: "查一下",
            running: true,
          })}
        />,
      );
      expect(container.querySelector("section.step-list-card")).toBeNull();
      expect(container.querySelector("section[data-card-id]")).toBeNull();
    });
  });

  describe("chat-content-frame outer wrapper", () => {
    it("wraps everything in .chat-content-frame (replaces .chat-panel)", () => {
      const { container } = renderIntl(
        <TurnContainer runner={emptyRunner()} />,
      );
      expect(container.querySelector(".chat-content-frame")).toBeTruthy();
    });
  });

  describe("BotByline order", () => {
    it("BotByline precedes the first StepRow in DOM order", () => {
      applyCardCreated({ turnId: "t-order" });
      applyStepStarted({
        turnId: "t-order",
        stepId: "s1",
        stepType: "tool_run",
        toolName: "bi_query",
        headerZh: "查询销售",
        startedAt: new Date("2026-05-22T14:32:00Z"),
      });

      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-order",
            user: "看看销售",
            running: true,
            startedAt: 1735128720000,
          })}
        />,
      );

      const byline = container.querySelector('[data-testid="bot-byline"]');
      expect(byline).toBeTruthy();
      const stepList = container.querySelector('[aria-label="步骤列表"]');
      // Step list may or may not be present depending on the connect path;
      // when present, byline must precede it.
      if (stepList) {
        const pos = byline!.compareDocumentPosition(stepList);
        // DOCUMENT_POSITION_FOLLOWING = 4 → stepList comes AFTER byline.
        expect(pos & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      }
    });
  });

  describe("settled execution disclosure order", () => {
    beforeEach(() => _resetAllTurnsForTesting());

    it("live completed turn keeps the expanded disclosure before the final answer until it folds", () => {
      const turnId = "t-disclosure-live";
      const answer = "最终答案内容。";
      seedCompletedTurnWithAnswer(turnId, answer);

      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId,
            user: "查一下",
            answer,
            started: true,
            running: false,
          })}
        />,
      );

      const answerNode = Array.from(container.querySelectorAll("p")).find(
        (node) => node.textContent === answer,
      );
      const disclosure = container.querySelector(".bh-step-list");
      expect(answerNode).toBeTruthy();
      expect(disclosure).toBeTruthy();
      expectBefore(disclosure!, answerNode!);
    });

    it("live completed turn places the folded disclosure immediately before the final answer", async () => {
      vi.useFakeTimers();
      const turnId = "t-disclosure-live-folded";
      const answer = "折叠后的最终答案。";
      seedCompletedTurnWithAnswer(turnId, answer);

      let archiveCurrentTurn!: () => void;
      function Harness() {
        const [archived, setArchived] = useState(false);
        archiveCurrentTurn = () => setArchived(true);
        return (
          <TurnContainer
            runner={
              archived
                ? emptyRunner({
                    archivedTurns: [archivedTurn({ turnId, answer })],
                  })
                : emptyRunner({
                    turnId,
                    user: "查一下",
                    answer,
                    started: true,
                    running: false,
                  })
            }
          />
        );
      }
      const { container } = renderIntl(<Harness />);

      await act(async () => {
        vi.advanceTimersByTime(700);
      });

      const answerNode = Array.from(container.querySelectorAll("p")).find(
        (node) => node.textContent === answer,
      );
      const disclosure = container.querySelector('[data-testid="step-summary"]');
      expect(answerNode).toBeTruthy();
      expect(disclosure).toBeTruthy();
      expectBefore(disclosure!, answerNode!);

      fireEvent.pointerDown(disclosure!);
      fireEvent.click(disclosure!);
      const expandedDisclosure = container.querySelector(".bh-step-list");
      expect(expandedDisclosure).toBeTruthy();
      expectBefore(expandedDisclosure!, answerNode!);

      act(() => archiveCurrentTurn());
      const archived = container.querySelector('.bh-bot-block[data-archived="true"]');
      const archivedAnswer = Array.from(archived?.querySelectorAll("p") ?? []).find(
        (node) => node.textContent === answer,
      );
      const archivedDisclosure = archived?.querySelector(".bh-step-list");
      expect(archived?.querySelector('[data-testid="step-summary"]')).toBeNull();
      expect(archivedDisclosure).toBeTruthy();
      expect(archivedAnswer).toBeTruthy();
      expectBefore(archivedDisclosure!, archivedAnswer!);
    });

    it("archived turn renders its folded disclosure before the replayed answer", () => {
      const turnId = "t-disclosure-archived";
      const answer = "刷新后的最终答案。";
      seedCompletedTurnWithAnswer(turnId, answer);

      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            archivedTurns: [
              archivedTurn({
                turnId,
                answer,
              }),
            ],
          })}
        />,
      );

      const archived = container.querySelector('.bh-bot-block[data-archived="true"]');
      const answerNode = Array.from(archived?.querySelectorAll("p") ?? []).find(
        (node) => node.textContent === answer,
      );
      const disclosure = archived?.querySelector('[data-testid="step-summary"]');
      expect(answerNode).toBeTruthy();
      expect(disclosure).toBeTruthy();
      expectBefore(disclosure!, answerNode!);
    });

    it("keeps an archived completed trace open while the same turn awaits clarification", () => {
      const turnId = "t-disclosure-archived-awaiting";
      const answer = "我需要你再确认一个范围。";
      seedCompletedTurnWithAnswer(turnId, answer);

      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId,
            archivedTurns: [archivedTurn({ turnId, answer })],
            clarificationPending: {
              clarificationId: "clar_scope",
              candidates: ["近 30 天", "本季度"],
              fallbackText: "请选择分析范围",
            },
          })}
        />,
      );

      const archived = container.querySelector('.bh-bot-block[data-archived="true"]');
      const disclosure = archived?.querySelector(".bh-step-list");
      const answerNode = Array.from(archived?.querySelectorAll("p") ?? []).find(
        (node) => node.textContent === answer,
      );
      expect(archived?.querySelector('[data-testid="execution-disclosure"]')).toBeNull();
      expect(archived?.querySelector('[data-testid="step-summary"]')).toBeNull();
      expect(disclosure).toBeTruthy();
      expect(answerNode).toBeTruthy();
      expectBefore(disclosure!, answerNode!);
      expect(container.textContent).toContain("请选择分析范围");
    });

    it("places the folded disclosure before a legacy fallback answer without an answer beat", async () => {
      vi.useFakeTimers();
      const turnId = "t-disclosure-live-fallback";
      const answer = "这是旧数据路径的兜底答案。";
      applyCardCreated({ turnId });
      applyStepStarted({
        turnId,
        stepId: "s1",
        stepType: "tool_run",
        toolName: "bi_query",
        headerZh: "查询销售",
        startedAt: new Date("2026-05-22T14:32:00Z"),
      });
      applyStepDone({
        turnId,
        stepId: "s1",
        state: "done",
        summary: "234 行",
        durationMs: 1200,
      });
      applyCardFinalized({ turnId, finalState: "completed" });

      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId,
            user: "查一下",
            answer,
            started: true,
            running: false,
          })}
        />,
      );

      await act(async () => {
        vi.advanceTimersByTime(700);
      });

      const answerNode = Array.from(container.querySelectorAll("p")).find(
        (node) => node.textContent === answer,
      );
      const disclosure = container.querySelector('[data-testid="step-summary"]');
      expect(answerNode).toBeTruthy();
      expect(disclosure).toBeTruthy();
      expectBefore(disclosure!, answerNode!);
    });

    it("lets a durable live cancellation override a stale completed card", async () => {
      vi.useFakeTimers();
      const turnId = "t-disclosure-live-cancelled";
      const answer = "取消前已收到的文本。";
      applyCardCreated({ turnId });
      applyStepStarted({
        turnId,
        stepId: "s1",
        stepType: "tool_run",
        toolName: "bi_query",
        headerZh: "查询销售",
        startedAt: new Date("2026-05-22T14:32:00Z"),
      });
      applyStepDone({
        turnId,
        stepId: "s1",
        state: "failed",
        summary: "已取消",
        durationMs: 1200,
      });
      applyFinalAnswer({ turnId, text: answer });
      applyCardFinalized({ turnId, finalState: "completed" });

      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId,
            user: "查一下",
            answer,
            started: true,
            running: false,
            terminalStatus: "cancelled",
          })}
          onSolidify={vi.fn()}
        />,
      );

      await act(async () => {
        vi.advanceTimersByTime(700);
      });

      const answerNode = Array.from(container.querySelectorAll("p")).find(
        (node) => node.textContent === answer,
      );
      const disclosure = container.querySelector(".bh-step-list");
      expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();
      expect(answerNode).toBeTruthy();
      expect(disclosure).toBeTruthy();
      expectBefore(disclosure!, answerNode!);
      expect(container.textContent).not.toContain("verified");
      expect(container.textContent).not.toContain("固化此流程");
    });

    it("lets a durable archived cancellation override a stale completed card", () => {
      const turnId = "t-disclosure-archived-cancelled";
      const answer = "取消前已收到的文本。";
      applyCardCreated({ turnId });
      applyStepStarted({
        turnId,
        stepId: "s1",
        stepType: "tool_run",
        toolName: "bi_query",
        headerZh: "查询销售",
        startedAt: new Date("2026-05-22T14:32:00Z"),
      });
      applyStepDone({
        turnId,
        stepId: "s1",
        state: "failed",
        summary: "已取消",
        durationMs: 1200,
      });
      applyFinalAnswer({ turnId, text: answer });
      applyCardFinalized({ turnId, finalState: "completed" });

      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            archivedTurns: [
              archivedTurn({
                turnId,
                answer,
                terminalStatus: "cancelled",
              }),
            ],
          })}
          onSolidify={vi.fn()}
        />,
      );

      const archived = container.querySelector('.bh-bot-block[data-archived="true"]');
      const answerNode = Array.from(archived?.querySelectorAll("p") ?? []).find(
        (node) => node.textContent === answer,
      );
      const disclosure = archived?.querySelector(".bh-step-list");
      expect(archived?.querySelector('[data-testid="execution-disclosure"]')).toBeNull();
      expect(archived?.querySelector('[data-testid="step-summary"]')).toBeNull();
      expect(answerNode).toBeTruthy();
      expect(disclosure).toBeTruthy();
      expectBefore(disclosure!, answerNode!);
      expect(archived?.textContent).not.toContain("verified");
      expect(archived?.textContent).not.toContain("固化此流程");
    });

    it("same-turn HITL resume reopens a previously folded disclosure inline", async () => {
      vi.useFakeTimers();
      const turnId = "t-disclosure-resume";
      const answer = "澄清前的阶段性答案。";
      seedCompletedTurnWithAnswer(turnId, answer);

      const { container, getByTestId } = renderIntl(
        <ResumeDisclosureHarness turnId={turnId} answer={answer} />,
      );

      await act(async () => {
        vi.advanceTimersByTime(700);
      });
      expect(container.querySelector('[data-testid="step-summary"]')).toBeTruthy();

      fireEvent.click(getByTestId("resume-turn"));

      // chat-ux-2026-08: after resume, this turn is running again → the running disclosure surface is the 1e band.
      // The pinned invariant remains: **disclosure reopens in place, before the answer**; only its host element changes.
      // ⚠ This branch covers a user who **did not** explicitly inspect disclosure during clarification; by the user's decision
      //   (inventory §4 ③), the band takes over. The next case guards the opposite branch; keep them paired.
      expect(container.querySelector('[data-testid="execution-disclosure"]')).toBeNull();
      expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();
      const disclosure = container.querySelector('[data-testid="running-band"]');
      const answerNode = Array.from(container.querySelectorAll("p")).find(
        (node) => node.textContent === answer,
      );
      expect(disclosure).toBeTruthy();
      expect(answerNode).toBeTruthy();
      // ⚠ We **no longer** assert disclosure precedes the answer here. That old assertion was a **side effect** of beat order
      //   (step beat before answer beat), not a contract established by this sprint;
      //   the band sits outside the beat stream, last in this column in design 1e (prose/answer above, band below).
      //   Keeping the old assertion would treat an order never agreed upon as a regression.
      //   The actual contract is disclosure returning to **expanded** state after resume, pinned by the three assertions above.
      //   A dedicated case in `TurnContainer.running-band.test.tsx` guards band/prose ordering.
    });

    it("★ same-turn HITL resume keeps the trace when the reader was inspecting it", async () => {
      // User decision (inventory §4 ③): after resume, the band must not replace content a reader inspected during clarification.
      // The criterion is explicit inspection of disclosure, using the existing
      // `markExplicitDisclosureInspection` signal.
      //
      // ⚠ Do **not** reuse `explicitlyInspectedDisclosureRef`: the HITL resume
      //   effect clears it (it serves the auto-collapse timer). The band's yielding must survive resume,
      //   so it uses separate state reset by turnId. Mutation: also reset `traceRequested` in
      //   the resume effect → this case must fail.
      vi.useFakeTimers();
      const turnId = "t-disclosure-resume-inspected";
      const answer = "澄清前的阶段性答案。";
      seedCompletedTurnWithAnswer(turnId, answer);

      const { container, getByTestId } = renderIntl(
        <ResumeDisclosureHarness turnId={turnId} answer={answer} />,
      );

      // The user explicitly inspects disclosure before collapsing (pointerdown).
      const disclosureBefore = container.querySelector(".bh-step-list") as HTMLElement;
      expect(disclosureBefore).toBeTruthy();
      fireEvent.pointerDown(disclosureBefore);

      fireEvent.click(getByTestId("resume-turn"));

      expect(container.querySelector('[data-testid="running-band"]')).toBeNull();
      expect(container.querySelector(".bh-step-list")).toBeTruthy();
    });

    it("same-turn HITL resume clears stale before-answer expansion intent", async () => {
      vi.useFakeTimers();
      const turnId = "t-disclosure-resume-stale-intent";
      const answer = "澄清前的阶段性答案。";
      seedCompletedTurnWithAnswer(turnId, answer);

      const { container, getByTestId } = renderIntl(
        <ResumeDisclosureHarness turnId={turnId} answer={answer} />,
      );

      await act(async () => {
        vi.advanceTimersByTime(700);
      });
      const summary = container.querySelector('[data-testid="step-summary"]');
      expect(summary).toBeTruthy();
      fireEvent.pointerDown(summary!);
      fireEvent.click(summary!);
      expect(container.querySelector(".bh-step-list")).toBeTruthy();

      fireEvent.click(getByTestId("resume-turn"));
      expect(container.querySelector('[data-testid="execution-disclosure"]')).toBeNull();

      fireEvent.click(getByTestId("complete-turn"));
      const archived = container.querySelector('.bh-bot-block[data-archived="true"]');
      const archivedSummary = archived?.querySelector('[data-testid="step-summary"]');
      const archivedAnswer = Array.from(archived?.querySelectorAll("p") ?? []).find(
        (node) => node.textContent === answer,
      );
      expect(archivedSummary).toBeTruthy();
      expect(archivedAnswer).toBeTruthy();
      expectBefore(archivedSummary!, archivedAnswer!);
    });

    it("does not treat a local network give-up as settled before card finalization", () => {
      const turnId = "t-disclosure-not-finalized";
      const answer = "连接中断前已收到的文本。";
      applyCardCreated({ turnId });
      applyStepStarted({
        turnId,
        stepId: "s1",
        stepType: "tool_run",
        toolName: "bi_query",
        headerZh: "查询销售",
        startedAt: new Date("2026-05-22T14:32:00Z"),
      });
      applyStepDone({
        turnId,
        stepId: "s1",
        state: "done",
        summary: "234 行",
        durationMs: 1200,
      });
      applyFinalAnswer({ turnId, text: answer });

      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId,
            user: "查一下",
            answer,
            started: true,
            running: false,
            streamingState: "error",
          })}
        />,
      );

      expect(container.querySelector('[data-testid="execution-disclosure"]')).toBeNull();
      const disclosure = container.querySelector(".bh-step-list");
      const answerNode = Array.from(container.querySelectorAll("p")).find(
        (node) => node.textContent === answer,
      );
      expect(disclosure).toBeTruthy();
      expect(answerNode).toBeTruthy();
      expectBefore(disclosure!, answerNode!);
    });

    it("keeps an unfinalized archived trace inline instead of fabricating a settled tail", () => {
      const turnId = "t-disclosure-archived-unfinalized";
      const answer = "连接中断前已收到的文本。";
      applyCardCreated({ turnId });
      applyStepStarted({
        turnId,
        stepId: "s1",
        stepType: "tool_run",
        toolName: "bi_query",
        headerZh: "查询销售",
        startedAt: new Date("2026-05-22T14:32:00Z"),
      });
      applyStepDone({
        turnId,
        stepId: "s1",
        state: "done",
        summary: "234 行",
        durationMs: 1200,
      });
      applyFinalAnswer({ turnId, text: answer });

      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            archivedTurns: [archivedTurn({ turnId, answer })],
          })}
        />,
      );

      const archived = container.querySelector('.bh-bot-block[data-archived="true"]');
      expect(archived?.querySelector('[data-testid="execution-disclosure"]')).toBeNull();
      const disclosure = archived?.querySelector(".bh-step-list");
      const answerNode = Array.from(archived?.querySelectorAll("p") ?? []).find(
        (node) => node.textContent === answer,
      );
      expect(disclosure).toBeTruthy();
      expect(answerNode).toBeTruthy();
      expectBefore(disclosure!, answerNode!);
    });
  });

  describe("UserMessage plain-text", () => {
    it("user message has no background fill (plain-text path)", () => {
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-ub",
            user: "你好",
            running: true,
          })}
        />,
      );
      const ub = container.querySelector('[data-testid="user-msg"]') as HTMLElement | null;
      expect(ub).toBeTruthy();
      // Inline style: no `background` set → empty string.
      // We assert the user-msg wrapper itself has no background fill.
      const bg = ub!.style.background;
      expect(bg === "" || bg === "transparent" || bg === "none").toBe(true);
    });

    it("user message renders mono byline above plain text", () => {
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-ub2",
            user: "查询",
            running: true,
            startedAt: 1735128720000,
          })}
        />,
      );
      const ub = container.querySelector('[data-testid="user-msg"]') as HTMLElement | null;
      expect(ub).toBeTruthy();
      const children = Array.from(ub!.children) as HTMLElement[];
      // First child = mono byline div with "你 · time" prefix.
      expect(children[0].textContent).toMatch(/你/);
    });

    it("byline uses a readable ink tier, not the faint label tier", () => {
      // feedback-triage-2026-08-24: bylines previously used --c-ink-4; measured in dark mode:
      // #4F4C46 on #131210 = 2.21:1 (10.5px), practically unreadable.
      // Deliberately forbid ink-4 / ink-5 rather than require ink-3:
      // switching to ink-2 also fixes it; pinning one value would obstruct future changes.
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-ub-contrast",
            user: "查询",
            running: true,
            startedAt: 1735128720000,
          })}
        />,
      );
      const ub = container.querySelector('[data-testid="user-msg"]') as HTMLElement | null;
      const byline = Array.from(ub!.children)[0] as HTMLElement;
      expect(byline.textContent).toMatch(/你/);
      expect(byline.style.color).not.toMatch(/--c-ink-4|--c-ink-5/);
      expect(byline.style.color).toMatch(/--c-ink(-2|-3)?\)/);
    });

    it("attachment-only placeholder uses a readable ink tier", () => {
      // Same rule: with attachments but no text, this row **stands in for body text** (needing even more readability than the byline).
      // To reach this branch, supply actual attachments; merely clearing user prevents the whole UserMessage from rendering,
      // making the assertion target null (the first version did that; failure revealed it tested nothing).
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-ub-noteless",
            user: "   ",
            running: true,
            userAttachments: [{ artifact_id: "art_x", filename: "a.csv" } as never],
          })}
        />,
      );
      const ub = container.querySelector('[data-testid="user-msg"]') as HTMLElement | null;
      expect(ub).toBeTruthy();
      const placeholder = Array.from(ub!.children)[1] as HTMLElement;
      expect(placeholder.style.fontStyle).toBe("italic");
      expect(placeholder.style.color).not.toMatch(/--c-ink-4|--c-ink-5/);
    });

    it("user message exposes data-msg-id for stable lookup", () => {
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-ub3",
            user: "嗯",
            userMsgId: "bub-msg-abc",
            running: true,
          })}
        />,
      );
      const ub = container.querySelector('[data-msg-id="bub-msg-abc"]');
      expect(ub).toBeTruthy();
    });
  });

  describe("live + archived structure parity", () => {
    it("both live and archived turns render under `.bh-bot-block`", () => {
      const archived = archivedTurn({ turnId: "arch-1", user: "上次", answer: "答" });
      const { container: liveCt } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "live-1",
            user: "现在",
            answer: "回答",
            running: false,
          })}
        />,
      );
      const liveBlock = liveCt.querySelector('.bh-bot-block[data-archived="false"]');
      expect(liveBlock).toBeTruthy();
      expect(liveBlock?.getAttribute("data-results-message-token")).toBe("live-1");
      expect(liveBlock?.getAttribute("tabindex")).toBe("-1");
      cleanup();
      const { container: archCt } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            archivedTurns: [archived],
          })}
        />,
      );
      const archBlock = archCt.querySelector('.bh-bot-block[data-archived="true"]');
      expect(archBlock).toBeTruthy();
      expect(archBlock?.getAttribute("data-results-message-token")).toBe("arch-1");
      expect(archBlock?.getAttribute("tabindex")).toBe("-1");
      // Both have a BotByline at the top.
      expect(archBlock?.querySelector('[data-testid="bot-byline"]')).toBeTruthy();
    });
  });

  describe("beat dispatch", () => {
    beforeEach(() => _resetAllTurnsForTesting());

    it("narrative beats render via LeadNarrative", () => {
      applyCardCreated({ turnId: "t-n" });
      applyNarrativeAppend({ turnId: "t-n", text: "好的，我先查 Q3 数据", segId: "seg1" });
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-n",
            user: "Q3 怎么样",
            running: true,
          })}
        />,
      );
      expect(container.textContent).toContain("好的，我先查 Q3 数据");
    });
  });

  // workflow-solidify surface B — regression for the live-dogfood finding: the chip
  // must key on a COMPLETED turn that ran STEPS, NOT on runner.answer (BI/report
  // turns deliver via the canvas → runner.answer is empty, so an answer-gated chip
  // never appeared on exactly the analysis turns worth saving as a workflow).
  describe("面 B 固化 chip gate", () => {
    beforeEach(() => _resetAllTurnsForTesting());

    function seedStep(turnId: string): void {
      applyCardCreated({ turnId });
      applyStepStarted({
        turnId, stepId: "s1", stepType: "tool_run", toolName: "bi_query",
        headerZh: "查询", startedAt: new Date("2026-05-22T14:32:00Z"),
      });
    }
    const chipOf = (c: HTMLElement) =>
      Array.from(c.querySelectorAll("button")).find((b) => /固化此流程/.test(b.textContent || ""));

    it("shows on a completed turn WITH steps even when runner.answer is empty (canvas-delivered)", () => {
      seedStep("t-chip-ok");
      applyCardFinalized({ turnId: "t-chip-ok", finalState: "completed" });
      const onSolidify = vi.fn();
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-chip-ok",
            user: "看销售",
            answer: "",
            running: false,
            terminalStatus: "completed",
          })}
          onSolidify={onSolidify}
        />,
      );
      const chip = chipOf(container);
      expect(chip).toBeTruthy();
      chip!.click();
      expect(onSolidify).toHaveBeenCalledWith("t-chip-ok");
    });

    it("hides on a completed pure-chat turn with NO steps (no 流程 to solidify)", () => {
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({ turnId: "t-chip-nosteps", user: "你好", answer: "你好！", running: false })}
          onSolidify={vi.fn()}
        />,
      );
      expect(chipOf(container)).toBeUndefined();
    });

    it("hides while the turn is still running", () => {
      seedStep("t-chip-run");
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({ turnId: "t-chip-run", user: "看销售", running: true })}
          onSolidify={vi.fn()}
        />,
      );
      expect(chipOf(container)).toBeUndefined();
    });

    it("hides when onSolidify is absent (固化 flag off)", () => {
      seedStep("t-chip-flagoff");
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({ turnId: "t-chip-flagoff", user: "看销售", running: false })}
        />,
      );
      expect(chipOf(container)).toBeUndefined();
    });

    // scheduled-recipe-runs S2 (116 dogfood P1-C): the "固化整段对话" entry.
    // Backend session distillation was already ready, but the frontend could not send session_id; users saw an independent
    // chip per turn and could save only one (capturing an overturned definition or half a workflow missing data retrieval).
    const sessionChipOf = (c: HTMLElement) =>
      Array.from(c.querySelectorAll("button")).find((b) => /固化整段对话/.test(b.textContent || ""));

    it("多轮会话出「固化整段对话」并回传 sessionId", () => {
      seedStep("t-chip-session");
      applyCardFinalized({ turnId: "t-chip-session", finalState: "completed" });
      const onSolidifySession = vi.fn();
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-chip-session",
            sessionId: "s_multi",
            user: "改成自然月 Top10",
            running: false,
            terminalStatus: "completed",
            archivedTurns: [
              { turnId: "t-prev", terminalStatus: "completed", user: "近30天 Top5", answer: "…" },
            ] as never,
          })}
          onSolidify={vi.fn()}
          onSolidifySession={onSolidifySession}
        />,
      );
      const chip = sessionChipOf(container);
      expect(chip).toBeTruthy();
      chip!.click();
      expect(onSolidifySession).toHaveBeenCalledWith("s_multi");
    });

    it("单轮会话不出整段入口（与单轮 chip 等价，多一个只是噪声）", () => {
      seedStep("t-chip-single");
      applyCardFinalized({ turnId: "t-chip-single", finalState: "completed" });
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-chip-single",
            sessionId: "s_single",
            user: "看销售",
            running: false,
            terminalStatus: "completed",
          })}
          onSolidify={vi.fn()}
          onSolidifySession={vi.fn()}
        />,
      );
      expect(chipOf(container)).toBeTruthy();  // The single-turn chip remains normally.
      expect(sessionChipOf(container)).toBeUndefined();
    });

    it("F5 后（全部轮次走 archived、无 live turn）整段入口仍在", () => {
      // Self-review caught the entry living in the live turn's action row, disappearing after refresh, whereas returning later
      // to save the conversation often happens on session reentry. Move the entry to the conversation's end accordingly.
      const onSolidifySession = vi.fn();
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: null,          // No live turn.
            sessionId: "s_reloaded",
            running: false,
            archivedTurns: [
              { turnId: "t-1", terminalStatus: "completed", user: "近30天 Top5", answer: "…" },
              { turnId: "t-2", terminalStatus: "completed", user: "改成自然月", answer: "…" },
            ] as never,
          })}
          onSolidify={vi.fn()}
          onSolidifySession={onSolidifySession}
        />,
      );
      const chip = sessionChipOf(container);
      expect(chip).toBeTruthy();
      chip!.click();
      expect(onSolidifySession).toHaveBeenCalledWith("s_reloaded");
    });

    it("turn 还在跑时不出整段入口（跑完再固化）", () => {
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-running",
            sessionId: "s_busy",
            running: true,
            archivedTurns: [{ turnId: "t-1", terminalStatus: "completed", user: "x", answer: "y" }] as never,
          })}
          onSolidify={vi.fn()}
          onSolidifySession={vi.fn()}
        />,
      );
      expect(sessionChipOf(container)).toBeUndefined();
    });

    it("没有 sessionId 时不出整段入口（发不出目标就别给按钮）", () => {
      seedStep("t-chip-nosession");
      applyCardFinalized({ turnId: "t-chip-nosession", finalState: "completed" });
      const { container } = renderIntl(
        <TurnContainer
          runner={emptyRunner({
            turnId: "t-chip-nosession",
            sessionId: null,
            user: "看销售",
            running: false,
            terminalStatus: "completed",
            archivedTurns: [{ turnId: "t-prev", terminalStatus: "completed", user: "x", answer: "y" }] as never,
          })}
          onSolidify={vi.fn()}
          onSolidifySession={vi.fn()}
        />,
      );
      expect(sessionChipOf(container)).toBeUndefined();
    });
  });
});

