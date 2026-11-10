/**
 * Live SSE byline times (dogfood N3, 116 1.7.18): the archived snapshot of a turn carries the server times from its
 * events, not the browser clock at the moment it was archived. Before, `archivedAt: Date.now()` turned a reply that
 * ended at 18:48 into 18:50 the moment the next message was sent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";
import { _resetNarrativeCursorsForTesting } from "./turn-store-dispatch";
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
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), fn]);
  }
  removeEventListener() {
    /* noop */
  }
  close() {
    this.readyState = 2;
  }
  emit(name: string, payload: Record<string, unknown>) {
    for (const fn of this.listeners.get(name) ?? []) fn({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

let sends = 0;

beforeEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  FakeEventSource.instances = [];
  sends = 0;
  (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).includes("/chat/send")) {
      sends += 1;
      return new Response(
        JSON.stringify({ turn_id: `t_${sends}`, session_id: "s_1", stream_url: `/api/v1/chat/stream/t_${sends}` }),
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
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

const STARTED = "2026-11-08T02:40:10.000000+00:00";
const ENDED = "2026-11-08T02:41:53.000000+00:00";

function renderRunner() {
  return renderHook(
    (props: { message: string; sendTick: number }) =>
      useTurnRunner({ mode: "sse", workspaceId: "w", memberUid: "m", ...props }),
    { initialProps: { message: "Top 5 regions", sendTick: 1 } },
  );
}

describe("live archived snapshot byline times", () => {
  it.each(["failed", "clarification_pending"])(
    "a %s turn archived by the next message keeps its done and turn_started server times",
    async (status) => {
      const hook = renderRunner();
      await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
      const es = FakeEventSource.instances[0];
      act(() => es.emit("turn_started", { seq: 1, turn_id: "t_1", session_id: "s_1", started_at: STARTED }));
      act(() => es.emit("done", { seq: 2, turn_id: "t_1", status, reason_code: "lead_internal_error", ended_at: ENDED }));
      await waitFor(() => expect(hook.result.current.running).toBe(false));
      expect(getTurnSnapshot("t_1")?.endedAt?.getTime()).toBe(Date.parse(ENDED));

      hook.rerender({ message: "And by month?", sendTick: 2 });
      await waitFor(() => expect(hook.result.current.archivedTurns?.length).toBe(1));
      const archived = hook.result.current.archivedTurns![0];
      expect(archived.turnId).toBe("t_1");
      expect(archived.endedAt).toBe(Date.parse(ENDED));
      expect(archived.userSentAt).toBe(Date.parse(STARTED));
    },
  );

  it("a completed turn archived on its done keeps the done's server time", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es = FakeEventSource.instances[0];
    act(() => es.emit("turn_started", { seq: 1, turn_id: "t_1", session_id: "s_1", started_at: STARTED }));
    act(() => es.emit("final_answer", { seq: 2, turn_id: "t_1", card_id: "t_1", text: "East leads." }));
    act(() => es.emit("done", { seq: 3, turn_id: "t_1", status: "completed", ended_at: ENDED }));
    await waitFor(() => expect(hook.result.current.archivedTurns?.length).toBe(1));
    expect(hook.result.current.archivedTurns![0].endedAt).toBe(Date.parse(ENDED));
  });
});
