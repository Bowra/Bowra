import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  chatWidthBounds,
  DEFAULT_SPLIT_RATIO,
  keyboardSplitRatio,
  MIN_CANVAS_PX,
  MIN_CHAT_PX,
  readSplitRatio,
  resolveSplit,
  SPLIT_KEY_STEP_PX,
  SPLIT_RATIO_KEY,
  writeSplitRatio,
} from "./split-pane";

// jsdom in this setup exposes `window.localStorage` without functional methods, so
// install a minimal in-memory Storage for the persistence tests (read/writeSplitRatio
// swallow a broken Storage via try/catch, which would silently hide round-trips).
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

// 1440 desktop with rail(56)+ctx(260): split area left=316, width=1124.
const LEFT = 316;
const WIDTH = 1124;
const x = (chatPx: number) => LEFT + chatPx; // pointer clientX for a given chat width

describe("resolveSplit", () => {
  it("returns a clamped fraction in the comfortable middle", () => {
    const r = resolveSplit(x(500), LEFT, WIDTH);
    expect(r.kind).toBe("ratio");
    if (r.kind === "ratio") expect(r.ratio).toBeCloseTo(500 / 1124, 5);
  });

  it("snaps to canvas-full when chat is dragged well below its min", () => {
    // chatPx 300 < 360-56=304 → snap
    expect(resolveSplit(x(300), LEFT, WIDTH)).toEqual({ kind: "snap", to: "canvas" });
  });

  it("snaps to chat-only when canvas is dragged well below its min", () => {
    // canvasPx = 1124-800 = 324 < 420-56=364 → snap
    expect(resolveSplit(x(800), LEFT, WIDTH)).toEqual({ kind: "snap", to: "chat" });
  });

  it("clamps UP to minChat instead of snapping inside the slack band", () => {
    // chatPx 340: not < 304, canvasPx 784 not < 364 → ratio, clamped up to 360
    const r = resolveSplit(x(340), LEFT, WIDTH);
    expect(r.kind).toBe("ratio");
    if (r.kind === "ratio") expect(r.ratio).toBeCloseTo(MIN_CHAT_PX / WIDTH, 5);
  });

  it("clamps DOWN so canvas keeps its min", () => {
    // chatPx 730: canvasPx 394 not < 364 → ratio, clamped to width-minCanvas=704
    const r = resolveSplit(x(730), LEFT, WIDTH);
    expect(r.kind).toBe("ratio");
    if (r.kind === "ratio") expect(r.ratio).toBeCloseTo((WIDTH - MIN_CANVAS_PX) / WIDTH, 5);
  });

  it("honours custom mins/slack", () => {
    const r = resolveSplit(x(200), LEFT, WIDTH, { minChat: 200, minCanvas: 200, snapSlack: 0 });
    expect(r.kind).toBe("ratio");
    if (r.kind === "ratio") expect(r.ratio).toBeCloseTo(200 / 1124, 5);
  });
});

describe("readSplitRatio / writeSplitRatio", () => {
  beforeEach(() => installMemStorage());
  afterEach(() => window.localStorage.clear());

  it("defaults when unset", () => {
    expect(readSplitRatio()).toBe(DEFAULT_SPLIT_RATIO);
  });

  it("round-trips a valid fraction", () => {
    writeSplitRatio(0.55);
    expect(window.localStorage.getItem(SPLIT_RATIO_KEY)).toBe("0.55");
    expect(readSplitRatio()).toBeCloseTo(0.55, 5);
  });

  it("falls back to default for invalid / out-of-range stored values", () => {
    window.localStorage.setItem(SPLIT_RATIO_KEY, "not-a-number");
    expect(readSplitRatio()).toBe(DEFAULT_SPLIT_RATIO);
    window.localStorage.setItem(SPLIT_RATIO_KEY, "1.5");
    expect(readSplitRatio()).toBe(DEFAULT_SPLIT_RATIO);
    window.localStorage.setItem(SPLIT_RATIO_KEY, "0");
    expect(readSplitRatio()).toBe(DEFAULT_SPLIT_RATIO);
  });
});

describe("keyboardSplitRatio (keyboard resize of the divider)", () => {
  const AREA = 1124;

  it("←/→ 每次 16px", () => {
    expect(keyboardSplitRatio("ArrowRight", 450, AREA)! * AREA).toBeCloseTo(466);
    expect(keyboardSplitRatio("ArrowLeft", 450, AREA)! * AREA).toBeCloseTo(434);
    expect(SPLIT_KEY_STEP_PX).toBe(16);
  });

  it("Home / End 到对话栏最小宽与最大宽（成果栏留 420）", () => {
    expect(keyboardSplitRatio("Home", 600, AREA)! * AREA).toBeCloseTo(MIN_CHAT_PX);
    expect(keyboardSplitRatio("End", 450, AREA)! * AREA).toBeCloseTo(AREA - MIN_CANVAS_PX);
  });

  it("夹在范围内、不越界切成单栏", () => {
    expect(keyboardSplitRatio("ArrowLeft", MIN_CHAT_PX, AREA)! * AREA).toBeCloseTo(MIN_CHAT_PX);
    expect(keyboardSplitRatio("ArrowRight", AREA - MIN_CANVAS_PX, AREA)! * AREA).toBeCloseTo(AREA - MIN_CANVAS_PX);
  });

  it("其他键、区域宽度未知时不处理", () => {
    expect(keyboardSplitRatio("Enter", 450, AREA)).toBeNull();
    expect(keyboardSplitRatio("ArrowRight", 450, 0)).toBeNull();
  });

  it("区域太窄时上下限收成一点", () => {
    expect(chatWidthBounds(600)).toEqual({ min: MIN_CHAT_PX, max: MIN_CHAT_PX });
  });
});
