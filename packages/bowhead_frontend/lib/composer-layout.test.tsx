import { useRef } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  classifyComposerWidth,
  useComposerLayoutMode,
} from "./composer-layout";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("classifyComposerWidth", () => {
  it.each([
    [320, "ultra"],
    [359.99, "ultra"],
    [360, "compact"],
    [559.99, "compact"],
    [560, "comfortable"],
    [840, "comfortable"],
    [960, "comfortable"],
  ] as const)("classifies %s as %s", (width, expected) => {
    expect(classifyComposerWidth(width)).toBe(expected);
  });

  it("falls back when ResizeObserver emits once and then becomes silent", () => {
    vi.useFakeTimers();
    let resizeCallback!: ResizeObserverCallback;
    class HealthyResizeObserver implements ResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        resizeCallback = callback;
      }

      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", HealthyResizeObserver);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(performance.now()), 0),
    );
    vi.stubGlobal("cancelAnimationFrame", (id: number) =>
      window.clearTimeout(id),
    );
    let width = 400;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      () => ({
        width,
        height: 100,
        top: 0,
        right: width,
        bottom: 100,
        left: 0,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      }),
    );

    function Probe() {
      const ref = useRef<HTMLDivElement | null>(null);
      const mode = useComposerLayoutMode(ref);
      return (
        <div ref={ref} data-testid="probe">
          {mode}
        </div>
      );
    }

    const { getByTestId } = render(<Probe />);
    act(() => {
      resizeCallback(
        [
          {
            target: document.querySelector("[data-testid='probe']") as Element,
            borderBoxSize: [{ inlineSize: 400, blockSize: 100 }],
          } as unknown as ResizeObserverEntry,
        ],
        {} as ResizeObserver,
      );
    });
    expect(getByTestId("probe").textContent).toBe("compact");
    width = 600;
    act(() => vi.advanceTimersByTime(800));
    expect(getByTestId("probe").textContent).toBe("comfortable");
  });

  it("falls back to polling when observe succeeds but never emits a callback", () => {
    vi.useFakeTimers();
    class SilentResizeObserver implements ResizeObserver {
      constructor(_callback: ResizeObserverCallback) {}
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", SilentResizeObserver);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(performance.now()), 0),
    );
    vi.stubGlobal("cancelAnimationFrame", (id: number) =>
      window.clearTimeout(id),
    );
    let width = 400;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      () => ({
        width,
        height: 100,
        top: 0,
        right: width,
        bottom: 100,
        left: 0,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      }),
    );

    function Probe() {
      const ref = useRef<HTMLDivElement | null>(null);
      const mode = useComposerLayoutMode(ref);
      return (
        <div ref={ref} data-testid="probe">
          {mode}
        </div>
      );
    }

    const { getByTestId } = render(<Probe />);
    expect(getByTestId("probe").textContent).toBe("compact");
    width = 600;
    act(() => vi.advanceTimersByTime(800));
    expect(getByTestId("probe").textContent).toBe("comfortable");
  });

  it("recovers healthy state when observer entries omit borderBoxSize", () => {
    vi.useFakeTimers();
    let resizeCallback!: ResizeObserverCallback;
    class LegacyResizeObserver implements ResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        resizeCallback = callback;
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", LegacyResizeObserver);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(performance.now()), 0),
    );
    vi.stubGlobal("cancelAnimationFrame", (id: number) =>
      window.clearTimeout(id),
    );
    const measure = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue({
        width: 400,
        height: 100,
        top: 0,
        right: 400,
        bottom: 100,
        left: 0,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      });

    function Probe() {
      const ref = useRef<HTMLDivElement | null>(null);
      const mode = useComposerLayoutMode(ref);
      return (
        <div ref={ref} data-testid="probe">
          {mode}
        </div>
      );
    }

    render(<Probe />);
    const target = document.querySelector("[data-testid='probe']") as Element;
    act(() => {
      const entry = { target } as ResizeObserverEntry;
      resizeCallback([entry], {} as ResizeObserver);
      resizeCallback([entry], {} as ResizeObserver);
    });
    const callsAfterRecovery = measure.mock.calls.length;
    act(() => vi.advanceTimersByTime(1_000));

    // The 500ms health sample remains, but the 250ms stale poll is stopped.
    expect(measure.mock.calls.length - callsAfterRecovery).toBeLessThanOrEqual(
      3,
    );
  });
});
