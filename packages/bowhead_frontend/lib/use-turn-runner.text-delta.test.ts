/**
 * A streamed text delta is recorded in the turn store and leaves the runner state alone.
 *
 * The runner state belongs to the page component. Replacing it for every delta rendered the whole page hundreds
 * of times per second while a reply streamed (116, 1.7.69, an observed task).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { getTurnSnapshot, subscribeTurn } from "./turn-store";
import { useTurnRunner } from "./use-turn-runner";

const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_ES = globalThis.EventSource;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  url: string;
  readyState = 1;
  onerror: ((e?: unknown) => void) | null = null;
  private listeners = new Map<string, Array<(e: MessageEvent) => void>>();
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(name: string, fn: (e: MessageEvent) => void) {
    const arr = this.listeners.get(name) ?? [];
    arr.push(fn);
    this.listeners.set(name, arr);
  }
  removeEventListener() {}
  close() {
    this.readyState = 2;
  }
  emit(name: string, payload: Record<string, unknown>) {
    for (const fn of this.listeners.get(name) ?? []) {
      fn({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }
}

// Each test has its own Turn: the turn store and its record of applied event numbers are module state.
let turnId = "";
let testNumber = 0;

beforeEach(() => {
  testNumber += 1;
  turnId = `t_text_delta_${testNumber}`;
  FakeEventSource.instances = [];
  (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).includes("/chat/send")) {
      return new Response(JSON.stringify({ turn_id: turnId, session_id: "s_1", stream_url: `/api/v1/chat/stream/${turnId}` }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  (globalThis as Record<string, unknown>).EventSource = ORIGINAL_ES;
  vi.restoreAllMocks();
});

function leadText(): string {
  const beats = getTurnSnapshot(turnId)?.beats ?? [];
  return beats.map((beat) => (beat.kind === "narrative" ? beat.text : "")).join("");
}

describe("narrative.delta on the live stream", () => {
  it("reaches the turn store without rendering the runner's owner, and announces once per frame", async () => {
    // Mutation: let `narrative.delta` fall through to the reducer → one render of the owner per delta.
    // Mutation: dispatch live text without the frame option → the store listener is called per delta.
    let renders = 0;
    const hook = renderHook(() => {
      renders += 1;
      return useTurnRunner({ mode: "sse", message: "写一份手册", workspaceId: "w", memberUid: "m", sendTick: 1 });
    });
    await waitFor(() => expect(hook.result.current.turnId).toBe(turnId));
    const es = FakeEventSource.instances[0];
    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "写一份手册" }));
    act(() => es.emit("step_list_card_created", { seq: 2, card_id: turnId }));

    let announced = 0;
    const stop = subscribeTurn(turnId, () => {
      announced += 1;
    });
    const pieces = ["门店", "巡检", "手册", "共三", "十节，", "每节", "八条", "要点。"];
    // The first delta after a real state change: React may run the owner once more to confirm nothing changed.
    act(() => es.emit("narrative.delta", { seq: 3, card_id: turnId, delta: pieces[0] }));
    const before = renders;
    const stateBefore = hook.result.current;
    // Each later delta arrives as its own task, as on the wire.
    pieces.slice(1).forEach((piece, i) => {
      act(() => es.emit("narrative.delta", { seq: 4 + i, card_id: turnId, delta: piece }));
    });

    expect(leadText()).toBe(pieces.join(""));
    expect(renders).toBe(before);
    expect(hook.result.current).toBe(stateBefore);
    await waitFor(() => expect(announced).toBeGreaterThan(0));
    // Eight deltas, far fewer announcements: one per frame that passed while they arrived.
    expect(announced).toBeLessThan(pieces.length);
    stop();
  });

  it("treats a helper's text the same way and keeps Lead's own partial answer in the runner state", async () => {
    // Mutation: route only `narrative.delta` past the reducer → the owner renders for every helper delta.
    let renders = 0;
    const hook = renderHook(() => {
      renders += 1;
      return useTurnRunner({ mode: "sse", message: "写一份手册", workspaceId: "w", memberUid: "m", sendTick: 1 });
    });
    await waitFor(() => expect(hook.result.current.turnId).toBe(turnId));
    const es = FakeEventSource.instances[0];
    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "写一份手册" }));
    act(() => es.emit("partial_answer", { seq: 2, card_id: turnId, parent_agent_id: "sub_1", delta: "已读" }));
    const before = renders;
    ["到第", "三节", "。"].forEach((piece, i) => {
      act(() => es.emit("partial_answer", { seq: 3 + i, card_id: turnId, parent_agent_id: "sub_1", delta: piece }));
    });
    expect(getTurnSnapshot(turnId)?.peerProse.sub_1).toBe("已读到第三节。");
    expect(renders).toBe(before);

    // Lead's own partial answer has no `parent_agent_id`: it still accumulates in `answer`.
    act(() => es.emit("partial_answer", { seq: 6, delta: "结论：" }));
    expect(hook.result.current.answer).toBe("结论：");
  });

  it("still clears the reconnecting state, as every other event does", async () => {
    // Mutation: return before the state update for text deltas → the banner stays until another event arrives.
    const hook = renderHook(() =>
      useTurnRunner({ mode: "sse", message: "写一份手册", workspaceId: "w", memberUid: "m", sendTick: 1 }),
    );
    await waitFor(() => expect(hook.result.current.turnId).toBe(turnId));
    const first = FakeEventSource.instances[0];
    act(() => first.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "写一份手册" }));
    // The connection drops: the runner shows the reconnecting state and opens the stream again.
    act(() => first.onerror?.());
    expect(hook.result.current.streamingState).toBe("error");
    await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(1), { timeout: 10_000 });
    const second = FakeEventSource.instances[FakeEventSource.instances.length - 1];

    act(() => second.emit("narrative.delta", { seq: 2, card_id: turnId, delta: "重连后的第一段文字" }));

    expect(hook.result.current.streamingState).toBe("active");
    expect(leadText()).toBe("重连后的第一段文字");
  }, 15_000);
});
