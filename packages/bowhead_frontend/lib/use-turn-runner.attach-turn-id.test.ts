/**
 * chat-ux-2026-08 · attaching a stream must advance runner's turnId to the turn owning it.
 *
 * Root cause caught by read-only sampling on 116 prod 1.0.79 (every 300ms):
 *
 * | Time | Cards | Turn blocks | What happened |
 * | 303ms | 1 | A0,A1 | — |
 * | **11703ms** | **2** | A0,A1,**L1** | **Only `__open__`** |
 * | 12039ms | | | `turn_started` arrived (336ms later) |
 *
 * The second plan card rendered **before any execution-turn event arrived**; events did not cause it.
 * `attachStreamCallback`'s `setState({...s, running:true})` made the live turn block appear,
 * but that block queried beats with `useTurn(runner.turnId)` while `turnId` still referred to the **previous turn**,
 * rendering the previous plan beat again.
 *
 * More than an extra plan card: **the entire live block stayed attached to the previous turn throughout execution**
 * (step ledger and beats were all misaligned); the card was just the most visible symptom.
 *
 * Previously this path never updated `turnId`: `turn_started` did not set it,
 * while `streamTurnId` parsed from the URL fed only canvas reconciliation.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

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
  /** Test driver: simulate Gateway delivery of one SSE event (same shape as reconnect tests). */
  emit(name: string, payload: Record<string, unknown>) {
    for (const fn of this.listeners.get(name) ?? []) {
      fn({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).includes("/chat/send")) {
      return new Response(
        JSON.stringify({ turn_id: "t_propose", session_id: "s_1", stream_url: "/api/v1/chat/stream/t_propose" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  (globalThis as Record<string, unknown>).EventSource = ORIGINAL_ES;
  vi.restoreAllMocks();
});

/** First use normal send to place runner on the proposal turn (same setup as existing reconnect tests). */
function renderRunner() {
  return renderHook(() =>
    useTurnRunner({ mode: "sse", message: "提计划", workspaceId: "w", memberUid: "m", sendTick: 1 }),
  );
}

/** Wait for runner to settle on the proposal turn. */
async function settledOnProposal(hook: ReturnType<typeof renderRunner>) {
  await waitFor(() => expect(hook.result.current.turnId).toBe("t_propose"));
  return hook;
}

describe("attachStream — turnId 推进到流所属的那一轮", () => {
  it("★ 接新一轮的流 → runner.turnId 变成那一轮（此前恒留在上一轮）", async () => {
    const hook = await settledOnProposal(renderRunner());
    act(() => hook.result.current.attachStream?.("/api/v1/chat/stream/t_exec"));
    expect(hook.result.current.turnId).toBe("t_exec");
  });

  it("★ 推进时清掉上一轮的 live 内容（否则新轮块顶着旧答案/旧步骤）", async () => {
    const hook = await settledOnProposal(renderRunner());
    const es = FakeEventSource.instances[0];
    act(() => es.emit("partial_answer", { seq: 1, text: "旧答案" }));
    act(() => hook.result.current.attachStream?.("/api/v1/chat/stream/t_exec"));
    expect(hook.result.current.answer).toBe("");
    expect(hook.result.current.traceSteps).toEqual([]);
    expect(hook.result.current.chips).toEqual([]);
  });

  it("★ 推进时把上一轮**归档**，不是丢弃（116 dogfood 逮到的自伤）", async () => {
    // The first version manually cleared user/answer; if the previous turn was live, its entire content vanished,
    // recoverable only via F5 from the server: observed before approval L1 → after approval A0 (proposal vanished)
    // → F5 restored A1,A0. Another live≠replay mismatch.
    const hook = await settledOnProposal(renderRunner());
    const es = FakeEventSource.instances[0];
    act(() => es.emit("partial_answer", { seq: 1, text: "上一轮的答案" }));

    const beforeCount = hook.result.current.archivedTurns?.length ?? 0;
    act(() => hook.result.current.attachStream?.("/api/v1/chat/stream/t_exec"));

    const archived = hook.result.current.archivedTurns ?? [];
    expect(archived.length).toBe(beforeCount + 1);
    expect(archived[archived.length - 1].turnId).toBe("t_propose");
    expect(archived[archived.length - 1].user).toBe("提计划");
  });

  it("同一轮续跑(clarification resume)是幂等的——turnId 不变", async () => {
    const hook = await settledOnProposal(renderRunner());
    act(() => hook.result.current.attachStream?.("/api/v1/chat/stream/t_propose"));
    expect(hook.result.current.turnId).toBe("t_propose");
  });

  it("带 query 的流 URL 也能取出 turnId（不能被 ?a=b 干扰）", async () => {
    const hook = await settledOnProposal(renderRunner());
    act(() => hook.result.current.attachStream?.("/api/v1/chat/stream/t_exec?generation=2"));
    expect(hook.result.current.turnId).toBe("t_exec");
  });

  it("接流后 running 置真（原有行为不能被本次改动破坏）", async () => {
    const hook = await settledOnProposal(renderRunner());
    act(() => hook.result.current.attachStream?.("/api/v1/chat/stream/t_exec"));
    expect(hook.result.current.running).toBe(true);
  });
});
