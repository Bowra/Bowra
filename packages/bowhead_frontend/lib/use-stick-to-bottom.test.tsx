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
 * ⚠ Testing discipline: detaching follow **requires real wheel / touchmove / keydown events**, or a
 * `scroll` event that moved **up** away from the bottom. Directly changing `scrollTop` without an event
 * proves nothing: our own scrollTo also changes scrollTop.
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
  const hook = renderHook(({ running }) => useStickToBottom(running), {
    initialProps: { running: enabled },
  });
  act(() => {
    hook.result.current.scrollRef(scroll.el);
    hook.result.current.contentRef(content);
  });
  return { hook, scroll, content };
}

/**
 * A disclosure row (`aria-expanded` button) at `rowY()` within the content. jsdom has no layout, so its
 * on-screen top is derived from the fake scroll position (the scroll container's own top is 0).
 */
function addDisclosureRow(
  scroll: ReturnType<typeof makeScrollEl>,
  content: HTMLElement,
  rowY: () => number,
) {
  const row = document.createElement("button");
  row.setAttribute("aria-expanded", "false");
  row.getBoundingClientRect = () => ({ top: rowY() - scroll.scrollTop } as DOMRect);
  content.appendChild(row);
  return {
    row,
    screenTop: () => rowY() - scroll.scrollTop,
    /** Click as a reader would; the component flips its state in the same event. */
    toggle() {
      act(() => {
        row.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        row.setAttribute("aria-expanded", row.getAttribute("aria-expanded") === "true" ? "false" : "true");
      });
    },
  };
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

describe("useStickToBottom — after the Turn ends (2026-11-08 dogfood F8)", () => {
  it("growth after the Turn ends does not pull a reader who has engaged back to the bottom", () => {
    // Mutation: drop the `!enabledRef.current && readerEngagedRef.current` guard → this case fails.
    const { hook, scroll } = mount(true);
    scroll.setScrollTop(600);
    hook.rerender({ running: false });
    act(() => {
      scroll.el.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    });
    growAndSettle(scroll, 400);
    expect(scroll.scrollTop).toBe(600);
  });

  it("while the Turn runs, growth is still followed after the reader clicks inside the conversation", () => {
    const { scroll } = mount(true);
    scroll.setScrollTop(600);
    act(() => {
      scroll.el.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    });
    growAndSettle(scroll, 400);
    expect(scroll.atBottom()).toBe(true);
  });

  it("a conversation opened after its Turn ended still settles on its latest content", () => {
    // Positive control for the guard above: gating on the running state alone would open finished
    // conversations at the top, because this observer is what brings them to their latest content.
    const { scroll } = mount(false);
    growAndSettle(scroll, 1200);
    growAndSettle(scroll, 300); // A chart below lays out late.
    expect(scroll.atBottom()).toBe(true);
  });

  it("the closing render of a Turn stays in view for a reader who is still following", () => {
    const { hook, scroll } = mount(true);
    scroll.setScrollTop(600);
    act(() => {
      scroll.el.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    });
    hook.rerender({ running: false });
    growAndSettle(scroll, 120);
    expect(scroll.atBottom()).toBe(true);
  });

  it("★ opening a trace after the Turn ended keeps the clicked row where it was (116: 120 px → −283 px)", () => {
    // Mutation: make the observer ignore the disclosure hold and the engaged reader → the row leaves the screen.
    const { hook, scroll, content } = mount(true);
    scroll.setScrollTop(600); // At the bottom, following.
    hook.rerender({ running: false });
    const trace = addDisclosureRow(scroll, content, () => 720);
    expect(trace.screenTop()).toBe(120);

    trace.toggle();
    growAndSettle(scroll, 400); // The trace opens below its row.
    expect(trace.screenTop()).toBe(120);
    expect(hook.result.current.following).toBe(false); // "Jump to latest" offers the way back.
  });

  it("a disclosure toggled while the Turn runs keeps its row instead of following the growth", () => {
    const { hook, scroll, content } = mount(true);
    scroll.setScrollTop(600);
    const step = addDisclosureRow(scroll, content, () => 700);
    step.toggle();
    growAndSettle(scroll, 300);
    expect(step.screenTop()).toBe(100);
    expect(hook.result.current.following).toBe(false);
    growAndSettle(scroll, 200); // Streaming output continues below.
    expect(step.screenTop()).toBe(100);
  });

  it("a toggled row is held in place when content above it changes in the same resize", () => {
    const { scroll, content } = mount(false);
    scroll.setScrollTop(600);
    let rowY = 700;
    const row = addDisclosureRow(scroll, content, () => rowY);
    row.toggle();
    act(() => {
      rowY += 300; // Content above the row grew …
      scroll.grow(500); // … and the opened section below it.
      roCallbacks.forEach((cb) => cb());
    });
    expect(row.screenTop()).toBe(100);
    expect(scroll.scrollTop).toBe(900);
  });

  it("a click that toggles nothing does not hold the view: streaming growth is still followed", () => {
    const { scroll, content } = mount(true);
    scroll.setScrollTop(600);
    const menu = addDisclosureRow(scroll, content, () => 700);
    act(() => {
      menu.row.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    growAndSettle(scroll, 300);
    expect(scroll.atBottom()).toBe(true);
  });

  it("an upward scroll by other means (scrollbar drag, focus move) releases following", () => {
    // Mutation: restore `onScroll` to only re-attach → following stays true and the next growth yanks the reader.
    const { hook, scroll } = mount(true);
    scroll.setScrollTop(600);
    act(() => {
      scroll.el.dispatchEvent(new Event("scroll"));
    });
    expect(hook.result.current.following).toBe(true);
    act(() => {
      scroll.setScrollTop(200);
      scroll.el.dispatchEvent(new Event("scroll"));
    });
    expect(hook.result.current.following).toBe(false);
    growAndSettle(scroll, 400);
    expect(scroll.scrollTop).toBe(200);
  });
});
