import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { DEFAULT_SPLIT_RATIO, MIN_CANVAS_PX, MIN_CHAT_PX, SPLIT_RATIO_KEY } from "@/lib/split-pane";
import { SplitPane } from "./SplitPane";

afterEach(() => cleanup());

const noop = vi.fn();

describe("SplitPane collapsed Results edge", () => {
  it("names a data-only right pane as Results instead of Report", () => {
    renderIntl(
      <SplitPane
        mode="chat"
        chat={<div>chat</div>}
        canvas={<div>results</div>}
        canvasLabel="成果"
        onSnapCanvas={noop}
        onSnapChat={noop}
        onReopen={noop}
      />,
    );

    expect(screen.getByRole("button", { name: "重开成果" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重开报告" })).not.toBeInTheDocument();
  });
});

/**
 * Batch 4a (workbench behavior): divider keyboard controls, double-click reset, and the restore bar's New Content dot.
 * jsdom has no layout: give the split area width 1124 and an immediately-calling ResizeObserver so SplitPane obtains its area width.
 */
describe("SplitPane keyboard and double-click reset", () => {
  const AREA = 1124;
  let restore: (() => void) | null = null;

  beforeEach(() => {
    const store: Record<string, string> = {};
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => { store[k] = String(v); },
        removeItem: (k: string) => { delete store[k]; },
        clear: () => undefined,
        key: () => null,
        length: 0,
      },
    });
    const widthDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get(this: HTMLElement) { return this.classList.contains("v2-split-area") ? AREA : 0; },
    });
    const RO = globalThis.ResizeObserver;
    globalThis.ResizeObserver = class {
      constructor(private cb: ResizeObserverCallback) {}
      observe() { this.cb([], this as unknown as ResizeObserver); }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    restore = () => {
      if (widthDesc) Object.defineProperty(HTMLElement.prototype, "clientWidth", widthDesc);
      globalThis.ResizeObserver = RO;
    };
  });
  afterEach(() => {
    restore?.();
  });

  const renderSplit = (mode: "split" | "chat" = "split", canvasHasNew = false) => renderIntl(
    <SplitPane
      mode={mode}
      chat={<div>chat</div>}
      canvas={<div>results</div>}
      canvasLabel="成果"
      canvasHasNew={canvasHasNew}
      onSnapCanvas={noop}
      onSnapChat={noop}
      onReopen={noop}
    />,
  );
  const chatWidth = () => (document.querySelector(".v2-split-chat") as HTMLElement).style.width;

  it("the divider is focusable with aria-valuenow; ←/→ move 16px, Home / End go to the ends, double-click resets", () => {
    renderSplit();
    const sep = screen.getByRole("separator", { name: "调整对话 / 成果宽度" });
    expect(sep).toHaveAttribute("tabindex", "0");
    expect(sep).toHaveAttribute("aria-valuenow", String(Math.round(AREA * DEFAULT_SPLIT_RATIO / AREA * 100)));
    expect(sep).toHaveAttribute("aria-valuemin", String(Math.round((MIN_CHAT_PX / AREA) * 100)));
    expect(sep).toHaveAttribute("aria-valuemax", String(Math.round(((AREA - MIN_CANVAS_PX) / AREA) * 100)));
    expect(sep.getAttribute("aria-valuetext")).toBe("对话栏占 40%");
    const start = AREA * DEFAULT_SPLIT_RATIO;
    fireEvent.keyDown(sep, { key: "ArrowRight" });
    expect(parseFloat(chatWidth())).toBeCloseTo(start + 16);
    fireEvent.keyDown(sep, { key: "ArrowLeft" });
    fireEvent.keyDown(sep, { key: "ArrowLeft" });
    expect(parseFloat(chatWidth())).toBeCloseTo(start - 16);
    fireEvent.keyDown(sep, { key: "Home" });
    expect(parseFloat(chatWidth())).toBeCloseTo(MIN_CHAT_PX);
    fireEvent.keyDown(sep, { key: "End" });
    expect(parseFloat(chatWidth())).toBeCloseTo(AREA - MIN_CANVAS_PX);
    expect(window.localStorage.getItem(SPLIT_RATIO_KEY)).toBe(String((AREA - MIN_CANVAS_PX) / AREA));
    // Ignore modified keys (leave them to the system and other shortcuts)
    fireEvent.keyDown(sep, { key: "Home", shiftKey: true });
    expect(parseFloat(chatWidth())).toBeCloseTo(AREA - MIN_CANVAS_PX);
    fireEvent.doubleClick(sep);
    expect(parseFloat(chatWidth())).toBeCloseTo(start);
    expect(window.localStorage.getItem(SPLIT_RATIO_KEY)).toBe(String(DEFAULT_SPLIT_RATIO));
  });

  it("clamps aria-valuenow to valuemax when the saved ratio exceeds the current maximum", () => {
    window.localStorage.setItem(SPLIT_RATIO_KEY, "0.9");
    renderSplit();
    const sep = screen.getByRole("separator");
    expect(Number(sep.getAttribute("aria-valuenow"))).toBe(Number(sep.getAttribute("aria-valuemax")));
    expect(sep.getAttribute("aria-valuetext")).toBe(`对话栏占 ${sep.getAttribute("aria-valuemax")}%`);
  });

  it("DOM order puts the divider between the panes (Tab: chat → divider → results)", () => {
    const order = () => Array.from(document.querySelector(".v2-split-area")!.children).map((el) =>
      el.getAttribute("role") === "separator" ? "sep" : el.className);
    renderSplit();
    expect(order()).toEqual(["v2-split-chat", "sep", "v2-split-canvas"]);
  });

  it("new content while Results is collapsed → the reopen edge shows a dot and announces it", () => {
    renderSplit("chat", true);
    const edge = screen.getByRole("button", { name: "重开成果，有新内容" });
    expect(edge).toHaveAttribute("data-has-new", "1");
    expect(edge.querySelector(".v2-ws-new-dot")).not.toBeNull();
  });
});
