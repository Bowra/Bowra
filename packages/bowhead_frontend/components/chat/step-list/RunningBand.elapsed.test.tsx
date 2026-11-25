// @vitest-environment jsdom
/**
 * The band stays mounted between Turns. Its timer must start from the new Turn, not from page load or
 * from the previous Turn's last tick (116, 2026-11-25: "1 分 58 秒" five seconds after sending into a
 * conversation opened two minutes earlier).
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { _resetTurnElapsedAnchorsForTesting, elapsedAnchorKeyOf } from "@/lib/turn-elapsed";

import { useElapsedText } from "./RunningBand";

type Props = { startedAt: number | null; running: boolean; key: string | null };

const t = ((key: string, values?: Record<string, number>) => `${key}:${JSON.stringify(values ?? {})}`) as never;

function renderElapsed(initialProps: Props) {
  return renderHook((p: Props) => useElapsedText(p.startedAt, p.running, t, p.key), { initialProps });
}

afterEach(() => {
  vi.useRealTimers();
  _resetTurnElapsedAnchorsForTesting();
});

describe("useElapsedText across Turns", () => {
  it("starts the next Turn near zero after the previous Turn ended two minutes earlier", () => {
    vi.useFakeTimers();
    const t0 = 1_800_000_000_000;
    vi.setSystemTime(t0);
    const { result, rerender } = renderElapsed({ startedAt: t0, running: true, key: elapsedAnchorKeyOf("t_prev", 1) });
    act(() => { vi.advanceTimersByTime(10_000); });
    rerender({ startedAt: t0, running: false, key: elapsedAnchorKeyOf("t_prev", 1) });

    vi.setSystemTime(t0 + 10_000 + 118_000);
    rerender({ startedAt: Date.now(), running: true, key: elapsedAnchorKeyOf("t_next", 1) });
    act(() => { vi.advanceTimersByTime(5_000); });

    expect(result.current).toBe('band_elapsed_sec:{"count":5}');
  });

  it("starts near zero when the conversation was opened two minutes before sending", () => {
    vi.useFakeTimers();
    const openedAt = 1_800_000_000_000;
    vi.setSystemTime(openedAt);
    // The conversation's earlier Turn is shown, finished, when the page opens.
    const earlierStart = openedAt - 2 * 86_400_000;
    const { result, rerender } = renderElapsed({ startedAt: earlierStart, running: false, key: elapsedAnchorKeyOf("t_old", 1) });

    vi.setSystemTime(openedAt + 118_000);
    rerender({ startedAt: Date.now(), running: true, key: elapsedAnchorKeyOf("t_new", 1) });
    act(() => { vi.advanceTimersByTime(5_000); });

    expect(result.current).toBe('band_elapsed_sec:{"count":5}');
  });

  it("keeps the server-ahead protection when the server start time lies in the browser's future", () => {
    vi.useFakeTimers();
    const sentAt = 1_800_000_000_000;
    vi.setSystemTime(sentAt);
    const key = elapsedAnchorKeyOf("t_skew", 1);
    const { result, rerender } = renderElapsed({ startedAt: sentAt, running: true, key });
    // turn_started replaces the browser send time with a server time 52 s ahead.
    rerender({ startedAt: sentAt + 52_000, running: true, key });
    act(() => { vi.advanceTimersByTime(7_000); });

    expect(result.current).toBe('band_elapsed_sec:{"count":7}');
  });
});
