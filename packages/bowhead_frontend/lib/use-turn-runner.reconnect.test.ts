import { documentCanvasOpen } from "./document-canvas-events";
/**
 * fix/sse-reconnect-resilience — SSE resilience contract for long BI turns.
 *
 * Background (s_fa97c59ac8a5 / t_73ed54841e60 @116 dogfood 2026-06-09): long turns (5min+)
 * lost their connection behind Tencent CLB every ~60–90s. Old attachStream:
 *   - onerror closed immediately + set interrupted/running=false ⇒ lost terminal state on real disconnects;
 *   - watchdog returned early on readyState===OPEN (half-open socket) ⇒ turn stuck "运行中" forever;
 *   - server fully replayed on every reconnect, but FE did not deduplicate ⇒ legacy reducer counted twice.
 * Backend already supported reconnect (replay + done according to status, chat.py:962-991); all gaps were in FE.
 *
 * Contract:
 *   1. Real transport interruption ⇒ keep running, reconnect the same turn with backoff until terminal state (no freeze/false interruption).
 *   2. Server fully replays on reconnect ⇒ deduplicate by bus seq, without duplicate chips/steps.
 *   3. Half-open socket (readyState OPEN, no keepalive) ⇒ watchdog forcibly cycles the connection.
 *   4. A long quiet stretch asks the backend: keep following while the turn is alive or finished; give up only after
 *      its worker has stopped renewing the lease for a while (agent run limits, phase 3).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useTurnRunner } from "./use-turn-runner";
import { getCanvasSnapshot, resetCanvas } from "./canvas-store";

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
  removeEventListener() {
    /* not needed for these contracts */
  }
  close() {
    this.readyState = 2;
  }
  /** Test driver: simulate one event delivered by gateway SSE. */
  emit(name: string, payload: Record<string, unknown>) {
    for (const fn of this.listeners.get(name) ?? []) {
      fn({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }
  /** Test driver: simulate LB/network disconnect (native transport error). */
  fail() {
    this.readyState = 0; // CONNECTING: browser state on a native error.
    this.onerror?.();
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  (globalThis as Record<string, unknown>).EventSource = FakeEventSource;
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).includes("/chat/send")) {
      return new Response(
        JSON.stringify({
          turn_id: "t_1",
          session_id: "s_1",
          stream_url: "/api/v1/chat/stream/t_1",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  (globalThis as Record<string, unknown>).EventSource = ORIGINAL_ES;
  delete window.__BOWHEAD_BASE_PATH__;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function renderRunner() {
  return renderHook(() =>
    useTurnRunner({ mode: "sse", message: "查一下", workspaceId: "w", memberUid: "m", sendTick: 1 }),
  );
}

/** Advance fake time and flush microtasks (send IIFE's fetch / .json chain). */
async function flush(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("use-turn-runner SSE 韧性", () => {
  it.each(["", "/bowhead", "/ai/apps/bowhead"])("reconnects on the current browser entry %s and completes the turn", async (prefix) => {
    window.__BOWHEAD_BASE_PATH__ = prefix;
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es0 = FakeEventSource.instances[0];
    expect(es0.url).toBe(`${prefix}/api/v1/chat/stream/t_1`);

    act(() => es0.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下" }));
    act(() => es0.emit("tool_call", { seq: 2, call_id: "c1", tool_name: "datarc.query" }));
    expect(hook.result.current.running).toBe(true);
    expect(hook.result.current.chips.length).toBe(1);

    // LB disconnects mid-turn.
    act(() => es0.fail());
    // Do not give up: remain running, do not mark interrupted, show a "重连中" banner.
    expect(hook.result.current.running).toBe(true);
    expect(hook.result.current.interrupted ?? false).toBe(false);
    expect(hook.result.current.streamingState).toBe("error");
    expect(FakeEventSource.instances.length).toBe(1); // Backoff in progress, not reconnected yet.

    // Backoff (~1s) ⇒ reconnect the same turn.
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2), { timeout: 3000 });
    const es1 = FakeEventSource.instances[1];
    expect(es1.url).toBe(`${prefix}/api/v1/chat/stream/t_1`);

    // Server full replay (seq 1,2 already seen ⇒ deduplicate) + continued live events + done.
    act(() => es1.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下" }));
    act(() => es1.emit("tool_call", { seq: 2, call_id: "c1", tool_name: "datarc.query" }));
    expect(hook.result.current.chips.length).toBe(1); // Replay did not double the chip count.
    act(() => es1.emit("final_answer", { seq: 3, text: "答案" }));
    act(() => es1.emit("done", { seq: 4, turn_id: "t_1", status: "completed" }));

    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.streamingState).toBe("idle");
    // Completed turn folds into history (B7/W13): assistant answer enters messages; live answer field is cleared.
    const assistant = hook.result.current.messages.filter((m) => m.role === "assistant");
    expect(assistant.some((m) => m.content === "答案")).toBe(true);
  });

  it("重连全量 replay 的同 seq 事件被去重 (不重复 chips)", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es0 = FakeEventSource.instances[0];

    act(() => es0.emit("tool_call", { seq: 7, call_id: "c1", tool_name: "q" }));
    expect(hook.result.current.chips.length).toBe(1);
    // Reconnect semantics: redeliver the same event ⇒ must deduplicate by seq.
    act(() => es0.emit("tool_call", { seq: 7, call_id: "c1", tool_name: "q" }));
    act(() => es0.emit("tool_call", { seq: 7, call_id: "c1", tool_name: "q" }));
    expect(hook.result.current.chips.length).toBe(1);
    // Truly new events (new seq) must still apply.
    act(() => es0.emit("tool_call", { seq: 8, call_id: "c2", tool_name: "q2" }));
    expect(hook.result.current.chips.length).toBe(2);
  });

  it("无 seq 的事件 (synthetic replayed done / bypass-bus) 不被去重, 照常处理", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es0 = FakeEventSource.instances[0];

    // seq<1 (no seq) never enters seenSeq; both distinct tool_call events must apply.
    act(() => es0.emit("tool_call", { call_id: "c1", tool_name: "q" }));
    act(() => es0.emit("tool_call", { call_id: "c2", tool_name: "q2" }));
    expect(hook.result.current.chips.length).toBe(2);
    // synthetic durable done (chat.py — payload carries the exact turns.status, no seq)
    // Terminal state must still apply, or reconnecting to a completed turn would never receive done.
    act(() => es0.emit("done", { turn_id: "t_1", status: "completed" }));
    expect(hook.result.current.running).toBe(false);
  });

  it("keeps only the durable Subagent draft pointer from live or replayed events", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const eventSource = FakeEventSource.instances[0];

    act(() => eventSource.emit("cca.subagent_creator_open", {
      seq: 21,
      purpose_hint: "Analyze weekly sales",
      draft_id: "sad_live",
      draft_status: "ready",
      draft_title: "Weekly sales analyst",
      name_hint: "sales-helper",
      display_name_hint: "Sales helper",
      definition: { system_prompt: "must never be consumed from SSE" },
    }));

    expect(hook.result.current.subagentCreatorOpen).toEqual({
      purpose_hint: "Analyze weekly sales",
      draft_id: "sad_live",
      draft_status: "ready",
      draft_title: "Weekly sales analyst",
      draft_version: null,
      name_hint: "sales-helper",
      display_name_hint: "Sales helper",
    });

    act(() => eventSource.emit("cca.subagent_creator_open", {
      seq: 22,
      purpose_hint: "Analyze weekly sales",
      draft_id: "sad_live",
      draft_status: "ready",
      draft_title: "Updated sales analyst",
      draft_version: 4,
      name_hint: null,
      display_name_hint: null,
    }));
    expect(hook.result.current.subagentCreatorOpen).toMatchObject({
      draft_id: "sad_live",
      draft_version: 4,
      name_hint: "sales-helper",
      display_name_hint: "Sales helper",
    });

    // manual-card open carrying the user-stated name
    act(() => eventSource.emit("cca.subagent_creator_open", {
      seq: 23,
      purpose_hint: "Answer GitHub repo docs questions",
      draft_id: null,
      draft_status: null,
      draft_title: null,
      name_hint: "dogfood-wiki-helper",
      display_name_hint: "Wiki helper",
    }));
    expect(hook.result.current.subagentCreatorOpen).toMatchObject({
      name_hint: "dogfood-wiki-helper",
      display_name_hint: "Wiki helper",
    });

    act(() => eventSource.emit("cca.subagent_creator_open", {
      seq: 24,
      purpose_hint: "A different agent",
      draft_id: "sad_other",
      draft_status: "ready",
    }));
    expect(hook.result.current.subagentCreatorOpen).toMatchObject({
      draft_id: "sad_other",
      name_hint: null,
      display_name_hint: null,
    });
  });

  it("半开 socket (readyState OPEN, 无 keepalive) ⇒ watchdog 强制 cycle 连接", async () => {
    vi.useFakeTimers();
    const hook = renderRunner();
    // Flush send IIFE (fetch + .json microtask chain).
    for (let i = 0; i < 5 && FakeEventSource.instances.length === 0; i++) {
      await flush(0);
    }
    expect(FakeEventSource.instances.length).toBe(1);
    const es0 = FakeEventSource.instances[0];
    act(() => es0.emit("turn_started", { seq: 1 }));
    es0.readyState = 1; // Half-open: browser still considers it OPEN, but gateway no longer pushes (no keepalive).

    // Advance past STALL_RECONNECT_MS (40s); watchdog ticks every 5s.
    await flush(46_000);

    // Old implementation returned early on readyState===OPEN ⇒ stuck forever; new implementation forces a new connection.
    expect(FakeEventSource.instances.length).toBeGreaterThanOrEqual(2);
    expect(hook.result.current.running).toBe(true); // Still following, without falsely declaring failure.
    expect(FakeEventSource.instances[FakeEventSource.instances.length - 1].url)
      .toContain("/chat/stream/t_1");
  });

  const answerStatus = (body: Record<string, unknown>) => {
    const base = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/api/v1/turns/t_1/status")) {
        return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
      }
      return base(url, init);
    }) as typeof fetch;
  };

  it("a long quiet stretch keeps following while the backend reports the turn alive", async () => {
    vi.useFakeTimers();
    answerStatus({ terminal: false, alive: true });
    const hook = renderRunner();
    for (let i = 0; i < 5 && FakeEventSource.instances.length === 0; i++) {
      await flush(0);
    }
    const hookEs = FakeEventSource.instances[0];
    act(() => hookEs.emit("turn_started", { seq: 1 }));
    hookEs.readyState = 1;

    // Far past the old 10-minute give-up, with no new event: a long agent run is quiet but healthy.
    await flush(25 * 60_000);
    expect(hook.result.current.running).toBe(true);
    const count = FakeEventSource.instances.length;
    await flush(120_000);
    expect(FakeEventSource.instances.length).toBeGreaterThan(count);
    expect(globalThis.fetch).toHaveBeenCalledWith(expect.stringContaining("/api/v1/turns/t_1/status"), expect.anything());
  });

  it("gives up only after the turn's worker has stopped renewing its lease for a while", async () => {
    vi.useFakeTimers();
    answerStatus({ terminal: false, alive: false });
    const hook = renderRunner();
    for (let i = 0; i < 5 && FakeEventSource.instances.length === 0; i++) {
      await flush(0);
    }
    const hookEs = FakeEventSource.instances[0];
    act(() => hookEs.emit("turn_started", { seq: 1 }));
    hookEs.readyState = 1;

    await flush(601_000);
    expect(hook.result.current.running).toBe(true); // owner lost, but not yet for 2 minutes
    await flush(4 * 60_000);
    expect(hook.result.current.running).toBe(false);
    const countAtGiveUp = FakeEventSource.instances.length;
    await flush(120_000);
    expect(FakeEventSource.instances.length).toBe(countAtGiveUp);
  });
});

