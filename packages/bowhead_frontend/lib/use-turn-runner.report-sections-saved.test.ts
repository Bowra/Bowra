/**
 * Terminal done report_sections_saved (report sections persisted this turn): live reads SSE done; F5 reads the last done in `/turns/{id}/events`
 * (page.tsx `_fetchLastFromTurn`), both using `reportSectionsSavedFromDone` to get the same value;
 * missing fields or values that are not nonnegative integers return null (not 0); reset when a new turn starts.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { reportSectionsSavedFromDone } from "./report-sections-saved";
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
  globalThis.fetch = ORIGINAL_FETCH;
  (globalThis as Record<string, unknown>).EventSource = ORIGINAL_ES;
  vi.restoreAllMocks();
});

describe("reportSectionsSavedFromDone", () => {
  it.each([
    [{ report_sections_saved: 3 }, 3],
    [{ report_sections_saved: 0 }, 0],
    [{}, null],
    [{ report_sections_saved: null }, null],
    [{ report_sections_saved: -1 }, null],
    [{ report_sections_saved: 2.5 }, null],
    [{ report_sections_saved: "3" }, null],
    [null, null],
  ])("%j ⇒ %s", (payload, expected) => {
    expect(reportSectionsSavedFromDone(payload)).toBe(expected);
  });
});

async function liveDone(payload: Record<string, unknown>) {
  const hook = renderHook(() =>
    useTurnRunner({ mode: "sse", message: "写报告", workspaceId: "w", memberUid: "m", sendTick: 1 }),
  );
  await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
  await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
  const es = FakeEventSource.instances[0];
  act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "写报告" }));
  act(() => es.emit("done", { seq: 2, turn_id: "t_1", ...payload }));
  return hook;
}

function hydrated(lastReportSectionsSaved: number | null | undefined, lastTerminalStatus = "aborted", mode: "sse" | "mock" = "sse") {
  // Keep the hydrate object stable across renders (recreating it each render makes the runner repeatedly hydrate).
  const args = {
    mode,
    hydrate: {
      sessionId: "s_1",
      sessionTitle: null,
      messages: [],
      lastIntent: null,
      lastTurnId: "t_1",
      lastTerminalStatus,
      lastTurnRouted: true,
      ...(lastReportSectionsSaved === undefined ? null : { lastReportSectionsSaved }),
    },
    hydrateTick: 1,
  };
  return renderHook(() => useTurnRunner(args));
}

describe("live 与 F5 得到同一个节数", () => {
  it.each([
    ["被停止、落库 3 节", { status: "aborted", report_sections_saved: 3 }, 3],
    ["修订轮（只改上一轮的节）", { status: "aborted", report_sections_saved: 0 }, 0],
    ["旧协议 / 旁路：没有这个字段", { status: "failed" }, null],
  ])("%s", async (_name, payload, expected) => {
    const live = await liveDone(payload);
    await waitFor(() => expect(live.result.current.running).toBe(false));
    expect(live.result.current.reportSectionsSaved).toBe(expected);
    // F5: page.tsx reads the same field from the last done, passes it through the same function to hydrate.
    const f5 = hydrated(reportSectionsSavedFromDone(payload), String(payload.status));
    await waitFor(() => expect(f5.result.current.turnId).toBe("t_1"));
    expect(f5.result.current.reportSectionsSaved).toBe(expected);
  });

  it.each(["sse", "mock"] as const)("水合（%s 模式的 runner）把节数原样交给本轮", async (mode) => {
    const f5 = hydrated(5, "aborted", mode);
    await waitFor(() => expect(f5.result.current.turnId).toBe("t_1"));
    expect(f5.result.current.reportSectionsSaved).toBe(5);
    const missing = hydrated(undefined, "aborted", mode);
    await waitFor(() => expect(missing.result.current.turnId).toBe("t_1"));
    expect(missing.result.current.reportSectionsSaved).toBeNull();
  });

  it("发下一轮时清掉上一轮的节数（不把上一轮的「已保存 N 节」带进新一轮）", async () => {
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
    const hook = renderHook(
      ({ tick }) => useTurnRunner({ mode: "sse", message: `第 ${tick} 轮`, workspaceId: "w", memberUid: "m", sendTick: tick }),
      { initialProps: { tick: 1 } },
    );
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_1"));
    const es = FakeEventSource.instances[0];
    act(() => es.emit("turn_started", { seq: 1, session_id: "s_1", user_text: "第 1 轮" }));
    act(() => es.emit("done", { seq: 2, turn_id: "t_1", status: "aborted", report_sections_saved: 4 }));
    await waitFor(() => expect(hook.result.current.reportSectionsSaved).toBe(4));
    hook.rerender({ tick: 2 });
    await waitFor(() => expect(hook.result.current.turnId).toBe("t_2"));
    expect(hook.result.current.reportSectionsSaved).toBeNull();
  });

  it("F5 读的是 page.tsx `_fetchLastFromTurn` 里 done 分支的同一个函数（不自己数画布）", () => {
    const page = readFileSync(join(__dirname, "../app/(app)/page.tsx"), "utf8");
    const doneBranch = page.slice(page.indexOf("async function _fetchLastFromTurn"), page.indexOf("case \"turn_failed\""));
    expect(doneBranch).toContain("reportSectionsSaved = reportSectionsSavedFromDone(p);");
    expect(page).toMatch(/lastReportSectionsSaved: projection\.reportSectionsSaved/);
  });
});
