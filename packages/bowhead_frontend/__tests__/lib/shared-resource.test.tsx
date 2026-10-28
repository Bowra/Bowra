/**
 * Shared resources — single ownership + single cache.
 *
 * Asserts the properties every consumer relies on:
 *   1. Concurrent readers share one in-flight request.
 *   2. A settled value is reused inside the TTL and refetched after it.
 *   3. Two components mounted against the same key issue one request and
 *      observe the same snapshot.
 *   4. Different keys (e.g. two spaces) never share a cache entry.
 *   5. `invalidate()` during a flight discards that response instead of
 *      caching it.
 *   6. A broadcast burst coalesces into one refresh.
 *   7. The poll interval is per resource, not per subscriber, and stops when
 *      the last subscriber unmounts.
 */
import { act, render, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSharedResource,
  useSharedResource,
} from "@/lib/shared-resource";

const flush = () => act(async () => { await Promise.resolve(); });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("createSharedResource", () => {
  it("shares one in-flight request between concurrent readers", async () => {
    const gate = deferred<string>();
    const fetcher = vi.fn(() => gate.promise);
    const resource = createSharedResource({
      name: "t",
      fetcher,
      ttlMs: 1_000,
    });

    const a = resource.read("k");
    const b = resource.read("k");
    expect(fetcher).toHaveBeenCalledTimes(1);

    gate.resolve("value");
    await expect(a).resolves.toBe("value");
    await expect(b).resolves.toBe("value");
  });

  it("reuses a settled value inside the TTL and refetches after it", async () => {
    let calls = 0;
    const fetcher = vi.fn(async () => `v${++calls}`);
    const resource = createSharedResource({
      name: "t",
      fetcher,
      ttlMs: 1_000,
    });

    const now = vi.spyOn(Date, "now");
    now.mockReturnValue(1_000);
    await expect(resource.read("k")).resolves.toBe("v1");
    now.mockReturnValue(1_500);
    await expect(resource.read("k")).resolves.toBe("v1");
    expect(fetcher).toHaveBeenCalledTimes(1);

    now.mockReturnValue(2_500);
    await expect(resource.read("k")).resolves.toBe("v2");
    expect(fetcher).toHaveBeenCalledTimes(2);
    now.mockRestore();
  });

  it("keeps separate cache entries per key", async () => {
    const fetcher = vi.fn(async (key: string) => `for:${key}`);
    const resource = createSharedResource({
      name: "t",
      fetcher,
      ttlMs: 60_000,
    });

    await expect(resource.read("ws_a")).resolves.toBe("for:ws_a");
    await expect(resource.read("ws_b")).resolves.toBe("for:ws_b");
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(resource.peek("ws_a").data).toBe("for:ws_a");
    expect(resource.peek("ws_b").data).toBe("for:ws_b");
  });

  it("drops a response that lands after an invalidation", async () => {
    const gate = deferred<string>();
    const fetcher = vi.fn(() => gate.promise);
    const resource = createSharedResource({
      name: "t",
      fetcher,
      ttlMs: 60_000,
    });

    const pending = resource.read("k");
    resource.invalidate("k");
    gate.resolve("stale");
    await pending;

    expect(resource.peek("k").data).toBeUndefined();
    // The next read must go back to the network rather than serve "stale".
    await resource.read("k");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("keeps the last good value when a refresh fails", async () => {
    let attempt = 0;
    const fetcher = vi.fn(async () => {
      attempt += 1;
      if (attempt === 2) throw new Error("network down");
      return "good";
    });
    const resource = createSharedResource({ name: "t", fetcher, ttlMs: 0 });

    await resource.read("k");
    await expect(resource.refresh("k")).rejects.toThrow("network down");
    const snapshot = resource.peek("k");
    expect(snapshot.data).toBe("good");
    expect(snapshot.error?.message).toBe("network down");
  });
});

describe("useSharedResource", () => {
  it("issues one request for two components on the same key", async () => {
    const fetcher = vi.fn(async () => "shared");
    const resource = createSharedResource({
      name: "t",
      fetcher,
      ttlMs: 60_000,
    });
    const seen: string[] = [];

    function Consumer({ label }: { label: string }) {
      const { data } = useSharedResource(resource, "k");
      if (data) seen.push(`${label}:${data}`);
      return null;
    }

    render(
      <>
        <Consumer label="sidebar" />
        <Consumer label="drawer" />
      </>,
    );

    await waitFor(() => expect(seen).toContain("sidebar:shared"));
    expect(seen).toContain("drawer:shared");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not subscribe or fetch while disabled", async () => {
    const fetcher = vi.fn(async () => "x");
    const resource = createSharedResource({ name: "t", fetcher, ttlMs: 60_000 });

    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useSharedResource(resource, "k", { enabled }),
      { initialProps: { enabled: false } },
    );
    await flush();
    expect(fetcher).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  });

  it("coalesces a burst of invalidation broadcasts into one refresh", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(async () => "v");
    const resource = createSharedResource({
      name: "t",
      fetcher,
      ttlMs: 60_000,
      invalidateOnEvents: ["test:changed"],
      coalesceMs: 50,
    });

    renderHook(() => useSharedResource(resource, "k"));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => {
      window.dispatchEvent(new Event("test:changed"));
      window.dispatchEvent(new Event("test:changed"));
      window.dispatchEvent(new Event("test:changed"));
      await vi.advanceTimersByTimeAsync(60);
    });

    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("does not re-render subscribers when a poll returns the same data", async () => {
    vi.useFakeTimers();
    const payload = { items: ["a", "b"] };
    const resource = createSharedResource({
      name: "t",
      // A fresh object every time, like an adapter that rebuilds its result.
      fetcher: async () => JSON.parse(JSON.stringify(payload)) as typeof payload,
      ttlMs: 0,
      pollMs: 1_000,
      equals: (previous, next) =>
        JSON.stringify(previous) === JSON.stringify(next),
    });

    let renders = 0;
    renderHook(() => {
      renders += 1;
      return useSharedResource(resource, "k");
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const settled = renders;

    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    // Three polls, three identical responses, zero renders — the composer
    // subscribes to the session list and must not churn while typing.
    expect(renders).toBe(settled);

    payload.items.push("c");
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(renders).toBeGreaterThan(settled);
  });

  it("keeps invalidating after the last subscriber unmounts", async () => {
    // A settings surface can rename a space and unmount before anything
    // re-subscribes; dropping that broadcast would serve a known-stale list.
    let calls = 0;
    const resource = createSharedResource({
      name: "t",
      fetcher: async () => `v${++calls}`,
      ttlMs: 60_000,
      invalidateOnEvents: ["test:changed"],
      coalesceMs: 0,
    });

    const mounted = renderHook(() => useSharedResource(resource, "k"));
    await waitFor(() => expect(calls).toBe(1));
    mounted.unmount();

    await act(async () => {
      window.dispatchEvent(new Event("test:changed"));
    });
    await expect(resource.read("k")).resolves.toBe("v2");
  });

  it("runs one poll timer for the resource and stops it on the last unmount", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(async () => "v");
    const resource = createSharedResource({
      name: "t",
      fetcher,
      ttlMs: 0,
      pollMs: 1_000,
    });

    const first = renderHook(() => useSharedResource(resource, "k"));
    const second = renderHook(() => useSharedResource(resource, "k"));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(fetcher).toHaveBeenCalledTimes(1);

    // Two subscribers, one tick — one request, not two.
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(fetcher).toHaveBeenCalledTimes(2);

    first.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(fetcher).toHaveBeenCalledTimes(3);

    second.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });
});
