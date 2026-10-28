/**
 * chat-ux-2026-08 batch C — viewport following.
 *
 * 116 prod v1.0.71: scrollbar placed at bottom (gap=0) then untouched; during a 270-second turn,
 * bottom gap 0 → 230 → 581 → 1418px **fell monotonically behind**, with no return entry in UI.
 *
 * Two old root causes:
 *   ① Effect depended only on `traceSteps.length`; **growth inside existing steps** never triggered it
 *   ② Hard exit at `dist > 200`; crossing once made return impossible
 *
 * ⚠ Testing discipline: detaching follow **requires real wheel / touchmove / keydown events**.
 * Directly changing `scrollTop` creates a vacuously green test: our own scrollTo
 * also changes scrollTop, so implementation deliberately excludes that path.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useStickToBottom } from "./use-stick-to-bottom";

/** Controlled fake scroll container: writable scrollHeight/clientHeight, recorded scrollTo calls. */
function makeScrollEl(opts: { clientHeight?: number } = {}) {
  const el = document.createElement("div");
  let scrollHeight = 1000;
  let scrollTop = 0;
  Object.defineProperty(el, "scrollHeight", { get: () => scrollHeight });
  Object.defineProperty(el, "clientHeight", { get: () => opts.clientHeight ?? 400 });
  Object.defineProperty(el, "scrollTop", {
    get: () => scrollTop,
    set: (v: number) => { scrollTop = v; },
  });
  (el as unknown as { scrollTo: (o: ScrollToOptions) => void }).scrollTo = (o) => {
    scrollTop = o.top ?? scrollTop;
    el.dispatchEvent(new Event("scroll"));
  };
  document.body.appendChild(el);
  return {
    el,
    grow(by: number) { scrollHeight += by; },
    get scrollTop() { return scrollTop; },
    setScrollTop(v: number) { scrollTop = v; },
    atBottom() { return scrollHeight - scrollTop - (opts.clientHeight ?? 400) <= 24; },
  };
}

/** Record latest ResizeObserver callback for manual test triggering (jsdom does no real layout measurement). */
let roCallbacks: Array<() => void> = [];
class FakeResizeObserver {
  constructor(cb: () => void) { roCallbacks.push(cb); }
  observe() {}
  disconnect() {}
}

beforeEach(() => {
  roCallbacks = [];
  (globalThis as Record<string, unknown>).ResizeObserver = FakeResizeObserver;
});
afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

function mount(enabled = true) {
  const scroll = makeScrollEl();
  const content = document.createElement("div");
  scroll.el.appendChild(content);
  const hook = renderHook(() => useStickToBottom(enabled));
  act(() => {
    hook.result.current.scrollRef(scroll.el);
    hook.result.current.contentRef(content);
  });
  return { hook, scroll, content };
}

function growAndSettle(scroll: ReturnType<typeof makeScrollEl>, by: number) {
  act(() => {
    scroll.grow(by);
    roCallbacks.forEach((cb) => cb());
  });
}

describe("useStickToBottom — 内容长高时跟随", () => {
  it("★ 步骤内部长高（无新步骤）也跟随 —— 这是旧实现漏掉的那条路", () => {
    const { scroll } = mount();
    scroll.setScrollTop(600); // At bottom (1000-600-400=0).
    growAndSettle(scroll, 400);
    expect(scroll.atBottom()).toBe(true);
  });

  it("连续多次长高不掉队（旧实现是 0→230→581→1418 单调增）", () => {
    const { scroll } = mount();
    scroll.setScrollTop(600);
    for (const by of [230, 351, 837]) growAndSettle(scroll, by);
    expect(scroll.atBottom()).toBe(true);
  });
});

