/**
 * stream-liveness ① — transport measurement **wiring** contract.
 *
 * ⚠ Why a separate file: `transport-health.test.ts` tests **conditions**,
 * and can pass while the module is correct but entirely unwired. This sprint's ⑤ failed exactly that way:
 * six tests called handlers directly, bypassing actual dispatch, until independent review found the feature
 * was a no-op on real paths. Thus use `useTurnRunner` + actual SSE event streams here.
 *
 * Contract:
 *   1. Record every inbound named event (firstEventAt / lastEventAt)
 *   2. Record keepalive **separately** (it bypasses handle), never count as a business event
 *   3. Count every reconnect (historical replay clusters events, causing verdict abstention)
 *   4. Positive control: normal turn reconnects must be 0
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
  fail() {
    this.readyState = 0;
    this.onerror?.();
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).includes("/chat/send")) {
      return new Response(
        JSON.stringify({ turn_id: "t_1", session_id: "s_1", stream_url: "/api/v1/chat/stream/t_1" }),
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

function renderRunner() {
  return renderHook(() =>
    useTurnRunner({ mode: "sse", message: "查一下", workspaceId: "w", memberUid: "m", sendTick: 1 }),
  );
}

describe("传输测量接线", () => {
  it("入站事件被记录（firstEventAt 落在第一条，lastEventAt 跟着走）", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es = FakeEventSource.instances[0];

    expect(hook.result.current.transportRef?.current?.firstEventAt).toBeNull();

    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下" }));
    const s1 = hook.result.current.transportRef!.current!;
    expect(s1.firstEventAt).not.toBeNull();
    const first = s1.firstEventAt!;

    act(() => es.emit("tool_call", { seq: 2, call_id: "c1", tool_name: "datarc.query" }));
    const s2 = hook.result.current.transportRef!.current!;
    expect(s2.firstEventAt).toBe(first); // First arrival is never overwritten.
    expect(s2.lastEventAt).not.toBeNull();
    expect(s2.lastEventAt!).toBeGreaterThanOrEqual(first);
  });

  it("keepalive 既进心跳台账，也算作「收到字节」", async () => {
    // ★ Heartbeats **are** evidence of live transport. Initially I excluded them from firstEventAt,
    // making msSinceLastEvent falsely report silence throughout heartbeat-only periods without business events,
    // precisely when the path is healthy and backend is working.
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es = FakeEventSource.instances[0];

    act(() => es.emit("keepalive", { ts: 1 }));
    act(() => es.emit("keepalive", { ts: 2 }));

    const s = hook.result.current.transportRef!.current!;
    expect(s.keepaliveArrivals.length).toBe(2); // Separate ledger (corroborates heartbeat gaps).
    expect(s.firstEventAt).not.toBeNull();      // Also counts as inbound bytes.
  });

  it("★不经过 handle 的监听（canvas 那一族）同样被记录", async () => {
    // review #4: ~17 canvas listeners share **the same es** and bypass handle.
    // Individual call-site patches miss some; wrap es.addEventListener to record them.
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es = FakeEventSource.instances[0];

    let seen = 0;
    es.addEventListener("canvas.section_chunk", () => { seen += 1; });
    act(() => es.emit("canvas.section_chunk", { seq: 1, text: "片段" }));

    expect(seen).toBe(1); // Original handler still runs (wrapper does not swallow it).
    expect(hook.result.current.transportRef!.current!.firstEventAt).not.toBeNull();
  });

  it("重连被计数（判据据此弃权）", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es0 = FakeEventSource.instances[0];

    act(() => es0.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下" }));
    expect(hook.result.current.transportRef!.current!.reconnects).toBe(0);

    act(() => es0.fail());
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2), { timeout: 3000 });

    expect(hook.result.current.transportRef!.current!.reconnects).toBe(1);
  });

  it("★watchdog 的 stall cycle 重连也要计数（不只是 onerror 那条路）", async () => {
    // HIGH caught by review #1: `openConnection` has **three** call sites: first connection, `scheduleReopen`,
    // and watchdog stall cycle. I originally counted in scheduleReopen, missing watchdog's
    // **main** path (half-open socket; this file records Tencent CLB cutting connections every 60–90s).
    // Cuts + replay bursts falsely look buffered, exactly what the reconnected guard prevents.
    //
    // ⚠ Advance timers throughout, never waitFor: its real clock interlocks with fake timers.
    vi.useFakeTimers();
    let hook: ReturnType<typeof renderRunner> | null = null;
    try {
      hook = renderRunner();
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(FakeEventSource.instances.length).toBe(1);
      const es0 = FakeEventSource.instances[0];
      act(() => es0.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下" }));
      expect(hook.result.current.transportRef!.current!.reconnects).toBe(0);

      // Half-open: readyState stays OPEN with zero incoming bytes; watchdog forces a cycle after 40s.
      // Watchdog ticks every 5s; advancing 46s crosses STALL_RECONNECT_MS.
      await act(async () => { await vi.advanceTimersByTimeAsync(46_000); });

      expect(FakeEventSource.instances.length).toBeGreaterThan(1);
      expect(hook.result.current.transportRef!.current!.reconnects).toBeGreaterThan(0);
    } finally {
      // Unmount before restoring timers, preventing reconnect/watchdog loops leaking into the next case.
      hook?.unmount();
      vi.useRealTimers();
    }
  });

  it("★正对照：正常跑完一轮，reconnects 必须是 0", async () => {
    // Without this, the previous 1 could mean always count 1, failing to verify the count actually follows reconnects.
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es = FakeEventSource.instances[0];

    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下" }));
    act(() => es.emit("final_answer", { seq: 2, text: "答案" }));
    act(() => es.emit("done", { seq: 3, turn_id: "t_1", status: "completed" }));

    expect(hook.result.current.transportRef!.current!.reconnects).toBe(0);
  });
});
