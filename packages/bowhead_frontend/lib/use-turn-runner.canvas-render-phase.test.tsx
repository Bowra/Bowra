/**
 * Terminal canvas-store effects run outside the runner's state updaters.
 *
 * A queued `setState(updater)` runs while the hook's host component renders. A canvas-store write inside it
 * notifies every subscriber (ChatWorkbench reads the whole canvas snapshot), and React then warns
 * "Cannot update a component (`ChatWorkbench`) while rendering a different component (`CanvasHomeInner`)" —
 * seen when a hosted Skill opened from chat gave the page pending work just before the turn's `done`.
 */
import { act, render, waitFor } from "@testing-library/react";
import { useSyncExternalStore } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getCanvasSnapshot, onSectionStreaming, resetCanvas, subscribeCanvas, upsertPyCell } from "./canvas-store";
import { useTurnRunner } from "./use-turn-runner";

const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_ES = globalThis.EventSource;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
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
    for (const fn of this.listeners.get(name) ?? []) fn({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).includes("/chat/send")) {
      return new Response(JSON.stringify({ turn_id: "t_1", session_id: "s_1", stream_url: "/api/v1/chat/stream/t_1" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
});

afterEach(() => {
  resetCanvas();
  globalThis.fetch = ORIGINAL_FETCH;
  (globalThis as Record<string, unknown>).EventSource = ORIGINAL_ES;
  vi.restoreAllMocks();
});

const RUNNER_ARGS = { mode: "sse" as const, message: "打开技能", workspaceId: "w", memberUid: "m", sendTick: 1 };

let latestTurnId: string | null = null;

/**
 * A runner host (stands in for CanvasHomeInner) next to a sibling subscribed to the whole canvas snapshot
 * (stands in for ChatWorkbench). React reports this warning once per component pair, so each case gets
 * its own named pair; otherwise the first failing case would hide the others.
 */
function renderPage(label: string) {
  function RunnerHost() {
    const runner = useTurnRunner(RUNNER_ARGS);
    latestTurnId = runner.turnId;
    return null;
  }
  function CanvasSubscriber() {
    useSyncExternalStore(subscribeCanvas, getCanvasSnapshot, getCanvasSnapshot);
    return null;
  }
  RunnerHost.displayName = `RunnerHost_${label}`;
  CanvasSubscriber.displayName = `CanvasSubscriber_${label}`;
  render(
    <>
      <RunnerHost />
      <CanvasSubscriber />
    </>,
  );
}

const renderPhaseWarnings = (spy: ReturnType<typeof vi.spyOn>) =>
  spy.mock.calls.filter((call: unknown[]) => String(call[0]).includes("Cannot update a component"));

describe("useTurnRunner terminal canvas effects", () => {
  it.each([
    ["done (completed)", "done", { status: "completed" }],
    ["turn_failed", "turn_failed", { reason_code: "lead_internal_error" }],
    ["turn_aborted", "turn_aborted", {}],
  ])("%s never writes the canvas store while the host renders", async (_label, terminal, payload) => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    renderPage(terminal);
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(latestTurnId).toBe("t_1"));
    const es = FakeEventSource.instances[0];
    // Give each terminal effect something to change: a buffered Lead section of this Turn (reveal / clear /
    // prune) and a Python cell (the completed done's archive clears pyCells).
    act(() => {
      onSectionStreaming({ section_id: "sec_lead_t_1_001", kind: "narrative" as never });
      upsertPyCell({ n: 1, name: "In[1]", status: "success", code: "1", output: null, durMs: null, turnId: "t_1" });
    });
    consoleError.mockClear();
    // Two frames in one batch: the first update stays pending, so the terminal frame's updater runs during
    // the host's render — the situation a hosted Skill opening just before `done` creates.
    act(() => {
      es.emit("partial_answer", { seq: 1, turn_id: "t_1", text: "已打开" });
      es.emit(terminal, { seq: 2, turn_id: "t_1", ...payload });
    });
    // Deferred store writes (the pyCells clear) settle after the render.
    await act(async () => { await Promise.resolve(); });
    expect(renderPhaseWarnings(consoleError)).toEqual([]);
  });
});
