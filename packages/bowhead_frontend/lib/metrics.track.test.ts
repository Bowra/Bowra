/**
 * Telemetry transport—batching + single-connection flush (2026-08-08, replacing `lib/track.test.ts`).
 *
 * Real-machine context: hydrating a 30-section report sends a dozen-plus `bowhead_canvas_report_perf_total`
 * events during one hydration; old implementation used **one `fetch(keepalive)` per event** → exhausted browser connections,
 * `ERR_INSUFFICIENT_RESOURCES`, and consumed the backend 120/60s quota per event → 429.
 *
 * ★ The key contract here is `counters are not deduplicated by transport`: 14 setContent calls must record 14 events.
 *   Transport deduplication erases what the instrument measures—those 14 calls are themselves the performance issue to report.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { _resetMetricsForTesting, flushMetrics, track, trackInteraction } from "./metrics";

function beaconMock() {
  return navigator.sendBeacon as unknown as ReturnType<typeof vi.fn>;
}

/** Parse (url, body) of the nth sendBeacon call. */
async function sentAt(n: number): Promise<{ url: string; body: unknown }> {
  const call = beaconMock().mock.calls[n];
  const blob = call[1] as Blob;
  return { url: String(call[0]), body: JSON.parse(await blob.text()) };
}

describe("metrics 传输层", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    _resetMetricsForTesting();
    Object.defineProperty(globalThis.navigator, "sendBeacon", {
      configurable: true,
      writable: true,
      value: vi.fn(() => true),
    });
  });

  afterEach(() => {
    _resetMetricsForTesting();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("★ counter 默认不去重——同 (name, labels) 连发 N 次就记 N 次", async () => {
    for (let i = 0; i < 14; i++) {
      track("bowhead_canvas_report_perf_total", {
        action: "set_content",
        sections: "16-30",
        doc_size: "50-150kb",
        duration: "150ms+",
      });
    }
    vi.advanceTimersByTime(300);

    // Preserve all 14 events' semantics…
    const { body } = await sentAt(0);
    expect((body as { events: unknown[] }).events).toHaveLength(14);
    // …while using only 1 connection / 1 rate-limit unit.
    expect(beaconMock()).toHaveBeenCalledTimes(1);
  });

  it("攒批窗口内的多条合成一次请求，走批量端点", async () => {
    track("bowhead_canvas_export_total", { action: "start", fmt: "pdf" });
    track("bowhead_canvas_export_total", { action: "done", fmt: "pdf" });
    expect(beaconMock()).not.toHaveBeenCalled(); // Still batching
    vi.advanceTimersByTime(300);

    const { url, body } = await sentAt(0);
    expect(url).toContain("/api/v1/metrics/events");
    expect((body as { events: unknown[] }).events).toHaveLength(2);
  });

  it("只有一条时走单条端点（不套 envelope）", async () => {
    track("bowhead_cmd_k_total");
    vi.advanceTimersByTime(300);

    const { url, body } = await sentAt(0);
    expect(url).toContain("/api/v1/metrics/event");
    expect(url).not.toContain("/events");
    expect(body).toEqual({ name: "bowhead_cmd_k_total", labels: {} });
  });

  it("超过一批上限时拆成多批，一条都不丢", async () => {
    for (let i = 0; i < 120; i++) track("bowhead_cmd_k_total", { i: String(i) });
    vi.advanceTimersByTime(300);

    let total = 0;
    for (let n = 0; n < beaconMock().mock.calls.length; n++) {
      const { body } = await sentAt(n);
      const events = (body as { events?: unknown[] }).events ?? [body];
      expect(events.length).toBeLessThanOrEqual(50);
      total += events.length;
    }
    expect(total).toBe(120);
  });

  it("页面隐藏时立刻 flush，不等窗口到期", async () => {
    track("bowhead_dark_toggle_total", { to: "dark" });
    expect(beaconMock()).not.toHaveBeenCalled();

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));

    expect(beaconMock()).toHaveBeenCalledTimes(1);
  });

  it("flushMetrics() 手动调用是幂等的（空队列不发空请求）", () => {
    flushMetrics();
    flushMetrics();
    expect(beaconMock()).not.toHaveBeenCalled();
  });

  describe("trackInteraction —— 交互类才去重", () => {
    it("100ms 内同 (name, labels) 只记一次", async () => {
      trackInteraction("bowhead_user_menu_total", { action: "open" });
      trackInteraction("bowhead_user_menu_total", { action: "open" });
      vi.advanceTimersByTime(300);

      const { body } = await sentAt(0);
      expect(body).toEqual({ name: "bowhead_user_menu_total", labels: { action: "open" } });
    });

    it("labels 不同不去重", async () => {
      trackInteraction("bowhead_dark_toggle_total", { to: "dark" });
      trackInteraction("bowhead_dark_toggle_total", { to: "light" });
      vi.advanceTimersByTime(300);

      const { body } = await sentAt(0);
      expect((body as { events: unknown[] }).events).toHaveLength(2);
    });

    it("超过窗口后再发同样事件正常记", async () => {
      trackInteraction("bowhead_cmd_k_total");
      vi.advanceTimersByTime(300);
      trackInteraction("bowhead_cmd_k_total");
      vi.advanceTimersByTime(300);
      expect(beaconMock()).toHaveBeenCalledTimes(2);
    });
  });
});