describe("use-turn-runner SSE 韧性 — canvas replay 幂等", () => {
  beforeEach(() => {
    resetCanvas();
  });

  const sectionMarkdown = (id: string): string | undefined =>
    (getCanvasSnapshot().sections.find((x) => x.id === id) as
      | { markdown?: string }
      | undefined)?.markdown;

  it("重连全量 replay 被 evt_seq 门丢弃, canvas 报告 prose 不翻倍 (TD-2)", async () => {
    // Long turn disconnected by CLB → FE reconnects → server fully replays. Canvas events bypass handle()'s
    // seq deduplication via independent canvas-sse listeners; attachStream's canvasGate (single evt_seq
    // gate) drops replay frames. Every real canvas event carries evt_seq (monotonic bus sequence, events.py). This guard
    // pins no double accumulation on reconnect, **decisively proving the gate blocks it, rather than replace-by-id**:
    // if replayed section_added were APPLIED, replace-by-id would clear and rebuild markdown; asserting
    // intact prose after replay proves section_added was dropped by the gate and never entered the reducer.
    // (dogfood t_be1bcc08 ordering failure came from a leftover getter stealing events from the backend's old single-consumer queue;
    //  the fan-out ring bus fixed that root cause.)
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es0 = FakeEventSource.instances[0];

    // Live: create section + stream prose chunks (delta append). Each carries evt_seq (= monotonic bus sequence).
    act(() => es0.emit("turn_started", { seq: 1, evt_seq: 1, session_id: "s_1" }));
    act(() => es0.emit("canvas.section_added", { seq: 0, evt_seq: 2, section: { id: "sec1", kind: "narrative" } }));
    act(() => es0.emit("canvas.section_chunk", { evt_seq: 3, section_id: "sec1", delta: "分析核心" }));
    act(() => es0.emit("canvas.section_chunk", { evt_seq: 4, section_id: "sec1", delta: "年中达成" }));
    expect(sectionMarkdown("sec1")).toBe("分析核心年中达成");

    // LB disconnect → reconnect with backoff.
    act(() => es0.fail());
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2), { timeout: 3000 });
    const es1 = FakeEventSource.instances[1];

    // Server full replay with the same evt_seq 2–4 (canvas events also carry bus evt_seq).
    act(() => es1.emit("turn_started", { seq: 1, evt_seq: 1, session_id: "s_1" }));
    // Decisive assertion: prose stays intact after replaying section_added(evt_seq=2) → the gate dropped it,
    // rather than replace-by-id clearing and rebuilding it (which would leave an empty "" section here).
    act(() => es1.emit("canvas.section_added", { seq: 0, evt_seq: 2, section: { id: "sec1", kind: "narrative" } }));
    expect(sectionMarkdown("sec1")).toBe("分析核心年中达成");
    // Replayed chunks are also dropped by the gate → no doubling (old ungated implementation = "分析核心年中达成分析核心年中达成").
    act(() => es1.emit("canvas.section_chunk", { evt_seq: 3, section_id: "sec1", delta: "分析核心" }));
    act(() => es1.emit("canvas.section_chunk", { evt_seq: 4, section_id: "sec1", delta: "年中达成" }));
    expect(sectionMarkdown("sec1")).toBe("分析核心年中达成");

    // Continued real live chunk (new evt_seq=5 > gate max=4) must still append.
    act(() => es1.emit("canvas.section_chunk", { evt_seq: 5, section_id: "sec1", delta: "情况良好" }));
    expect(sectionMarkdown("sec1")).toBe("分析核心年中达成情况良好");
  });

  it("无 evt_seq 的 canvas 事件 (ship 前旧持久化 / 合成) fail-open 照常处理", async () => {
    // Deduplicate only when evt_seq exists; otherwise pass through, protecting old-turn replay during transition.
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es0 = FakeEventSource.instances[0];
    act(() => es0.emit("canvas.section_added", { section: { id: "sec9", kind: "narrative" } }));
    act(() => es0.emit("canvas.section_chunk", { section_id: "sec9", delta: "无门" }));
    expect(sectionMarkdown("sec9")).toBe("无门");
  });
});

