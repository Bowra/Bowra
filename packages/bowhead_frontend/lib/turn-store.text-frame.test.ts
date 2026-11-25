/**
 * Streamed text reaches the Turn's state at once and its subscribers once per frame.
 *
 * A reply streams as hundreds of small deltas per second. One announcement per delta made React render the
 * conversation that often, and the browser fell behind the stream (116, 1.7.69, an observed task).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "./turn-store-dispatch";
import { _resetAllTurnsForTesting, getTurnSnapshot, subscribeTurn } from "./turn-store";

const T = "t_text_frame";
const LIVE_STREAM = { frameBatchedText: true };

let seq = 0;

function delta(text: string, options?: { frameBatchedText?: boolean }): void {
  seq += 1;
  dispatchToTurnStore("narrative.delta", { seq, card_id: T, delta: text }, "live", T, options);
}

function shownText(): string {
  const beats = getTurnSnapshot(T)?.beats ?? [];
  return beats.map((beat) => (beat.kind === "narrative" ? beat.text : "")).join("");
}

function listen(): { calls: () => number; stop: () => void } {
  let calls = 0;
  const stop = subscribeTurn(T, () => {
    calls += 1;
  });
  return { calls: () => calls, stop };
}

beforeEach(() => {
  seq = 0;
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "requestAnimationFrame", "cancelAnimationFrame"] });
  dispatchToTurnStore("turn_started", { seq: (seq += 1), turn_id: T, session_id: "s_1" });
  dispatchToTurnStore("step_list_card_created", { seq: (seq += 1), card_id: T });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

describe("streamed text is announced once per frame", () => {
  it("the state holds every delta at once; subscribers hear of them with the next frame, once", () => {
    // Mutation: commit a frame-batched append with an immediate announcement → five calls before the frame.
    const listener = listen();
    for (const piece of ["门", "店", "巡", "检", "手册"]) delta(piece, LIVE_STREAM);

    expect(shownText()).toBe("门店巡检手册");
    expect(listener.calls()).toBe(0);

    vi.advanceTimersByTime(17);
    expect(listener.calls()).toBe(1);

    // The frame is over: later text opens a new one.
    delta("。", LIVE_STREAM);
    expect(listener.calls()).toBe(1);
    vi.advanceTimersByTime(17);
    expect(listener.calls()).toBe(2);
    listener.stop();
  });

  it("any other event of the Turn announces at once and takes the pending text with it", () => {
    // Mutation: leave the Turn in the pending set on an immediate commit → a second call when the frame passes.
    const listener = listen();
    delta("先看", LIVE_STREAM);
    delta("数据。", LIVE_STREAM);
    expect(listener.calls()).toBe(0);

    dispatchToTurnStore("step_started", {
      seq: (seq += 1), card_id: T, step_id: "s1", step_type: "tool_run", tool_name: "bi_query", header_zh: "查询数据",
    });
    expect(listener.calls()).toBe(1);
    expect(shownText()).toBe("先看数据。");

    vi.advanceTimersByTime(200);
    expect(listener.calls()).toBe(1);
    listener.stop();
  });

  it("a helper's streamed text follows the same rule", () => {
    // Mutation: commit helper text with an immediate announcement → three calls before the frame.
    const listener = listen();
    for (const piece of ["已读", "到第", "三节。"]) {
      dispatchToTurnStore("partial_answer", { seq: (seq += 1), card_id: T, parent_agent_id: "sub_1", delta: piece }, "live", T, LIVE_STREAM);
    }
    expect(getTurnSnapshot(T)?.peerProse.sub_1).toBe("已读到第三节。");
    expect(listener.calls()).toBe(0);
    vi.advanceTimersByTime(17);
    expect(listener.calls()).toBe(1);

    dispatchToTurnStore("partial_answer", { seq: (seq += 1), card_id: T, parent_agent_id: "sub_1", delta: "完。" });
    expect(listener.calls()).toBe(2);
    listener.stop();
  });

  it("replay and direct callers keep one announcement per delta", () => {
    const listener = listen();
    delta("甲");
    delta("乙");
    expect(listener.calls()).toBe(2);
    vi.advanceTimersByTime(200);
    expect(listener.calls()).toBe(2);
    listener.stop();
  });

  it("a tab that gets no frames still hears of the text, from the timer beside the frame", () => {
    // A hidden tab runs no animation frames. Mutation: schedule the frame only → the listener is never called.
    vi.stubGlobal("requestAnimationFrame", () => 1);
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
    const listener = listen();
    delta("后台标签页里的文字", LIVE_STREAM);
    vi.advanceTimersByTime(50);
    expect(listener.calls()).toBe(0);
    vi.advanceTimersByTime(60);
    expect(listener.calls()).toBe(1);
    expect(shownText()).toBe("后台标签页里的文字");
    listener.stop();
  });
});