describe("useStickToBottom — 只有真实用户手势才解除跟随", () => {
  it("向上滚轮 → 解除跟随并出浮标", () => {
    const { hook, scroll } = mount();
    scroll.setScrollTop(600);
    expect(hook.result.current.following).toBe(true);

    act(() => {
      scroll.setScrollTop(100); // User scrolled up.
      scroll.el.dispatchEvent(new WheelEvent("wheel", { deltaY: -120 }));
    });
    expect(hook.result.current.following).toBe(false);
  });

  it("向下滚轮不解除（用户在追进度，不是往回看）", () => {
    const { hook, scroll } = mount();
    scroll.setScrollTop(100);
    act(() => {
      scroll.el.dispatchEvent(new WheelEvent("wheel", { deltaY: 120 }));
    });
    expect(hook.result.current.following).toBe(true);
  });

  it("PageUp 解除跟随", () => {
    const { hook, scroll } = mount();
    act(() => {
      scroll.setScrollTop(100);
      scroll.el.dispatchEvent(new KeyboardEvent("keydown", { key: "PageUp" }));
    });
    expect(hook.result.current.following).toBe(false);
  });

  it("★ 程序滚动**不**解除跟随 —— 我们自己的 scrollTo 也发 scroll 事件", () => {
    const { hook, scroll } = mount();
    scroll.setScrollTop(600);
    growAndSettle(scroll, 500); // Internal scrollTo triggers scroll event.
    expect(hook.result.current.following).toBe(true);
  });

  it("★ 平滑滚动**动画中途**的 scroll 事件不解除 —— 否则自己把自己关掉", () => {
    // Real scrollTo({behavior:'smooth'}) continuously dispatches scroll during animation,
    // while **not yet at bottom**. Using scroll as detachment criterion would let our own bottom-follow
    // disable following halfway, why this implementation deliberately excludes scroll as detachment signal.
    const { hook, scroll } = mount();
    scroll.setScrollTop(600);
    expect(hook.result.current.following).toBe(true);

    act(() => {
      scroll.grow(1000);            // Content grew; target bottom far below.
      scroll.setScrollTop(900);     // Animation halfway, not at bottom yet.
      scroll.el.dispatchEvent(new Event("scroll"));
    });
    expect(hook.result.current.following).toBe(true);
  });

  it("已经在底部时的向上滚轮不解除（惯性/回弹不该误伤）", () => {
    const { hook, scroll } = mount();
    scroll.setScrollTop(600); // At bottom.
    act(() => {
      scroll.el.dispatchEvent(new WheelEvent("wheel", { deltaY: -10 }));
    });
    expect(hook.result.current.following).toBe(true);
  });
});

describe("useStickToBottom — 回到最新", () => {
  it("解除后长高不再贴底，点回到最新恢复", () => {
    const { hook, scroll } = mount();
    act(() => {
      scroll.setScrollTop(100);
      scroll.el.dispatchEvent(new WheelEvent("wheel", { deltaY: -120 }));
    });
    growAndSettle(scroll, 800);
    expect(scroll.atBottom()).toBe(false); // Not following.

    act(() => hook.result.current.jumpToLatest());
    expect(hook.result.current.following).toBe(true);
    expect(scroll.atBottom()).toBe(true);
  });

  it("手动滚回底部也恢复跟随", () => {
    const { hook, scroll } = mount();
    act(() => {
      scroll.setScrollTop(100);
      scroll.el.dispatchEvent(new WheelEvent("wheel", { deltaY: -120 }));
    });
    expect(hook.result.current.following).toBe(false);

    act(() => {
      scroll.setScrollTop(600);
      scroll.el.dispatchEvent(new Event("scroll"));
    });
    expect(hook.result.current.following).toBe(true);
  });

  it("新进展计数只在解除跟随时累加，恢复后清零", () => {
    const { hook, scroll } = mount();
    act(() => hook.result.current.notePendingProgress());
    expect(hook.result.current.pendingCount).toBe(0); // Do not count while following.

    act(() => {
      scroll.setScrollTop(100);
      scroll.el.dispatchEvent(new WheelEvent("wheel", { deltaY: -120 }));
    });
    act(() => {
      hook.result.current.notePendingProgress();
      hook.result.current.notePendingProgress();
    });
    expect(hook.result.current.pendingCount).toBe(2);

    act(() => hook.result.current.jumpToLatest());
    expect(hook.result.current.pendingCount).toBe(0);
  });
});