it("projects the exact settings Tool pointer and proposal through live and replay", async () => {
  const hook = renderRunner();
  await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
  const eventSource = FakeEventSource.instances[0];
  const pointer = { page: "workspace_i18n", user_id: "u_original", team_id: "team_original", workspace_id: "ws_original" };
  act(() => eventSource.emit("configuration.canvas_open", { seq: 30, turn_id: "t_1", target: pointer }));
  expect(hook.result.current.configurationCanvasOpen).toEqual({ turnId: "t_1", target: { page: "workspace_i18n", userId: "u_original", teamId: "team_original", workspaceId: "ws_original" } });
  act(() => eventSource.emit("config.lite_propose", { seq: 31, turn_id: "t_1", proposal_id: "ccd_1", form_id: "workspace_i18n", rows: [] }));
  act(() => eventSource.emit("config.lite_propose", { seq: 31, turn_id: "t_1", proposal_id: "ccd_1", form_id: "workspace_i18n", rows: [] }));
  expect(hook.result.current.settingsProposalIds).toEqual(["ccd_1"]);
  act(() => eventSource.emit("configuration.canvas_open", { seq: 32, turn_id: "t_1", target: { ...pointer, page: "unknown" } }));
  expect(hook.result.current.configurationCanvasOpen?.target.page).toBe("workspace_i18n");
});


it("projects the subscribed document event and fences a mismatched transport Turn", async () => {
  const hook = renderRunner();
  await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
  const pointer = { version: 1, turn_id: "t_1", session_id: "s_1", user_id: "u_original",
    team_id: "team_original", workspace_id: "ws_original", job_id: "djob_original", draft_digest: "a".repeat(64) };
  act(() => FakeEventSource.instances[0].emit("document.draft_open", { ...pointer, seq: 30 }));
  expect(documentCanvasOpen(hook.result.current.turnId)).toEqual(pointer);
  act(() => FakeEventSource.instances[0].emit("document.draft_open", { ...pointer, turn_id: "t_foreign_open", seq: 31 }));
  expect(documentCanvasOpen("t_foreign_open")).toBeNull();
});
