/**
 * 116-hitl-resume fix — replay idempotence contract for clarificationPending.
 *
 * Background (t_2d813de63a4d @116 dogfood 2026-06-05): after HITL selection, FE calls
 * /resolve → /resume_turn → attachStream reconnect; Gateway fully replays events on reconnect
 * (chat.py stream semantics). Old clarification_required is redelivered, causing
 * reducer to restore clarificationPending just cleared by attachStream; HITL
 * card returns, suppressing resumed-pass rendering, so clicking appears ineffective.
 *
 * Contract: reducer must be idempotent for replay sequences:
 *   1. clarification.resolved reverses clarification_required
 *   2. done(status=completed) clears this turn's clarificationPending as fallback
 *   3. Other-turn done / other-request resolved must not wrongly clear it (stale-guard)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import {
  _resetClarificationTombstonesForTest,
  getAnsweredClarification,
  isClarificationAnswered,
} from "./clarification-tombstone";
import { useTurnRunner } from "./use-turn-runner";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";

const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_ES = globalThis.EventSource;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  url: string;
  readyState = 1;
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
    /* not needed for this contract */
  }
  close() {
    this.readyState = 2;
  }
  /** Test driver: simulate Gateway SSE delivery of one event. */
  emit(name: string, payload: Record<string, unknown>) {
    for (const fn of this.listeners.get(name) ?? []) {
      fn({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }
}

beforeEach(() => {
  // dingsiyan #11: module-level tombstones deliberately survive attachStream; reset between tests.
  _resetClarificationTombstonesForTest();
  _resetAllTurnsForTesting();
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
  vi.restoreAllMocks();
  _resetAllTurnsForTesting();
});

async function setupRunningTurn() {
  const hook = renderHook(() =>
    useTurnRunner({ mode: "sse", message: "查一下", workspaceId: "w", memberUid: "m", sendTick: 1 }),
  );
  await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
  // Stale-guard has a condition only once turnId is established.
  await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
  return { hook, es: FakeEventSource.instances[0] };
}

const CLAR_REQUIRED = {
  clarification_id: "c1",
  turn_id: "t_1",
  candidates: ["T2 是医院等级", "T2 是区域"],
  fallback_text: "T2 指什么？",
};

describe("clarificationPending replay idempotency", () => {
  it("a v1 send receipt cannot fall back to the legacy reducer", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return new Response(
          JSON.stringify({
            turn_id: "t_1",
            session_id: "s_1",
            stream_url: "/api/v1/chat/stream/t_1",
            terminal_protocol_version: 1,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as typeof fetch;
    const { hook, es } = await setupRunningTurn();

    act(() => es.emit("done", { status: "completed" }));

    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.streamingState).toBe("error");
    expect(hook.result.current.terminalStatus).toBe("failed");
  });

  it("protocol v1 keeps prior-generation done in history and closes only on current receipt", async () => {
    const { hook, es } = await setupRunningTurn();

    act(() => es.emit("stream.checkpoint", {
      terminal_protocol_version: 1,
      current_generation: 2,
      persistent_event_highwater: 3,
      current_receipt_id: "receipt_g2",
    }));
    act(() => es.emit("done", {
      terminal_protocol_version: 1,
      event_id: "event_g1_done",
      event_seq: 1,
      evt_seq: 1,
      generation: 1,
      terminal_current: false,
      terminal_receipt_id: "receipt_g1",
      status: "clarification_pending",
    }));

    expect(hook.result.current.running).toBe(true);
    expect(hook.result.current.terminalStatus).toBeNull();

    act(() => es.emit("final_answer", {
      terminal_protocol_version: 1,
      event_id: "event_g2_answer",
      event_seq: 2,
      evt_seq: 2,
      generation: 2,
      terminal_current: false,
      text: "resumed answer",
    }));
    act(() => es.emit("done", {
      terminal_protocol_version: 1,
      event_id: "event_g2_done",
      event_seq: 3,
      evt_seq: 3,
      generation: 2,
      terminal_current: true,
      terminal_receipt_id: "receipt_g2",
      status: "completed",
    }));

    expect(hook.result.current.archivedTurns?.at(-1)?.answer).toBe("resumed answer");
    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.terminalStatus).toBe("completed");
    expect(hook.result.current.terminalProtocolVersion).toBe(1);
    expect(
      hook.result.current.archivedTurns?.at(-1)
        ?.effectDisclosureDiscoveryPending,
    ).toBe(true);
    expect(hook.result.current.effectDisclosureDiscoveryPending).toBe(false);
  });

  it("protocol v1 cancelled terminal forces one exact disclosure read", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("stream.checkpoint", {
      terminal_protocol_version: 1,
      current_generation: 1,
      persistent_event_highwater: 1,
      current_receipt_id: "receipt_cancelled",
    }));
    act(() => es.emit("done", {
      terminal_protocol_version: 1,
      event_id: "event_cancelled_done",
      event_seq: 1,
      evt_seq: 1,
      generation: 1,
      terminal_current: true,
      terminal_receipt_id: "receipt_cancelled",
      status: "cancelled",
    }));

    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.terminalStatus).toBe("cancelled");
    expect(hook.result.current.effectDisclosureDiscoveryPending).toBe(true);
    expect(hook.result.current.archivedTurns ?? []).toHaveLength(0);
  });

  it("protocol v1 fails closed on an event-sequence gap", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("stream.checkpoint", {
      terminal_protocol_version: 1,
      current_generation: 1,
      persistent_event_highwater: 2,
      current_receipt_id: null,
    }));
    act(() => es.emit("final_answer", {
      terminal_protocol_version: 1,
      event_id: "event_2",
      event_seq: 2,
      evt_seq: 2,
      generation: 1,
      terminal_current: false,
      text: "must not apply",
    }));

    expect(hook.result.current.answer).toBe("");
    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.streamingState).toBe("error");
    expect(hook.result.current.terminalStatus).toBe("failed");
  });

  it("protocol v1 rejects business events before the checkpoint", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("final_answer", {
      terminal_protocol_version: 1,
      event_id: "event_1",
      event_seq: 1,
      evt_seq: 1,
      generation: 1,
      terminal_current: false,
      text: "must not apply",
    }));

    expect(hook.result.current.answer).toBe("");
    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.streamingState).toBe("error");
  });

  it("protocol v1 rejects a checkpoint that regresses below applied events", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("stream.checkpoint", {
      terminal_protocol_version: 1,
      current_generation: 1,
      persistent_event_highwater: 1,
      current_receipt_id: null,
    }));
    act(() => es.emit("final_answer", {
      terminal_protocol_version: 1,
      event_id: "event_1",
      event_seq: 1,
      evt_seq: 1,
      generation: 1,
      terminal_current: false,
      text: "applied",
    }));
    act(() => es.emit("stream.checkpoint", {
      terminal_protocol_version: 1,
      current_generation: 1,
      persistent_event_highwater: 0,
      current_receipt_id: null,
    }));

    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.streamingState).toBe("error");
    expect(hook.result.current.terminalStatus).toBe("failed");
  });

  it("protocol v1 rejects a current terminal from another generation", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("stream.checkpoint", {
      terminal_protocol_version: 1,
      current_generation: 2,
      persistent_event_highwater: 1,
      current_receipt_id: null,
    }));
    act(() => es.emit("done", {
      terminal_protocol_version: 1,
      event_id: "event_done",
      event_seq: 1,
      evt_seq: 1,
      generation: 1,
      terminal_current: true,
      terminal_receipt_id: "receipt_g1",
      status: "completed",
    }));

    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.streamingState).toBe("error");
    expect(hook.result.current.terminalStatus).toBe("failed");
  });

  it("protocol v1 rejects a current terminal bound to another receipt", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("stream.checkpoint", {
      terminal_protocol_version: 1,
      current_generation: 1,
      persistent_event_highwater: 1,
      current_receipt_id: "receipt_expected",
    }));
    act(() => es.emit("done", {
      terminal_protocol_version: 1,
      event_id: "event_done",
      event_seq: 1,
      evt_seq: 1,
      generation: 1,
      terminal_current: true,
      terminal_receipt_id: "receipt_other",
      status: "completed",
    }));

    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.streamingState).toBe("error");
    expect(hook.result.current.terminalStatus).toBe("failed");
  });

  it("peer_dispatch_failed records a peer failure without terminating the Lead turn", async () => {
    const { hook, es } = await setupRunningTurn();

    act(() => es.emit("peer_dispatch_failed", {
      card_id: "t_1",
      spawn_id: "peer_1",
      peer_name: "compute",
      reason_code: "peer_execution_timeout",
      error_preview: "Authorization: Bearer raw-secret",
    }));

    expect(hook.result.current.running).toBe(true);
    expect(hook.result.current.errorReason).toBeNull();
    expect(getTurnSnapshot("t_1")?.peerTerminalById.peer_1).toEqual({
      status: "failed",
      failureReason: "peer_execution_timeout",
    });
    expect(JSON.stringify(getTurnSnapshot("t_1"))).not.toContain("raw-secret");
  });

  it("clarification.resolved 清掉同 request 的 clarificationPending (replay 逆操作)", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");

    act(() => es.emit("clarification.resolved", { request_id: "c1", chosen_value: "T2 是医院等级" }));
    expect(hook.result.current.clarificationPending).toBeNull();
  });

  it("异 request 的 clarification.resolved 不误清", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    act(() => es.emit("clarification.resolved", { request_id: "OTHER", chosen_value: "x" }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
  });

  it("keeps the next question active when an older answered card replays", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    act(() => es.emit("clarification.resolved", { request_id: "c1", chosen_value: "2026" }));
    act(() => es.emit("turn.resumed", { turn_id: "t_1", request_id: "c1", execution_generation: 2, chosen_values: ["2026"] }));
    act(() => es.emit("clarification_required", { ...CLAR_REQUIRED, clarification_id: "c2", candidates: ["CNY", "USD"] }));
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    act(() => es.emit("clarification.resolved", { request_id: "c1", chosen_value: "2026" }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c2");
    expect(getAnsweredClarification("c1")?.values).toEqual(["2026"]);
    act(() => es.emit("clarification.resolved", { request_id: "c2", chosen_value: "CNY" }));
    expect(hook.result.current.clarificationPending).toBeNull();
    expect(getAnsweredClarification("c2")?.values).toEqual(["CNY"]);
    expect(getTurnSnapshot("t_1")?.beats.filter((beat) => beat.kind === "picker")).toHaveLength(2);
  });

  it("replays connector choices using the saved option label while retaining the identifier", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", {
      ...CLAR_REQUIRED,
      kind: "tool_context_select",
      options: [{ value: "1", label: "Retail workspace" }, { value: "6", label: "Other workspace" }],
    }));
    act(() => es.emit("clarification.resolved", { request_id: "c1", chosen_value: "1", chosen_values: ["1"] }));
    expect(hook.result.current.clarificationPending).toBeNull();
    expect(getAnsweredClarification("c1")).toMatchObject({ values: ["Retail workspace"], canonicalValues: ["1"] });
    act(() => es.emit("turn.resumed", { turn_id: "t_1", request_id: "c1", chosen_values: ["1"] }));
    expect(getAnsweredClarification("c1")).toMatchObject({ values: ["Retail workspace"], canonicalValues: ["1"] });
  });

  it("restores a connector label when the question arrives after its replayed answer", async () => {
    const { es } = await setupRunningTurn();
    act(() => es.emit("turn.resumed", { turn_id: "t_1", request_id: "c1", chosen_values: ["1"] }));
    act(() => es.emit("clarification_required", {
      ...CLAR_REQUIRED, kind: "tool_context_select", options: [{ value: "1", label: "Retail workspace" }],
    }));
    expect(getAnsweredClarification("c1")).toMatchObject({ values: ["Retail workspace"], canonicalValues: ["1"] });
  });

  it("clarification.dismissed 收掉 exact card 并留下 dismissed tombstone", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));

    act(() => es.emit("clarification.dismissed", {
      request_id: "c1",
      status: "expired",
      reason: "scheduled_input_timeout",
      actor: "system",
    }));

    expect(hook.result.current.clarificationPending).toBeNull();
    expect(getAnsweredClarification("c1")).toEqual({
      values: [],
      dismissed: true,
    });
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    expect(hook.result.current.clarificationPending).toBeNull();
  });

  it("异 request 的 clarification.dismissed 不误清当前卡", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    act(() => es.emit("clarification.dismissed", { request_id: "OTHER" }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
  });

  it.each([false, true])("removes answered cards while retaining unavailable continuation (%s)", async (unavailable) => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return new Response(JSON.stringify({
          turn_id: "t_1",
          session_id: "s_1",
          stream_url: "/api/v1/chat/stream/t_1",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (String(url).includes("/clarifications/turn/t_1")) {
        return new Response(JSON.stringify({ items: [], resume_failure: unavailable ? {
          request_id: "c1", turn_id: "t_1", execution_generation: 1,
          code: "clarification_resume_unavailable",
          recovery_action: "copy_diagnostics",
        } : null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));

    await waitFor(() => expect(hook.result.current.clarificationPending).toBeNull());
    expect(getAnsweredClarification("c1")?.dismissed).toBe(true);
    expect(hook.result.current.errorReason?.reasonCode ?? null).toBe(unavailable ? "clarification_resume_unavailable" : null);
    if (unavailable) {
      act(() => es.emit("done", { status: "completed", turn_id: "t_1" }));
      expect(hook.result.current.errorReason).toBeNull();
      expect(hook.result.current.archivedTurns?.at(-1)?.errorReason).toBeNull();
    }
  });

  it("可见卡 authority probe 命中当前 request 时保留", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return new Response(JSON.stringify({
          turn_id: "t_1",
          session_id: "s_1",
          stream_url: "/api/v1/chat/stream/t_1",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (String(url).includes("/clarifications/turn/t_1")) {
        return new Response(JSON.stringify({
          items: [{ request_id: "c1", turn_id: "t_1", status: "pending" }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/clarifications/turn/t_1"),
        expect.anything(),
      );
    });
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
  });

  it("authority probe 失败时 fail-open 保留当前卡", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return new Response(JSON.stringify({
          turn_id: "t_1",
          session_id: "s_1",
          stream_url: "/api/v1/chat/stream/t_1",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({ detail: { code: "temporary" } }), {
        status: 503,
        headers: { "content-type": "application/json" },
      });
    }) as typeof fetch;
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/clarifications/turn/t_1"),
        expect.anything(),
      );
    });
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
  });

  it.each(["clarification_pause_pending", "clarification_resume_unavailable"])("retains the original card when a deferred readiness read finishes after done (%s)", async (code) => {
    const originalFetch = globalThis.fetch;
    let finishRead!: (response: Response) => void;
    const firstRead = new Promise<Response>((resolve) => { finishRead = resolve; });
    let readCount = 0;
    let settledItems = [{ request_id: "c1", turn_id: "t_1", status: "pending" }];
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/clarifications/turn/t_1")) {
        readCount += 1;
        if (readCount === 1) return firstRead;
        return new Response(JSON.stringify({ items: settledItems }), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      return originalFetch(url, init);
    }) as typeof fetch;
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    await waitFor(() => expect(readCount).toBe(1));
    act(() => es.emit("done", { turn_id: "t_1", status: "clarification_pending" }));
    await act(async () => {
      finishRead(new Response(JSON.stringify({ detail: { code } }), {
        status: 409, headers: { "content-type": "application/json" },
      }));
    });
    await waitFor(() => expect(warning).toHaveBeenCalledWith(
      "clarification live authority probe deferred",
      expect.objectContaining({ status: 409, code }),
    ));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
    expect(isClarificationAnswered("c1")).toBe(false);

    // The same reader recognizes the original card after dispatch settles.
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    await waitFor(() => expect(readCount).toBe(2));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
    expect(isClarificationAnswered("c1")).toBe(false);

    // A later authoritative invalidation must still remove the original card.
    settledItems = [];
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    await waitFor(() => expect(hook.result.current.clarificationPending).toBeNull());
    expect(getAnsweredClarification("c1")?.dismissed).toBe(true);
  });

  it("ignores a superseded pending-list read after the next original card arrives", async () => {
    const originalFetch = globalThis.fetch;
    let finishRead!: (response: Response) => void;
    const firstRead = new Promise<Response>((resolve) => { finishRead = resolve; });
    let readCount = 0;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("/clarifications/turn/t_1")) {
        readCount += 1;
        if (readCount === 1) return firstRead;
        return new Response(JSON.stringify({
          items: [{ request_id: "c2", turn_id: "t_1", status: "pending" }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return originalFetch(url, init);
    }) as typeof fetch;
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    await waitFor(() => expect(readCount).toBe(1));
    act(() => es.emit("clarification_required", { ...CLAR_REQUIRED, clarification_id: "c2" }));
    await waitFor(() => expect(readCount).toBe(2));
    await act(async () => {
      finishRead(new Response(JSON.stringify({ items: [] }), {
        status: 200, headers: { "content-type": "application/json" },
      }));
    });
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c2");
    expect(isClarificationAnswered("c1")).toBe(false);
    expect(isClarificationAnswered("c2")).toBe(false);
  });

  it("页面 hidden 时不轮询 clarification authority", async () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    await act(async () => { await Promise.resolve(); });

    const urls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
      .map(([url]) => String(url));
    expect(urls.some((url) => url.includes("/clarifications/turn/"))).toBe(false);
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
  });

  it("done(status=completed) 兜底清掉本 turn 的 clarificationPending", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");

    act(() => es.emit("done", { turn_id: "t_1", status: "completed", duration_ms: 21217 }));
    expect(hook.result.current.clarificationPending).toBeNull();
  });

  it("异 turn 的 done(completed) 不误清 (stale-guard)", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    act(() => es.emit("done", { turn_id: "t_OTHER", status: "completed" }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
  });

  it("done(status=clarification_pending) 保留卡片 (turn 终结等用户选)", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", CLAR_REQUIRED));
    act(() => es.emit("done", { turn_id: "t_1", status: "clarification_pending" }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
  });

  it("clarification_required 保留 B5 questions 给 HITL picker 渲染", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", {
      ...CLAR_REQUIRED,
      questions: [
        {
          question: "主要看哪个区域?",
          options: ["全国", "华东"],
          type: "single_select",
        },
        {
          question: "重点看哪个指标?",
          options: ["销售额", "利润率"],
          type: "single_select",
        },
      ],
    }));

    expect(hook.result.current.clarificationPending?.questions).toEqual([
      {
        question: "主要看哪个区域?",
        options: ["全国", "华东"],
        type: "single_select",
      },
      {
        question: "重点看哪个指标?",
        options: ["销售额", "利润率"],
        type: "single_select",
      },
    ]);
  });

  it("retains the temporary workspace checkbox default in the live event", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", {
      ...CLAR_REQUIRED,
      kind: "tool_context_select",
      remember_option: true,
      remember_default: false,
    }));
    expect(hook.result.current.clarificationPending?.rememberDefault).toBe(false);
  });
});

