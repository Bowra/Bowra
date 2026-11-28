import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import type { TurnBeat } from "@/lib/turn-store";

import { FoldedNoteRow, foldNarrative, type FoldAnswer } from "./folded-notes";
import type { StepView } from "./types";

afterEach(() => cleanup());

const narrative = (segId: string, text: string): TurnBeat => ({ kind: "narrative", segId, text });
const stepBeat = (stepId: string): TurnBeat => ({ kind: "step", stepId });
const view = (stepId: string, toolName: StepView["toolName"]): StepView =>
  ({ stepId, parentStepId: null, concurrencyGroupId: null, stepType: "tool_run", toolName, headerZh: "", state: "done" });
const noAnswer: FoldAnswer = { hasAnswer: false, superseded: false, repeatsAnswer: () => false };

describe("foldNarrative (chat running state R1.9)", () => {
  it("folds text a later step follows, and leaves the open and the final segment in the flow", () => {
    const beats = [narrative("a", "我先查一下数据。"), stepBeat("q"), narrative("b", "数据查好了，开始汇总。"), stepBeat("c"), narrative("c2", "结论如下……")];
    const { notes, folded } = foldNarrative(beats, [view("q", "bi_query"), view("c", "python_sandbox")], noAnswer);
    expect(notes.map((note) => [note.text, note.beforeStepId])).toEqual([["我先查一下数据。", "q"], ["数据查好了，开始汇总。", "c"]]);
    expect(folded.has(beats[4])).toBe(false);
  });

  it("labels text before Lead's delivery check as a draft", () => {
    const beats = [narrative("a", "初步结论……"), stepBeat("review")];
    const [note] = foldNarrative(beats, [view("review", "review_delivery")], noAnswer).notes;
    expect(note.draft).toBe(true);
  });

  it("marks a draft replaced once the answer exists and the narrative was withdrawn, but not an ordinary note", () => {
    const beats = [narrative("a", "我先查一下数据。"), stepBeat("q"), narrative("b", "初步结论……"), stepBeat("review")];
    const steps = [view("q", "bi_query"), view("review", "review_delivery")];
    const withdrawn: FoldAnswer = { hasAnswer: true, superseded: true, repeatsAnswer: () => false };
    expect(foldNarrative(beats, steps, withdrawn).notes.map((note) => note.replaced)).toEqual([false, true]);
    // Before the answer starts nothing is replaced yet.
    expect(foldNarrative(beats, steps, { ...withdrawn, hasAnswer: false }).notes.map((note) => note.replaced)).toEqual([false, false]);
  });

  it("marks any note that repeats the answer as replaced", () => {
    const beats = [narrative("a", "门店 A 营业额最高。"), stepBeat("q")];
    const repeats: FoldAnswer = { hasAnswer: true, superseded: false, repeatsAnswer: (beat) => beat.text.includes("门店 A") };
    expect(foldNarrative(beats, [view("q", "bi_query")], repeats).notes[0].replaced).toBe(true);
  });
});

describe("FoldedNoteRow", () => {
  const note = { key: "0:a", text: "**初步结论**\n\n- 门店 A 最高\n- 门店 B 次之", beforeStepId: "review", draft: true, replaced: false };

  it("shows the first line, labels a draft, and opens the full text in place", () => {
    const { container, getByRole } = renderIntl(<FoldedNoteRow note={note} />);
    expect(container.textContent).toContain("初步结论");
    expect(container.textContent).not.toContain("门店 B 次之");
    const toggle = getByRole("button");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(container.textContent).toContain("门店 B 次之");
  });

  it("shows no text of a replaced note, so the answer's words never appear twice", () => {
    const { container } = renderIntl(<FoldedNoteRow note={{ ...note, replaced: true }} />);
    expect(container.querySelector('[data-note-replaced="1"]')).toBeTruthy();
    expect(container.textContent).not.toContain("初步结论");
  });
});

describe("foldNarrative while the Turn runs", () => {
  it("marks a draft replaced once Lead writes again after its delivery check", () => {
    const beats = [narrative("a", "初步结论……"), stepBeat("review"), narrative("b", "复核后的结论……")];
    const [note] = foldNarrative(beats, [view("review", "review_delivery")], noAnswer).notes;
    expect(note).toMatchObject({ draft: true, replaced: true });
    // Positive control: before the rewrite starts, the draft is not replaced yet.
    expect(foldNarrative(beats.slice(0, 2), [view("review", "review_delivery")], noAnswer).notes[0].replaced).toBe(false);
  });
});
