/**
 * Phase 1.7-A — beats[] order pipeline smoke gate.
 *
 * Per tech-design §2.6 + impl plan gates: given a sequence of dispatched
 * SSE events (narrative → step → narrative → step → answer), assert the
 * resulting `state.beats` matches the expected kinds in order. Pins the
 * beat-ordering invariant that 1.7-B composition rebuild depends on.
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyClarification,
  applyFinalAnswer,
  applyNarrativeAppend,
  applyStepDone,
  applyStepStarted,
  getTurnSnapshot,
  pushPickerBeat,
  selectAnswerPayload,
  selectNarrativeBuffer,
} from "./turn-store";
import {
  _resetNarrativeCursorsForTesting,
  dispatchToTurnStore,
} from "./turn-store-dispatch";

const T = "turn-beats";

afterEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

describe("Phase 1.7-A beat-order DOM smoke gate", () => {
  it("dispatched sequence narrative → step → narrative → step → answer yields expected beat kinds in order", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "好的，" });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "查询",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "bi_query",
    });
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "接下来" });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s2",
      step_type: "tool_run",
      header_zh: "查询",
      started_at: "2026-05-22T14:32:01Z",
      tool_name: "bi_query",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s2",
      step_type: "tool_run",
      state: "success",
      duration_ms: 500,
    });
    dispatchToTurnStore("final_answer", { turn_id: T, text: "结论" });

    const beats = getTurnSnapshot(T)?.beats ?? [];
    const kinds = beats.map((b) => b.kind);
    expect(kinds).toEqual([
      "narrative",
      "step",
      "narrative",
      "step",
      "answer",
    ]);
  });

  it("selectNarrativeBuffer concatenates narrative beats in dispatch order", () => {
    applyCardCreated({ turnId: T });
    applyNarrativeAppend({ turnId: T, text: "好的，", segId: "seg-A" });
    applyNarrativeAppend({ turnId: T, text: "我来", segId: "seg-A" });
    applyStepStarted({
      turnId: T,
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询",
    });
    applyNarrativeAppend({ turnId: T, text: "接下来对比。", segId: "seg-B" });
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("好的，我来接下来对比。");
  });

  it("selectAnswerPayload returns latest answer beat", () => {
    applyCardCreated({ turnId: T });
    applyFinalAnswer({ turnId: T, text: "first", payload: { turnId: T, exec_summary: "v1" } });
    applyFinalAnswer({ turnId: T, text: "second", payload: { turnId: T, exec_summary: "v2" } });
    const payload = selectAnswerPayload(getTurnSnapshot(T));
    expect(payload?.exec_summary).toBe("v2");
  });

  it("selectAnswerPayload returns null when no answer beat present", () => {
    applyCardCreated({ turnId: T });
    applyNarrativeAppend({ turnId: T, text: "narrative only", segId: "seg-A" });
    expect(selectAnswerPayload(getTurnSnapshot(T))).toBeNull();
  });

  it("2nd final_answer (M2 reconciliation corrective) replaces the answer beat — no duplicate bubble", () => {
    applyCardCreated({ turnId: T });
    applyNarrativeAppend({ turnId: T, text: "报告已生成，完整报告见画布。", segId: "seg-A" });
    applyFinalAnswer({ turnId: T, text: "报告已生成，完整报告见画布。" }); // 1st: phantom (D6-hidden path)
    applyFinalAnswer({ turnId: T, text: "本次未生成画布报告。" }); // 2nd: reconciliation corrective
    const beats = getTurnSnapshot(T)?.beats ?? [];
    expect(beats.filter((b) => b.kind === "answer")).toHaveLength(1); // one bubble, not two
  });

  it("applyClarification (V2.7 path) does NOT push a picker beat", () => {
    applyCardCreated({ turnId: T });
    applyClarification({
      turnId: T,
      clarification: { kind: "options", prompt: "选", options: [] },
    });
    const beats = getTurnSnapshot(T)?.beats ?? [];
    expect(beats.filter((b) => b.kind === "picker").length).toBe(0);
    // But ClarificationState is still on the snapshot
    expect(getTurnSnapshot(T)?.clarification?.kind).toBe("options");
  });

  it("pushPickerBeat (turn-terminating ThetaClarificationState) DOES push a picker beat", () => {
    applyCardCreated({ turnId: T });
    pushPickerBeat(T, {
      candidates: ["A", "B"],
      fallbackText: null,
      clarificationId: "clar-1",
    });
    const beats = getTurnSnapshot(T)?.beats ?? [];
    expect(beats.length).toBe(1);
    const beat = beats[0];
    expect(beat?.kind).toBe("picker");
  });

  it("step_done does NOT push an extra step beat (only step_started pushes)", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "查询",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "bi_query",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      state: "success",
      duration_ms: 500,
    });
    const beats = getTurnSnapshot(T)?.beats ?? [];
    expect(beats.filter((b) => b.kind === "step").length).toBe(1);
  });

  it("step_progress does NOT push a beat (progress feeds steps[].partialContent only)", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "lead_thinking_writing",
      header_zh: "写代码",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "lead_thinking_writing_python",
    });
    dispatchToTurnStore("step_progress", {
      card_id: T,
      step_id: "s1",
      content_delta: "import pandas",
    });
    const beats = getTurnSnapshot(T)?.beats ?? [];
    expect(beats.length).toBe(1);
    expect(beats[0]?.kind).toBe("step");
    // partialContent still accumulated on steps[]
    expect(getTurnSnapshot(T)?.steps[0]?.partialContent).toBe("import pandas");
  });

  it("DOM smoke: prettifyStepLabel coerces raw JSON dict to 空标签 (never raw JSON in beats[])", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "s1",
      step_type: "tool_run",
      header_zh: "查询",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "bi_query",
      // pathological raw object as input_preview
      input_preview: { foo: "bar", baz: 1 },
    });
    const step = getTurnSnapshot(T)?.steps[0];
    // chat-ux-2026-08 batch A: prettifyStepLabel returns an empty string for unknown shapes,
    // then turn-store-dispatch `prettifiedLabel || null` normalizes the falsy value to null.
    // Store contract = no label means null; assert the real contract, not an intermediate value.
    expect(step?.label).toBeNull();
    // Defensive check: no raw JSON braces in label
    expect(step?.label ?? "").not.toContain("{");
    expect(step?.label ?? "").not.toContain('"foo"');
  });
});