describe("dingsiyan #11 — tombstone 防复活（真 payload seq / t_1ccf9e0aec11 形状）", () => {
  it("same-turn resume clears the paused summary and times a synthetic Stop from the resumed generation", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("turn_started", {
      turn_id: "t_1",
      seq: 1,
      started_at: "2026-08-08T00:59:26.000Z",
    }));
    act(() => es.emit("done", {
      turn_id: "t_1",
      seq: 11,
      status: "clarification_pending",
      duration_ms: 4276,
    }));
    expect(hook.result.current.summary?.total).toBe("4.3s");

    const resumeAttachedAt = Date.parse("2026-08-08T01:02:02.000Z");
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(resumeAttachedAt);
    act(() => {
      hook.result.current.attachStream?.("/api/v1/chat/stream/t_1", 2);
    });
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2));
    expect(hook.result.current.summary).toBeNull();
    expect(hook.result.current.startedAt).toBe(resumeAttachedAt);

    const es2 = FakeEventSource.instances[1];
    const authoritativeStart = "2026-08-08T01:02:02.339Z";
    act(() => es2.emit("turn_started", {
      turn_id: "t_1",
      seq: 14,
      started_at: authoritativeStart,
    }));
    expect(hook.result.current.startedAt).toBe(Date.parse(authoritativeStart));

    nowSpy.mockReturnValue(
      Date.parse(authoritativeStart) + 116_000,
    );
    act(() => es2.emit("done", {
      turn_id: "t_1",
      seq: 718,
      status: "cancelled",
    }));
    expect(hook.result.current.summary?.total).toBe("116.0s");
    expect(hook.result.current.terminalStatus).toBe("cancelled");
    nowSpy.mockRestore();
  });

  it("撞号 resolved 会被 seenSeq 丢弃（锁死 prod 死法——BE 必须播种 seq 的原因）", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("turn_started", { turn_id: "t_1", seq: 1 }));
    act(() => es.emit("clarification_required", { ...CLAR_REQUIRED, seq: 12 }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");

    // resolved from an unseeded bus: payload seq=1 collides with turn_started, so handle()
    // drops it before the reducer; the inverse never runs (observed production t_1ccf9e0aec11 shape).
    act(() => es.emit("clarification.resolved", {
      request_id: "c1", chosen_value: "T2 是医院等级", seq: 1,
    }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
  });

  it("turn.resumed 标记 tombstone + 清 pending；重连重放的 required 不再复活", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("turn_started", { turn_id: "t_1", seq: 1 }));
    act(() => es.emit("clarification_required", { ...CLAR_REQUIRED, seq: 12 }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");

    // Resume placeholder event (spawn_resume_turn emits before the worker).
    act(() => es.emit("turn.resumed", {
      turn_id: "t_1", request_id: "c1",
      chosen_values: ["T2 是医院等级"], skipped: false, seq: 16,
    }));
    expect(hook.result.current.clarificationPending).toBeNull();
    expect(isClarificationAnswered("c1")).toBe(true);

    // User clicks again/reconnects → attachStream reconnect (new ES/new seenSeq) → full server replay.
    act(() => { hook.result.current.attachStream?.("/api/v1/chat/stream/t_1"); });
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2));
    const es2 = FakeEventSource.instances[1];
    act(() => es2.emit("turn_started", { turn_id: "t_1", seq: 1 }));
    // Old bug: replay restores card unanswered (per-attach seenSeq cannot block seq12).
    act(() => es2.emit("clarification_required", { ...CLAR_REQUIRED, seq: 12 }));
    expect(hook.result.current.clarificationPending).toBeNull();
    // Dropping colliding resolved from old production rows is harmless: tombstone is already authoritative.
    act(() => es2.emit("clarification.resolved", { request_id: "c1", seq: 1 }));
    expect(hook.result.current.clarificationPending).toBeNull();
  });

  it("修复后世界全链：正确 seq 的 resolved 逆操作 + 重放被 tombstone 拦住", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("turn_started", { turn_id: "t_1", seq: 1 }));
    act(() => es.emit("clarification_required", { ...CLAR_REQUIRED, seq: 12 }));
    // After emit_out_of_band seeding: resolved seq = max+1, no collision → reaches reducer normally.
    act(() => es.emit("clarification.resolved", {
      request_id: "c1", chosen_value: "T2 是医院等级",
      chosen_values: ["T2 是医院等级"], seq: 16,
    }));
    expect(hook.result.current.clarificationPending).toBeNull();
    expect(isClarificationAnswered("c1")).toBe(true);

    act(() => { hook.result.current.attachStream?.("/api/v1/chat/stream/t_1"); });
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2));
    const es2 = FakeEventSource.instances[1];
    act(() => es2.emit("clarification_required", { ...CLAR_REQUIRED, seq: 12 }));
    expect(hook.result.current.clarificationPending).toBeNull();
  });
});

