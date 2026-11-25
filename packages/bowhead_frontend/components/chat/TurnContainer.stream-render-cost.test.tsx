/**
 * A streamed text delta parses the block that grew and nothing else.
 *
 * 116, 1.7.69: with one earlier 7,000-character answer on the page, the browser fell behind the event stream by
 * 28 s at the delivery review and 105 s at `done` (an observed task), and Stop reached the server 15 s after the
 * Turn had completed (an observed task). Every delta rendered the whole conversation and parsed every Markdown
 * block on it again: the earlier answers, the reply a review had sent back, the lead-in and the growing reply.
 *
 * The parser is counted at `react-markdown` itself, so the test fails for any path that parses a block again.
 */

import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";
import { _resetAllTurnsForTesting } from "@/lib/turn-store";
import type { TurnEventDTO } from "@/lib/api";
import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

const parsed: string[] = [];

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

vi.mock("react-markdown", async (importOriginal) => {
  const real = await importOriginal<typeof import("react-markdown")>();
  const Real = real.default as unknown as (props: { children?: string }) => React.ReactElement;
  return {
    ...real,
    default: (props: { children?: string }) => {
      parsed.push(String(props.children ?? ""));
      return Real(props);
    },
  };
});

import { TurnContainer } from "./TurnContainer";

const LIVE = "t_live";
const EARLIER = "t_earlier";
const EARLIER_ANSWER = "## 上一轮的手册\n\n- 上一轮的要点一\n- 上一轮的要点二\n";
const LEAD_IN = "先让计算助手生成模拟数据。";
const FIRST_REPLY = "## 复核前写的手册\n\n- 第一份的要点一\n- 第一份的要点二\n";
const REWRITE_START = "## 复核后重写的手册\n\n- 重写的要点一\n";

let seq = 0;

function send(event: string, payload: Record<string, unknown>): void {
  seq += 1;
  dispatchToTurnStore(event, { seq, ...payload });
}

function earlierTurn(): ArchivedTurn {
  let rowSeq = 0;
  const row = (event_type: string, payload: Record<string, unknown>): TurnEventDTO => {
    rowSeq += 1;
    return { seq: rowSeq, event_type, payload: { seq: rowSeq, ...payload }, occurred_at: "" };
  };
  replayTurnEventRows(EARLIER, [
    row("turn_started", { turn_id: EARLIER, session_id: "s_1" }),
    row("step_list_card_created", { card_id: EARLIER }),
    row("narrative.delta", { card_id: EARLIER, delta: EARLIER_ANSWER }),
    row("final_answer", { card_id: EARLIER, turn_id: EARLIER, text: EARLIER_ANSWER }),
    row("step_list_card_finalized", { card_id: EARLIER, state: "completed", step_count: 0 }),
    row("done", { turn_id: EARLIER, status: "completed", duration_ms: 1000 }),
  ]);
  return {
    turnId: EARLIER,
    terminalStatus: "completed",
    user: "上一轮的问题",
    userAttachments: [],
    answer: EARLIER_ANSWER,
    traceSteps: [],
    peersDispatched: [],
    errorReason: null,
    responseKind: "chat",
  } as unknown as ArchivedTurn;
}

/** The live Turn of the 116 case: lead-in, a helper, a first reply the review sent back, and the rewrite. */
function startLiveTurn(): void {
  send("turn_started", { turn_id: LIVE, session_id: "s_1" });
  send("step_list_card_created", { card_id: LIVE });
  send("narrative.delta", { card_id: LIVE, delta: LEAD_IN });
  send("step_started", { card_id: LIVE, step_id: "spawn", step_type: "subagent_spawn", tool_name: "subagent_spawn", header_zh: "派发" });
  send("step_done", { card_id: LIVE, step_id: "spawn", state: "done", duration_ms: 900 });
  send("narrative.delta", { card_id: LIVE, delta: FIRST_REPLY });
  send("step_started", { card_id: LIVE, step_id: "review", step_type: "tool_run", tool_name: "review_delivery", header_zh: "复核" });
  send("step_done", { card_id: LIVE, step_id: "review", state: "done", duration_ms: 10 });
  send("narrative.delta", { card_id: LIVE, delta: REWRITE_START });
}

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
  return {
    user: "写一份手册",
    intent: null,
    complexity: null,
    chips: [],
    answer: "",
    artifact: null,
    source: null,
    summary: null,
    traceSteps: [],
    turnId: LIVE,
    sessionId: "s_1",
    sessionTitle: null,
    running: true,
    started: true,
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
  } as TurnRunnerState;
}

function resetAll(): void {
  cleanup();
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  parsed.length = 0;
  seq = 0;
}

describe("a streamed text delta and the Markdown already on the page", () => {
  beforeEach(resetAll);
  afterEach(resetAll);

  it("parses the growing reply only, not the earlier answer, the reply sent back or the lead-in", () => {
    // Mutation: drop `memo` from LeadNarrative → the lead-in and the first reply are parsed again; drop it from
    // ChatBubbleNew → the earlier Turn's answer is parsed again. Either makes this test fail.
    const state = runner({ archivedTurns: [earlierTurn()] });
    startLiveTurn();
    const view = render(<TurnContainer runner={state} />);
    // Positive control: every block was parsed once for the first paint.
    expect(parsed).toEqual(expect.arrayContaining([EARLIER_ANSWER, LEAD_IN, FIRST_REPLY, REWRITE_START]));

    parsed.length = 0;
    act(() => send("narrative.delta", { card_id: LIVE, delta: "- 重写的要点二\n" }));

    expect(parsed).toEqual([`${REWRITE_START}- 重写的要点二\n`]);
    const text = view.container.textContent ?? "";
    expect(text).toContain("重写的要点二");
    // The blocks that were skipped are still on the page.
    expect(text).toContain("上一轮的要点二");
    expect(text).toContain("第一份的要点二");
  });

  it("parses nothing when the page renders again and no text changed", () => {
    const state = runner({ archivedTurns: [earlierTurn()] });
    startLiveTurn();
    const view = render(<TurnContainer runner={state} />);
    parsed.length = 0;
    // Any other page state change hands the conversation a new runner object.
    view.rerender(<TurnContainer runner={{ ...state }} />);
    expect(parsed).toEqual([]);
  });

  it("parses a block again when its own text or state changes", () => {
    const state = runner({ archivedTurns: [earlierTurn()] });
    startLiveTurn();
    const view = render(<TurnContainer runner={state} />);
    parsed.length = 0;
    // The Turn ends: the rewrite stops streaming (its caret goes) and the final answer arrives.
    const final = `${REWRITE_START}- 重写的要点二\n`;
    act(() => {
      send("final_answer", { card_id: LIVE, turn_id: LIVE, text: final });
      send("done", { turn_id: LIVE, status: "completed", duration_ms: 2000 });
    });
    view.rerender(<TurnContainer runner={{ ...state, running: false, answer: final }} />);
    expect(parsed).toContain(final);
    expect(parsed).not.toContain(EARLIER_ANSWER);
  });
});
