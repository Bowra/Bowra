/**
 * Agent run limits, phase 3: the transient `turn.progress` frame reaches the runner state, and a new
 * turn starts without the earlier turn's snapshot.
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
  emit(name: string, payload: Record<string, unknown>) {
    for (const fn of this.listeners.get(name) ?? []) {
      fn({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
  let sendCount = 0;
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).includes("/chat/send")) {
      sendCount += 1;
      const id = `t_${sendCount}`;
      return new Response(JSON.stringify({ turn_id: id, session_id: "s_1", stream_url: `/api/v1/chat/stream/${id}` }), {
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

describe("turn.progress in the runner state", () => {
  it("a new turn starts without the earlier turn's agent progress", async () => {
    // an observed deployment dogfood (s_424e242beb0f): an observed task ran a compute helper and was stopped; the next turn
    // an observed task ran no helper, yet after a quiet minute its band read "仍在运行 · 数据与计算分析师 · 第 2 步".
    // Mutation: drop `agentProgress` from the fresh-turn reset → this test fails.
    const hook = renderHook(
      ({ tick }) => useTurnRunner({ mode: "sse", message: `request ${tick}`, workspaceId: "w", memberUid: "m", sendTick: tick }),
      { initialProps: { tick: 1 } },
    );
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es = FakeEventSource.instances[0];
    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "request 1" }));
    act(() => es.emit("turn.progress", {
      turn_id: "t_1",
      agents: [{ subagent_id: "sub_1", profile: "compute", display_name: "compute", step: 2 }],
    }));
    expect(hook.result.current.agentProgress).toEqual({
      agents: [{ subagentId: "sub_1", profile: "compute", displayName: "compute", step: 2 }],
    });
    act(() => es.emit("done", { seq: 2, turn_id: "t_1", status: "cancelled" }));

    hook.rerender({ tick: 2 });
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_2"));
    expect(hook.result.current.agentProgress ?? null).toBeNull();
  });
});
