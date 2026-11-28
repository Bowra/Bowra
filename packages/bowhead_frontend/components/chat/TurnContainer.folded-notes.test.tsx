/**
 * Chat running state R1.9: Lead's text a later step follows folds into the running card as a note, at its place in
 * time, instead of streaming below and vanishing at the end. A draft written before the delivery check is labelled
 * "初稿"; once the answer repeats it, the note says so and the page shows those words once.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { _resetAllTurnsForTesting } from "@/lib/turn-store";
import type { TurnRunnerState } from "@/lib/types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "zh-CN",
}));

import { TurnContainer } from "./TurnContainer";

const LIVE = "t_fold";
const LEAD_IN = "先请计算助手汇总各门店营业额。";
const DRAFT = "门店A营业额最高，门店B次之。";
let seq = 0;

function send(event: string, payload: Record<string, unknown>): void {
  seq += 1;
  dispatchToTurnStore(event, { seq, ...payload });
}

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
  return {
    user: "哪家门店营业额最高",
    intent: null, complexity: null, chips: [], answer: "", artifact: null, source: null, summary: null,
    traceSteps: [], turnId: LIVE, sessionId: "s_1", sessionTitle: null, running: true, started: true,
    messages: [], userAttachments: [], thinking: "", thinkingLength: 0, lastThinkingHintAttachedAt: "",
    clarifications: [], createToolOpen: null, subagentCreatorOpen: null, solidifyOpen: null, plan: null,
    ...over,
  } as TurnRunnerState;
}

function runUntilReview(): void {
  send("turn_started", { turn_id: LIVE, session_id: "s_1" });
  send("step_list_card_created", { card_id: LIVE });
  send("narrative.delta", { card_id: LIVE, delta: LEAD_IN });
  send("step_started", { card_id: LIVE, step_id: "spawn", step_type: "subagent_spawn", tool_name: "subagent_spawn", header_zh: "派发" });
  send("step_done", { card_id: LIVE, step_id: "spawn", state: "done", duration_ms: 900 });
  send("narrative.delta", { card_id: LIVE, delta: DRAFT });
  send("step_started", { card_id: LIVE, step_id: "review", step_type: "tool_run", tool_name: "review_delivery", header_zh: "复核" });
}

const notes = (root: HTMLElement) => Array.from(root.querySelectorAll('[data-row-kind="note"]'));
const occurrences = (root: HTMLElement, needle: string) => (root.textContent ?? "").split(needle).length - 1;

describe("Lead's interim text in the running card", () => {
  beforeEach(() => { cleanup(); _resetAllTurnsForTesting(); _resetNarrativeCursorsForTesting(); seq = 0; });
  afterEach(() => { cleanup(); _resetAllTurnsForTesting(); _resetNarrativeCursorsForTesting(); });

  it("folds the lead-in and the draft into the card, the draft labelled and not repeated in the text flow", () => {
    runUntilReview();
    const { container } = render(<TurnContainer runner={runner()} />);
    const band = container.querySelector('[data-testid="running-band"]')!;
    const inBand = notes(band as HTMLElement);
    expect(inBand.map((note) => note.textContent)).toEqual([
      expect.stringContaining(LEAD_IN),
      expect.stringContaining("note_draft"),
    ]);
    expect(inBand[1].textContent).toContain(DRAFT);
    expect(occurrences(container, DRAFT)).toBe(1);
    // The delivery check is the group's current line, not a step line.
    expect(band.querySelector('[data-row-kind="protocol"]')?.textContent).toBe("band_closeout_review");
  });

  it("says the draft was replaced once Lead writes again after the check, and shows those words once", () => {
    runUntilReview();
    send("step_done", { card_id: LIVE, step_id: "review", state: "done", duration_ms: 10 });
    send("narrative.delta", { card_id: LIVE, delta: `${DRAFT}门店C第三。` });
    const { container } = render(<TurnContainer runner={runner()} />);
    const draft = notes(container).find((note) => note.textContent?.includes("note_draft"));
    expect(draft?.textContent).toContain("note_replaced");
    expect(occurrences(container, DRAFT)).toBe(1);
  });
});