describe("connector-context T1#6: tool 判别符透传", () => {
  it("payload 带 tool → clarificationPending.tool 透传；不带 → undefined", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() =>
      es.emit("clarification_required", {
        ...CLAR_REQUIRED,
        kind: "tool_context_select",
        tool: "datarc",
      }),
    );
    expect(hook.result.current.clarificationPending?.tool).toBe("datarc");

    // Generic card (no tool field) → undefined (tsc's optional type fallback; rendering branches unaffected).
    act(() => es.emit("clarification.resolved", { request_id: "c1", chosen_value: "x" }));
    act(() => es.emit("clarification_required", { ...CLAR_REQUIRED, clarification_id: "c2" }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c2");
    expect(hook.result.current.clarificationPending?.tool).toBeUndefined();
  });
});

describe("graph-mode 批2: mid-run 卡与 running turn 共存(零先例形态, review P2-11/V5)", () => {
  it("running 中弹卡后 plan step 事件继续流, 卡不被清掉(卡开着 turn 还在流)", async () => {
    const { hook, es } = await setupRunningTurn();
    // Plan execution stream: step events keep arriving before and after the card appears.
    act(() => es.emit("step_started", { turn_id: "t_1", plan_id: "plan_1", level: 0, step_ids: ["ps_a"] }));
    act(() => es.emit("clarification_required", { ...CLAR_REQUIRED, kind: "tool_context_select" }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
    // Card stays open while other branches run; step_*/plan_* events must not clear clarificationPending.
    act(() => es.emit("step_done", { turn_id: "t_1", plan_id: "plan_1", step_id: "ps_a", digest: "d" }));
    act(() => es.emit("step_blocked", { turn_id: "t_1", plan_id: "plan_1", step_id: "ps_b", status: "blocked", error_kind: "needs_user_input", reason: "等待选择" }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");
    expect(hook.result.current.running).toBe(true);
  });

  it("终局过期 clarification.resolved(skipped=true) 收卡且 tombstone 记 skipped(不渲空「已确认」)", async () => {
    const { hook, es } = await setupRunningTurn();
    act(() => es.emit("clarification_required", { ...CLAR_REQUIRED, kind: "tool_context_select" }));
    expect(hook.result.current.clarificationPending?.clarificationId).toBe("c1");

    act(() => es.emit("clarification.resolved", { request_id: "c1", skipped: true }));
    expect(hook.result.current.clarificationPending).toBeNull();
    const answered = getAnsweredClarification("c1");
    expect(answered?.skipped).toBe(true);
    expect(answered?.values ?? []).toEqual([]);  // No chosen value → skip-style summary.
  });
});
