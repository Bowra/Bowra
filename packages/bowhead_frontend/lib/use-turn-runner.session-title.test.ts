/**
 * chat-ux-2026-08 batch E — session title live == replay (invariant repair).
 *
 * Defect (116 prod v1.0.71 observations + both sides' source comparison):
 *   backend `session_store.py::touch_session` uses `title = row["title"] or title_hint`
 *     → **first write fixes it, never overwritten** (title = first-turn question)
 *   frontend `use-turn-runner.ts` **unconditionally overwrote** sessionTitle on **every `turn_started`**
 *     → live showed the **last turn**, F5 showed the **first turn**
 *
 * One session thus had two names, top bar and sidebar disagreed, violating CLAUDE.md's chat invariant
 * that live, persisted and replayed data must match. This is a contract issue, beyond copy.
 *
 * Mutation check: remove `if (!ns.sessionTitle)` (restore unconditional assignment); the first case must fail.
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
  vi.restoreAllMocks();
  _resetAllTurnsForTesting();
});

const FIRST = "看一下最近30天各品牌的营业额和退款率，找出表现最差的3个品牌";
const SECOND = "刚才说的5.9倍是怎么算出来的？一句话解释";

function renderRunner() {
  return renderHook(() =>
    useTurnRunner({ mode: "sse", message: FIRST, workspaceId: "w", memberUid: "m", sendTick: 1 }),
  );
}

describe("use-turn-runner · 会话标题 = 首轮，后续轮不覆盖", () => {
  it("第二轮 turn_started 不改写标题（与后端 first-write-wins 同口径）", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const es = FakeEventSource.instances[0];

    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: FIRST }));
    await waitFor(() => expect(hook.result.current.sessionTitle).toBeTruthy());
    const afterFirst = hook.result.current.sessionTitle;
    expect(afterFirst).toBe(FIRST); // 30 characters, below the 40-character cap

    // Second turn in the same session—the existing behavior rewrites the title to this sentence; after fixing, it must remain unchanged.
    act(() => es.emit("turn_started", { seq: 9, session_id: "s_1", user_text: SECOND }));
    expect(hook.result.current.sessionTitle).toBe(afterFirst);
    expect(hook.result.current.sessionTitle).not.toContain("5.9");
  });

  it("首轮仍照常写入（守卫不能把正常路一起堵死）", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const es = FakeEventSource.instances[0];

    expect(hook.result.current.sessionTitle).toBeNull();
    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: FIRST }));
    await waitFor(() => expect(hook.result.current.sessionTitle).toBe(FIRST));
  });

  it("超长提问截到 40 字加省略号（与后端 user_text[:40] 同口径）", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const es = FakeEventSource.instances[0];

    const LONG = "把".repeat(120);
    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: LONG }));
    await waitFor(() =>
      expect(hook.result.current.sessionTitle).toBe("把".repeat(40) + "…"),
    );
  });

  it("短提问不加省略号（40 字 cap 与后端同口径）", async () => {
    const hook = renderRunner();
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const es = FakeEventSource.instances[0];

    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "查一下销售" }));
    await waitFor(() => expect(hook.result.current.sessionTitle).toBe("查一下销售"));
  });
});
