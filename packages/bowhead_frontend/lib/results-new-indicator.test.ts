import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  readSeenRecord,
  RESULTS_SEEN_STORAGE_KEY,
  stepResultsSeen,
  useResultsNewIndicator,
  writeSeenRecord,
} from "./results-new-indicator";

function installMemStorage() {
  let store: Record<string, string> = {};
  const mem: Storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { store = {}; },
    key: (i) => Object.keys(store)[i] ?? null,
    get length() { return Object.keys(store).length; },
  };
  Object.defineProperty(window, "localStorage", { value: mem, configurable: true });
}

beforeEach(installMemStorage);
afterEach(() => window.localStorage.clear());

describe("stepResultsSeen（纯函数）", () => {
  it("看得见时全部记为看过、没有圆点", () => {
    const step = stepResultsSeen(new Set(["a"]), ["a", "b"], true, true);
    expect([...step.seen]).toEqual(["a", "b"]);
    expect(step.hasNew).toBe(false);
    expect(step.persist).toBe(true);
  });

  it("看不见且算新内容时：有没看过的 id 就有圆点，不改看过集合", () => {
    const step = stepResultsSeen(new Set(["a"]), ["a", "b"], false, true);
    expect(step.hasNew).toBe(true);
    expect([...step.seen]).toEqual(["a"]);
    expect(stepResultsSeen(new Set(["a"]), ["a"], false, true).hasNew).toBe(false);
  });

  it("老会话首次打开（不算新内容）：加载到的成果直接当作看过", () => {
    const step = stepResultsSeen(new Set(), ["a", "b"], false, false);
    expect(step.hasNew).toBe(false);
    expect([...step.seen]).toEqual(["a", "b"]);
  });
});

describe("useResultsNewIndicator", () => {
  type P = Parameters<typeof useResultsNewIndicator>[0];
  const props = (over: Partial<P>): P => ({ enabled: true, sessionId: "s_1", ids: [], visible: false, running: false, ...over });

  it("live：运行中出现的成果算新内容；看过后清除；F5（重新挂载）后不再出现", () => {
    const { result, rerender, unmount } = renderHook((p: P) => useResultsNewIndicator(p), { initialProps: props({}) });
    rerender(props({ running: true }));
    rerender(props({ running: true, ids: ["section:a"] }));
    expect(result.current).toBe(true);
    rerender(props({ running: false, ids: ["section:a"] }));
    expect(result.current).toBe(true);
    rerender(props({ ids: ["section:a"], visible: true }));
    expect(result.current).toBe(false);
    rerender(props({ ids: ["section:a"], visible: false }));
    expect(result.current).toBe(false);
    unmount();
    const again = renderHook((p: P) => useResultsNewIndicator(p), { initialProps: props({ ids: ["section:a"] }) });
    expect(again.result.current).toBe(false);
  });

  it("F5：刷新前还没看过的新成果，刷新后圆点仍在", () => {
    const first = renderHook((p: P) => useResultsNewIndicator(p), { initialProps: props({ running: true }) });
    first.rerender(props({ running: true, ids: ["section:a"] }));
    expect(first.result.current).toBe(true);
    first.unmount();
    expect(readSeenRecord("s_1")).not.toBeNull();
    const again = renderHook((p: P) => useResultsNewIndicator(p), { initialProps: props({}) });
    again.rerender(props({ ids: ["section:a"] }));
    expect(again.result.current).toBe(true);
  });

  it("老会话首次打开（本机没有记录、没见过运行中）：分几次加载到的成果都不算新内容", () => {
    const { result, rerender } = renderHook((p: P) => useResultsNewIndicator(p), { initialProps: props({}) });
    rerender(props({ ids: ["section:a"] }));
    rerender(props({ ids: ["section:a", "result:r1"] }));
    expect(result.current).toBe(false);
  });

  it("when disabled: always false and never reads or writes the local record", () => {
    writeSeenRecord("s_1", new Set());
    const before = window.localStorage.getItem(RESULTS_SEEN_STORAGE_KEY);
    const { result, rerender } = renderHook((p: P) => useResultsNewIndicator(p), { initialProps: props({ enabled: false, running: true }) });
    rerender(props({ enabled: false, running: true, ids: ["section:a"] }));
    expect(result.current).toBe(false);
    expect(window.localStorage.getItem(RESULTS_SEEN_STORAGE_KEY)).toBe(before);
  });

  it("桌面：成果栏看过之后关掉，再出现的成果算新内容（同一次挂载内，不依赖本机记录）", () => {
    const { result, rerender } = renderHook((p: P) => useResultsNewIndicator(p), { initialProps: props({ visible: true, ids: ["section:a"] }) });
    rerender(props({ visible: false, ids: ["section:a"] }));
    expect(result.current).toBe(false);
    rerender(props({ visible: false, ids: ["section:a", "section:b"] }));
    expect(result.current).toBe(true);
  });

  it("没有会话时恒为 false", () => {
    const { result, rerender } = renderHook((p: P) => useResultsNewIndicator(p), { initialProps: props({ sessionId: null, running: true }) });
    rerender(props({ sessionId: null, running: true, ids: ["section:a"] }));
    expect(result.current).toBe(false);
  });

  it("按会话区分：换会话后按新会话自己的记录判断", () => {
    const { result, rerender } = renderHook((p: P) => useResultsNewIndicator(p), { initialProps: props({ running: true }) });
    rerender(props({ running: true, ids: ["section:a"] }));
    expect(result.current).toBe(true);
    rerender(props({ sessionId: "s_2", ids: ["section:b"] }));
    expect(result.current).toBe(false);
  });
});
