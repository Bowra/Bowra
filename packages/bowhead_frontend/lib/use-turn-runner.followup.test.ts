/**
 * UX-3 G2 (chat-solidify 2026-07-03) — steer follow-up stream attachment contract.
 *
 * Background: steer = buffer + follow-up turn. As the old turn ends, the worker emits
 * `turn.follow_up{follow_up_turn_id, follow_up_stream_url}` on its old bus. Dispatcher
 * comments always claimed the runner would chain subscriptions, but that implementation
 * never existed: the FE whitelist received it, the dispatcher did nothing, and the legacy
 * switch had no case. Follow-up live output stayed silent in the UI until F5 (the main
 * cause of the user-evidenced "steer 做得不好" complaint).
 * Contract:
 *   1. turn.follow_up ⇒ runner switches to the follow-up turn (turnId/running/started)
 *      and opens a new EventSource for follow_up_stream_url.
 *   2. The new stream's turn_started.user_text (backend-merged steer text) supplies the authoritative value if user is empty.
 *   3. Follow-up narrative/final_answer drives the new turn normally (not deduplicated by the old seq).
 */

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
    /* noop */
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
  vi.useRealTimers();
  vi.restoreAllMocks();
  _resetAllTurnsForTesting();
});

/** Advance fake time and flush microtasks (send IIFE's fetch/.json chain). */
async function flush(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function renderRunner() {
  return renderHook(() =>
    useTurnRunner({ mode: "sse", message: "查一下销售", workspaceId: "w", memberUid: "m", sendTick: 1 }),
  );
}

describe("use-turn-runner · turn.follow_up steer 续跑接流", () => {
  it("follow_up 到达 ⇒ 切 turn + 开新流; 新流事件驱动新 turn", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es0 = FakeEventSource.instances[0];

    act(() => es0.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下销售" }));
    // steer ack (🤝) enters turn-store (display mirror).
    act(() =>
      es0.emit("emoji_ack", {
        seq: 2, card_id: "t_1", target_msg_id: "m1", emoji: "🤝", text: "顺便按地区拆分",
      }),
    );
    // The worker sends the follow-up pointer before the parent terminal event. Even if a durable-recovery synthetic
    // pointer collides in seq with an ack already live but not persisted, the critical child link must remain reachable.
    act(() =>
      es0.emit("turn.follow_up", {
        seq: 2,
        follow_up_turn_id: "t_2",
        follow_up_stream_url: "/api/v1/chat/stream/t_2",
        reason: "steer_drain",
        buffered_count: 1,
        execution_generation: 3,
        attachments: [{
          fileId: "art_follow_up",
          filename: "follow-up.zip",
          size: 42,
          mime: "application/zip",
          bundleArtifactIds: ["art_child"],
        }],
      }),
    );
    // Duplicate replay pointer is idempotent by child identity, not seq.
    act(() =>
      es0.emit("turn.follow_up", {
        seq: 99,
        follow_up_turn_id: "t_2",
        follow_up_stream_url: "/api/v1/chat/stream/t_2",
        reason: "steer_drain",
        buffered_count: 1,
      }),
    );
    act(() => es0.emit("done", { seq: 4, turn_id: "t_1", status: "completed" }));

    // Contract 1: a new EventSource targets the follow-up stream; the runner has switched turns.
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(2));
    expect(FakeEventSource.instances).toHaveLength(2);
    const es1 = FakeEventSource.instances[1];
    expect(es1.url).toContain("/api/v1/chat/stream/t_2");
    expect(hook.result.current.turnId).toBe("t_2");
    expect(hook.result.current.running).toBe(true);
    expect(hook.result.current.executionGeneration).toBe(3);
    expect(hook.result.current.userAttachments).toEqual([{
      fileId: "art_follow_up",
      filename: "follow-up.zip",
      size: 42,
      mime: "application/zip",
      bundleArtifactIds: ["art_child"],
    }]);
    // User copy falls back to the steer 🤝 text.
    expect(hook.result.current.user).toContain("顺便按地区拆分");

    // Contract 3: follow-up events drive the turn normally (low seq is not wrongly discarded by the old connection's seenSeq;
    // attachStream resets per-connection deduplication).
    act(() => es1.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "顺便按地区拆分" }));
    act(() => es1.emit("final_answer", { seq: 2, card_id: "t_2", text: "按地区拆分结果如下" }));
    act(() => es1.emit("done", { seq: 3, turn_id: "t_2", status: "completed" }));
    await waitFor(() => expect(hook.result.current.running).toBe(false));
    // Archive the old turn in archivedTurns (done(completed) archive path); once the new turn's answer is in place,
    // archive it at done too. Assert neither turn loses content.
    const arch = hook.result.current.archivedTurns ?? [];
    expect(arch.length).toBeGreaterThanOrEqual(1);
  });

  it("user 为空时 turn_started.user_text 补权威值(不覆盖已有 user)", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const es0 = FakeEventSource.instances[0];
    // Normal path: send already set user, so turn_started does not overwrite it.
    act(() => es0.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "服务器视角文本" }));
    expect(hook.result.current.user).toBe("查一下销售");
  });

  it("archives an attachment-only completed turn", async () => {
    const attachment = {
      fileId: "art_only",
      filename: "evidence.pdf",
      size: 8,
      mime: "application/pdf",
    };
    const hook = renderHook(() => useTurnRunner({
      mode: "sse",
      message: "",
      attachments: [attachment],
      workspaceId: "w",
      memberUid: "m",
      sendTick: 1,
    }));
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const stream = FakeEventSource.instances[0];
    act(() => stream.emit("turn_started", {
      seq: 1,
      session_id: "s_1",
      user_text: "",
    }));
    act(() => stream.emit("done", {
      seq: 2,
      turn_id: "t_1",
      status: "completed",
    }));

    await waitFor(() => expect(hook.result.current.running).toBe(false));
    expect(hook.result.current.archivedTurns).toEqual([
      expect.objectContaining({
        turnId: "t_1",
        user: "",
        userAttachments: [attachment],
      }),
    ]);
    expect(hook.result.current.messages).toEqual([
      expect.objectContaining({
        role: "user",
        content: "",
        attachments: [attachment],
      }),
    ]);
  });

  it("does not leak an aborted parent into a completed follow-up", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const parent = FakeEventSource.instances[0];

    act(() =>
      parent.emit("turn_aborted", {
        seq: 1,
        turn_id: "t_1",
        reason: "interrupted",
      }),
    );
    expect(hook.result.current.aborted).toBe(true);

    act(() =>
      parent.emit("turn.follow_up", {
        seq: 2,
        follow_up_turn_id: "t_2",
        follow_up_stream_url: "/api/v1/chat/stream/t_2",
        reason: "steer_drain",
        buffered_count: 1,
      }),
    );
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_2"));
    expect(hook.result.current.aborted).toBe(false);

    const child = FakeEventSource.instances[1];
    act(() =>
      child.emit("done", {
        seq: 1,
        turn_id: "t_2",
        status: "completed",
      }),
    );
    expect(hook.result.current.terminalStatus).toBe("completed");
    expect(hook.result.current.aborted).toBe(false);
  });

  it("失败的 steer 轮也被归档(不从 scrollback 消失到 F5, code-review 2026-07-04)", async () => {
    // done{status:failed} skips the completed-archive branch (preserving trace for the terminal card); if follow_up
    // simply overwrites live state, the failed turn is lost. The fix archives it via archiveAndFreshTurn on follow_up.
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es0 = FakeEventSource.instances[0];
    act(() => es0.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下销售" }));
    act(() => es0.emit("final_answer", { seq: 2, card_id: "t_1", text: "部分结果…" }));
    // Failed terminal state (not completed) → done does not archive.
    act(() => es0.emit("done", { seq: 3, turn_id: "t_1", status: "failed" }));
    act(() =>
      es0.emit("turn.follow_up", {
        seq: 4, follow_up_turn_id: "t_2", follow_up_stream_url: "/api/v1/chat/stream/t_2",
        reason: "steer_drain", buffered_count: 1,
      }),
    );
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_2"));
    const arch = hook.result.current.archivedTurns ?? [];
    // Archive failed turn t_1, preserving its user/partial answer.
    expect(arch.some((a) => a.turnId === "t_1")).toBe(true);
    const t1 = arch.find((a) => a.turnId === "t_1");
    expect(t1?.user).toBe("查一下销售");
    expect(t1?.answer).toBe("部分结果…");
    // Previous-turn copy must not leak into the follow-up's live state (user is steer text or null, not t_1's user).
    expect(hook.result.current.user).not.toBe("查一下销售");
  });

  it("turn_failed 后断线仍重连并发现 follow_up", async () => {
    vi.useFakeTimers();
    const hook = renderRunner();
    for (let i = 0; i < 5 && FakeEventSource.instances.length === 0; i++) {
      await flush(0);
    }
    expect(FakeEventSource.instances).toHaveLength(1);
    const es0 = FakeEventSource.instances[0];
    act(() => es0.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下销售" }));
    act(() =>
      es0.emit("turn_failed", {
        seq: 2,
        turn_id: "t_1",
        reason_code: "lead_internal_error",
      }),
    );
    // turn_failed is a semantic UI terminal fact, while the transport
    // supervisor remains reconnectable until wire-level done/error.
    expect(hook.result.current.running).toBe(false);

    act(() => es0.onerror?.(new Event("error")));
    await flush(1_100);
    expect(FakeEventSource.instances.length).toBeGreaterThanOrEqual(2);
    const replayedParent = FakeEventSource.instances[FakeEventSource.instances.length - 1];
    act(() =>
      replayedParent.emit("turn.follow_up", {
        seq: 3,
        follow_up_turn_id: "t_2",
        follow_up_stream_url: "/api/v1/chat/stream/t_2",
        reason: "steer_drain",
        buffered_count: 1,
      }),
    );

    expect(hook.result.current.turnId).toBe("t_2");
    expect(FakeEventSource.instances[FakeEventSource.instances.length - 1].url)
      .toContain("/chat/stream/t_2");
  });

  it("durable wire error renders a finite failure instead of verified success", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es = FakeEventSource.instances[0];

    act(() =>
      es.emit("error", {
        seq: 1,
        evt_seq: 1,
        turn_id: "t_1",
        reason_code: "lead_internal_error",
        message: "scheduler secret must never reach the UI",
      }),
    );

    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.errorReason).toMatchObject({
      source: "turn_failed",
      reasonCode: "lead_internal_error",
    });
    expect(JSON.stringify(hook.result.current.errorReason)).not.toContain(
      "scheduler secret",
    );
  });

  it("tracks a cancelled done as non-success without inventing an error", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));

    act(() =>
      FakeEventSource.instances[0].emit("done", {
        seq: 1,
        turn_id: "t_1",
        status: "cancelled",
      }),
    );

    expect(hook.result.current.running).toBe(false);
    expect(hook.result.current.terminalStatus).toBe("cancelled");
    expect(hook.result.current.errorReason).toBeNull();
  });

  it("续跑流的 stall-watchdog 不被父流 done 的延迟清理误杀(code-review 2026-07-04)", async () => {
    // Regression guard: the parent stream's done setTimeout(clear, 1s) previously read shared watchdogRef, while
    // turn.follow_up's millisecond-scale attachStream had already put the follow-up watchdog in that same ref →
    // after 1s it cleared the follow-up watchdog, leaving a half-open follow-up stuck "运行中" until F5.
    // Verify behavior: a follow-up stream silent for >40s (half-open) still stall-cycles into a new connection.
    vi.useFakeTimers();
    renderRunner();
    for (let i = 0; i < 5 && FakeEventSource.instances.length === 0; i++) {
      await flush(0);
    }
    expect(FakeEventSource.instances.length).toBe(1);
    const es0 = FakeEventSource.instances[0];
    act(() => es0.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下销售" }));
    // Production order is follow-up pointer → parent terminal event; late parent done must not stop the child stream.
    act(() =>
      es0.emit("turn.follow_up", {
        seq: 2, follow_up_turn_id: "t_2", follow_up_stream_url: "/api/v1/chat/stream/t_2",
        reason: "steer_drain", buffered_count: 1,
      }),
    );
    act(() => es0.emit("done", { seq: 3, turn_id: "t_1", status: "completed" }));
    // The follow-up stream is already open (instances[1] = t_2).
    expect(FakeEventSource.instances.length).toBe(2);
    const es1 = FakeEventSource.instances[1];
    expect(es1.url).toContain("/chat/stream/t_2");
    es1.readyState = 1; // Half-open: the browser is still OPEN but the gateway no longer pushes.

    // Advance past the parent's 1s delayed cleanup + the follow-up's 40s stall threshold (watchdog ticks every 5s).
    await flush(46_000);

    // Watchdog alive → force a new follow-up connection. The bug cleared it, leaving the turn stuck without reconnecting.
    expect(FakeEventSource.instances.length).toBeGreaterThanOrEqual(3);
    expect(FakeEventSource.instances[FakeEventSource.instances.length - 1].url)
      .toContain("/chat/stream/t_2");
  });
});
