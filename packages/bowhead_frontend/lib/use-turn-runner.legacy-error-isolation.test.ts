/** Legacy terminal error fields must never become a user-visible detail. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useTurnRunner } from "./use-turn-runner";
import { _resetAllTurnsForTesting } from "./turn-store";

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
  emit(name: string, payload: Record<string, unknown>) {
    for (const fn of this.listeners.get(name) ?? []) {
      fn({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }
}

beforeEach(() => {
  _resetAllTurnsForTesting();
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
  _resetAllTurnsForTesting();
});

async function setupRunningTurn() {
  const hook = renderHook(() =>
    useTurnRunner({ mode: "sse", message: "查一下", workspaceId: "w", memberUid: "m", sendTick: 1 }),
  );
  await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
  await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
  return { hook, es: FakeEventSource.instances[0] };
}

describe("legacy terminal error isolation", () => {
  it.each(["document_information_incomplete", "document_planning_failed", "document_history_unavailable", "document_request_rejected", "document_routing_invalid", "document_routing_uncertain", "document_routing_source_unavailable"])("preserves the document result in a failed done (%s)", async (reasonCode) => {
    const { hook, es } = await setupRunningTurn();
    const retryable = reasonCode === "document_routing_invalid";
    act(() => es.emit("done", {
      turn_id: "t_1", status: "failed", reason_code: reasonCode,
      error: "private domain error must not be displayed",
      diagnostics: {
        schema_version: 1, disclosure: "authenticated", diagnostic_code: "chat_turn_failed", retryable,
        occurrence_id: "0123456789abcdef0123456789abcdef", turn_id: "t_1",
        safe_detail: `phase=chat; failure_class=${reasonCode === "document_information_incomplete" || reasonCode === "document_request_rejected" || reasonCode === "document_routing_uncertain" ? "content" : reasonCode === "document_history_unavailable" ? "configuration" : reasonCode === "document_routing_source_unavailable" ? "access" : "runtime"}`,
      },
    }));
    expect(hook.result.current.errorReason).toMatchObject({
      source: "turn_failed", reasonCode, diagnostics: { retryable },
    });
  });
  it("projects Canvas failures onto finite codes with typed diagnostics", async () => {
    const { hook, es } = await setupRunningTurn();

    act(() =>
      es.emit("canvas_op_failed", {
        turn_id: "t_1",
        op: "compose",
        reason_code: "canvas_compose_timeout",
        error: "Authorization: Bearer raw-secret",
        diagnostics: {
          schema_version: 1,
          disclosure: "authenticated",
          diagnostic_code: "chat_canvas_failed",
          occurrence_id: "0123456789abcdef0123456789abcdef",
          turn_id: "t_1",
          safe_detail: "phase=canvas; failure_class=timeout",
          retryable: true,
        },
      }),
    );

    expect(hook.result.current.errorReason).toMatchObject({
      source: "canvas_op_failed",
      reasonCode: "canvas_compose_timeout",
      diagnostics: {
        diagnosticCode: "chat_canvas_failed",
        occurrenceId: "0123456789abcdef0123456789abcdef",
        turnId: "t_1",
      },
    });
    expect(JSON.stringify(hook.result.current.errorReason)).not.toContain(
      "raw-secret",
    );
  });

  it("restores diagnostics when a failed done is the only replayed terminal", async () => {
    const { hook, es } = await setupRunningTurn();

    act(() =>
      es.emit("done", {
        turn_id: "t_1",
        status: "failed",
        reason_code: "plan_execution_failed",
        diagnostics: {
          schema_version: 1,
          disclosure: "authenticated",
          diagnostic_code: "chat_turn_failed",
          occurrence_id: "0123456789abcdef0123456789abcdef",
          turn_id: "t_1",
          safe_detail: "phase=chat; failure_class=plan",
          retryable: true,
        },
      }),
    );

    expect(hook.result.current.errorReason).toMatchObject({
      source: "turn_failed",
      reasonCode: "plan_execution_failed",
      diagnostics: {
        diagnosticCode: "chat_turn_failed",
        occurrenceId: "0123456789abcdef0123456789abcdef",
        turnId: "t_1",
      },
    });
  });

  it("reads the public reason_code when turn_failed also carries an internal reason", async () => {
    // Restart test on 116: lease recovery writes turn_failed as {reason: internal string, reason_code: public code}.
    const { hook, es } = await setupRunningTurn();
    act(() =>
      es.emit("turn_failed", {
        turn_id: "t_1",
        reason: "turn_dispatch_owner_lost",
        reason_code: "service_interrupted",
      }),
    );
    expect(hook.result.current.errorReason).toMatchObject({
      source: "turn_failed",
      reasonCode: "service_interrupted",
    });
  });

  it("rebuilds the same diagnostics after a fresh replay connection", async () => {
    const diagnostics = {
      schema_version: 1,
      disclosure: "authenticated",
      diagnostic_code: "chat_turn_failed",
      occurrence_id: "0123456789abcdef0123456789abcdef",
      turn_id: "t_1",
      safe_detail: "phase=chat; failure_class=timeout",
      retryable: true,
    };
    const emitTerminalPair = (es: FakeEventSource) => {
      act(() =>
        es.emit("turn_failed", {
          turn_id: "t_1",
          reason: "wallclock_exceeded",
          diagnostics,
        }),
      );
      act(() =>
        es.emit("done", {
          turn_id: "t_1",
          status: "error",
          reason_code: "wallclock_exceeded",
          diagnostics,
        }),
      );
    };

    const live = await setupRunningTurn();
    emitTerminalPair(live.es);
    const liveProjection = live.hook.result.current.errorReason?.diagnostics;
    live.hook.unmount();

    _resetAllTurnsForTesting();
    FakeEventSource.instances = [];
    const replay = await setupRunningTurn();
    emitTerminalPair(replay.es);

    expect(replay.hook.result.current.errorReason?.diagnostics).toEqual(
      liveProjection,
    );
  });

  it("keeps one versioned diagnostics object across turn_failed and done", async () => {
    const { hook, es } = await setupRunningTurn();
    const diagnostics = {
      schema_version: 1,
      disclosure: "authenticated",
      diagnostic_code: "chat_turn_failed",
      occurrence_id: "0123456789abcdef0123456789abcdef",
      turn_id: "t_1",
      safe_detail: "phase=chat; failure_class=timeout",
      retryable: true,
    };

    act(() =>
      es.emit("turn_failed", {
        turn_id: "t_1",
        reason: "wallclock_exceeded",
        diagnostics,
      }),
    );
    const first = hook.result.current.errorReason?.diagnostics;
    act(() =>
      es.emit("done", {
        turn_id: "t_1",
        status: "error",
        reason_code: "wallclock_exceeded",
        error: "wallclock_exceeded",
        diagnostics,
      }),
    );

    expect(first).toEqual({
      schemaVersion: 1,
      disclosure: "authenticated",
      code: "wallclock_exceeded",
      diagnosticCode: "chat_turn_failed",
      occurrenceId: diagnostics.occurrence_id,
      turnId: "t_1",
      safeDetail: "phase=chat; failure_class=timeout",
      retryable: true,
    });
    expect(hook.result.current.errorReason?.diagnostics).toEqual(first);
  });

  it("keeps typed LLM diagnostics across turn_failed and done", async () => {
    const { hook, es } = await setupRunningTurn();
    const diagnostics = {
      schema_version: 1,
      disclosure: "authenticated",
      diagnostic_code: "chat_llm_failed",
      occurrence_id: "0123456789abcdef0123456789abcdef",
      turn_id: "t_1",
      safe_detail: (
        "phase=stream; failure_class=upstream; " +
        "provider_family=openai_compatible; http_status=500; " +
        "stream_state=pre_visible; fallback=failed; attempts=3"
      ),
      retryable: true,
    };

    act(() =>
      es.emit("turn_failed", {
        turn_id: "t_1",
        reason: "llm_provider_unavailable",
        diagnostics,
      }),
    );
    const first = hook.result.current.errorReason?.diagnostics;
    act(() =>
      es.emit("done", {
        turn_id: "t_1",
        status: "error",
        reason_code: "llm_provider_unavailable",
        diagnostics,
      }),
    );

    expect(first).toMatchObject({
      code: "llm_provider_unavailable",
      diagnosticCode: "chat_llm_failed",
      occurrenceId: diagnostics.occurrence_id,
      turnId: "t_1",
    });
    expect(hook.result.current.errorReason?.diagnostics).toEqual(first);
  });

  it("keeps the first valid diagnostics when done disagrees", async () => {
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { hook, es } = await setupRunningTurn();

    act(() =>
      es.emit("turn_failed", {
        turn_id: "t_1",
        reason: "lead_internal_error",
        diagnostics: {
          schema_version: 1,
          disclosure: "authenticated",
          diagnostic_code: "chat_turn_failed",
          occurrence_id: "0123456789abcdef0123456789abcdef",
          turn_id: "t_1",
          safe_detail: "phase=chat; failure_class=internal",
          retryable: true,
        },
      }),
    );
    act(() =>
      es.emit("done", {
        turn_id: "t_1",
        status: "error",
        reason_code: "lead_internal_error",
        diagnostics: {
          schema_version: 1,
          disclosure: "authenticated",
          diagnostic_code: "chat_turn_failed",
          occurrence_id: "fedcba9876543210fedcba9876543210",
          turn_id: "t_1",
          safe_detail: "phase=chat; failure_class=internal",
          retryable: true,
        },
      }),
    );

    expect(
      hook.result.current.errorReason?.diagnostics?.occurrenceId,
    ).toBe("0123456789abcdef0123456789abcdef");
    expect(warning).toHaveBeenCalledWith(
      "chat.turn_failure_diagnostics_mismatch",
      {
        turnId: "t_1",
        reasonCode: "lead_internal_error",
      },
    );
  });

  it("rejects diagnostics for another turn and any legacy raw fallback", async () => {
    const { hook, es } = await setupRunningTurn();

    act(() =>
      es.emit("turn_failed", {
        turn_id: "t_1",
        reason: "lead_internal_error",
        details: "password=raw-secret",
        diagnostics: {
          schema_version: 1,
          disclosure: "authenticated",
          diagnostic_code: "chat_turn_failed",
          occurrence_id: "0123456789abcdef0123456789abcdef",
          turn_id: "t_other",
          safe_detail: "phase=chat; failure_class=internal",
          retryable: true,
        },
      }),
    );

    expect(hook.result.current.errorReason?.diagnostics).toBeUndefined();
    expect(JSON.stringify(hook.result.current.errorReason)).not.toContain(
      "raw-secret",
    );
  });

  it("ignores turn_failed.details and error", async () => {
    const { hook, es } = await setupRunningTurn();

    act(() =>
      es.emit("turn_failed", {
        turn_id: "t_1",
        reason: "lead_internal_error",
        details: "LLMTimeoutError: upstream did not respond",
        error: "Authorization: Bearer raw-secret",
      }),
    );

    expect(hook.result.current.errorReason?.reasonCode).toBe("lead_internal_error");
    expect(JSON.stringify(hook.result.current.errorReason)).not.toContain("upstream did not respond");
    expect(JSON.stringify(hook.result.current.errorReason)).not.toContain("raw-secret");
  });

  it("ignores done.error even when it contains a longer raw exception", async () => {
    const { hook, es } = await setupRunningTurn();

    act(() => es.emit("turn_failed", { turn_id: "t_1", reason: "lead_internal_error" }));
    act(() =>
      es.emit("done", {
        turn_id: "t_1",
        status: "error",
        reason_code: "lead_internal_error",
        error: "ConnectionResetError: sandbox executor died mid-run",
      }),
    );

    expect(hook.result.current.errorReason?.reasonCode).toBe("lead_internal_error");
    expect(JSON.stringify(hook.result.current.errorReason)).not.toContain("sandbox executor");
  });

  it("rejects unknown code-shaped reasons and safely maps known legacy prefixes", async () => {
    const first = await setupRunningTurn();

    act(() =>
      first.es.emit("turn_failed", {
        turn_id: "t_1",
        reason: "api_key_secret_should_not_be_a_public_code",
      }),
    );
    expect(first.hook.result.current.errorReason?.reasonCode).toBe("lead_internal_error");

    first.hook.unmount();
    FakeEventSource.instances = [];
    const second = await setupRunningTurn();
    act(() =>
      second.es.emit("turn_failed", {
        turn_id: "t_1",
        reason: "token_cap_exhausted: 1000 >= 1000",
      }),
    );
    expect(second.hook.result.current.errorReason?.reasonCode).toBe("token_cap_exhausted");
  });

  it("does not invent an errorReason when the user cancelled the turn", async () => {
    const { hook, es } = await setupRunningTurn();

    // Cancel terminates through `done` too — with error="user_cancel" and no
    // reason_code. Reacting to the field alone would show a failure banner.
    act(() =>
      es.emit("done", {
        turn_id: "t_1",
        status: "aborted",
        error: "user_cancel",
        error_kind: "aborted",
      }),
    );

    expect(hook.result.current.errorReason).toBeNull();
  });

  it("does not invent an errorReason on a completed turn", async () => {
    const { hook, es } = await setupRunningTurn();

    act(() => es.emit("done", { turn_id: "t_1", status: "completed", error: null }));

    expect(hook.result.current.errorReason).toBeNull();
  });
});
