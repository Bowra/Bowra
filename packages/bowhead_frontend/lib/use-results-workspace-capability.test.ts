import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { retryResultsCapabilities, useResultsCapabilities } from "./use-results-capabilities";
import {
  resetResultsWorkspaceCapabilityForTests,
  useResultsWorkspaceCapability,
  useResultsWorkspaceCapabilitySnapshot,
} from "./use-results-workspace-capability";

const { appFetchMock, authSessionMock } = vi.hoisted(() => ({
  appFetchMock: vi.fn(),
  authSessionMock: vi.fn(() => null as unknown),
}));

vi.mock("@/lib/base-path", () => ({
  appFetch: appFetchMock,
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: authSessionMock,
}));

function negotiated(
  values: Partial<{ workspace: boolean; complete_list: boolean; scale_nav: boolean; interactive_html: boolean }> = {},
): Response {
  return new Response(JSON.stringify({
    dto_version: 1,
    workspace: true,
    complete_list: false,
    scale_nav: false,
    max_page_size: 50,
    ...values,
  }), { status: 200 });
}

describe("useResultsWorkspaceCapability", () => {
  beforeEach(() => {
    installMemoryStorage();
    resetResultsWorkspaceCapabilityForTests();
    appFetchMock.mockReset();
    authSessionMock.mockReturnValue(null);
  });

  afterEach(() => {
    cleanup();
    resetResultsWorkspaceCapabilityForTests();
    vi.useRealTimers();
  });

  it("negotiates parent, Complete and Scale from the authenticated endpoint", async () => {
    appFetchMock.mockResolvedValue(negotiated({
      complete_list: true,
      scale_nav: true,
    }));

    const { result } = renderHook(() => useResultsCapabilities());
    expect(result.current).toEqual({
      workspace: false,
      completeList: false,
      scaleNav: false,
      interactiveHtml: false,
      resolved: false,
      error: null,
    });
    await waitFor(() => expect(result.current).toEqual({
      workspace: true,
      completeList: true,
      scaleNav: true,
      interactiveHtml: false,
      resolved: true,
      error: null,
    }));
  });

  it.each([true, false, "true", null, undefined])("admits interactive HTML only for a boolean true: %s", async (flag) => {
    appFetchMock.mockResolvedValue(new Response(JSON.stringify({
      dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50, interactive_html: flag,
    })));
    const { result } = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(result.current.resolved).toBe(true));
    expect(result.current.interactiveHtml).toBe(flag === true);
  });

  it("revokes interactive execution on authenticated capability refresh", async () => {
    appFetchMock.mockResolvedValueOnce(negotiated({ interactive_html: true }))
      .mockResolvedValueOnce(negotiated({ interactive_html: false }));
    const { result } = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(result.current.interactiveHtml).toBe(true));
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(result.current.interactiveHtml).toBe(false));
  });

  it("falls back to health for the parent only on an old backend", async () => {
    appFetchMock.mockImplementation((path: string) => Promise.resolve(
      path === "/health"
        ? new Response(JSON.stringify({ feature_flags: { results_workspace: true } }), { status: 200 })
        : new Response(null, { status: 404 }),
    ));

    const { result } = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(result.current.workspace).toBe(true));
    expect(result.current.completeList).toBe(false);
    expect(result.current.scaleNav).toBe(false);
    expect(result.current.resolved).toBe(true);
    expect(appFetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows a retryable unresolved state for malformed capabilities, not a disabled workspace", async () => {
    appFetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ dto_version: 2 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ feature_flags: {} }), { status: 200 }));

    const { result } = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(result.current.error).toBe("unavailable"));
    expect(appFetchMock).toHaveBeenCalledTimes(1);
    expect(result.current).toEqual({
      workspace: false,
      completeList: false,
      scaleNav: false,
      interactiveHtml: false,
      resolved: false,
      error: "unavailable",
    });
  });

  it("deduplicates capability requests across mounted readers", async () => {
    let resolveCapability!: (response: Response) => void;
    appFetchMock.mockImplementation(
      () => new Promise<Response>((resolve) => {
        resolveCapability = resolve;
      }),
    );

    const first = renderHook(() => useResultsWorkspaceCapability());
    const second = renderHook(() => useResultsWorkspaceCapability());
    await waitFor(() => expect(appFetchMock).toHaveBeenCalledTimes(1));
    await act(async () => {
      resolveCapability(negotiated());
    });
    expect(first.result.current).toBe(true);
    expect(second.result.current).toBe(true);
  });

  it("lets nested affordances follow the shared snapshot without loading", async () => {
    appFetchMock.mockResolvedValue(negotiated());

    const nested = renderHook(() => useResultsWorkspaceCapabilitySnapshot());
    expect(nested.result.current).toBe(false);
    expect(appFetchMock).not.toHaveBeenCalled();

    const owner = renderHook(() => useResultsWorkspaceCapability());
    await waitFor(() => expect(owner.result.current).toBe(true));
    expect(nested.result.current).toBe(true);
    expect(appFetchMock).toHaveBeenCalledTimes(1);
  });

  it("allows a development override only for the parent capability", async () => {
    window.localStorage.setItem("bowhead.feature.results_workspace", "1");
    appFetchMock.mockResolvedValue(new Response(null, { status: 404 }));

    const result = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(result.result.current.workspace).toBe(true));
    expect(result.result.current.completeList).toBe(false);
    expect(result.result.current.scaleNav).toBe(false);
    expect(result.result.current.resolved).toBe(true);
    expect(appFetchMock).toHaveBeenCalledTimes(1);
  });

  it("revalidates the authenticated kill switch on focus", async () => {
    appFetchMock
      .mockResolvedValueOnce(negotiated())
      .mockResolvedValueOnce(negotiated({ workspace: false }));

    const workspace = renderHook(() => useResultsWorkspaceCapability());
    await waitFor(() => expect(workspace.result.current).toBe(true));

    act(() => window.dispatchEvent(new Event("focus")));

    await waitFor(() => expect(workspace.result.current).toBe(false));
    expect(appFetchMock).toHaveBeenCalledTimes(2);
  });

  it.each(["network", "500", "invalid-json"])("keeps cold %s failures retryable without negotiating health", async (failure) => {
    if (failure === "network") appFetchMock.mockRejectedValueOnce(new TypeError("offline"));
    else appFetchMock.mockResolvedValueOnce(new Response(failure === "invalid-json" ? "{" : null, {
      status: failure === "500" ? 500 : 200,
    }));
    const { result } = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(result.current.error).toBe("unavailable"));
    expect(result.current.resolved).toBe(false);
    expect(appFetchMock).toHaveBeenCalledTimes(1);

    appFetchMock.mockResolvedValueOnce(negotiated({ complete_list: true, scale_nav: true }));
    await act(() => retryResultsCapabilities());
    expect(result.current).toMatchObject({ resolved: true, workspace: true, scaleNav: true, error: null });
  });

  it("preserves all confirmed capabilities on a transient refresh failure", async () => {
    appFetchMock.mockResolvedValueOnce(negotiated({ complete_list: true, scale_nav: true }));
    const { result } = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(result.current.scaleNav).toBe(true));
    const confirmed = result.current;
    appFetchMock.mockResolvedValueOnce(new Response(null, { status: 503 }));
    await act(() => retryResultsCapabilities());
    expect(result.current).toBe(confirmed);
    expect(appFetchMock).toHaveBeenCalledTimes(2);
  });

  it("bounds the entire capability negotiation and ignores a late response after retry", async () => {
    vi.useFakeTimers();
    let finishOld!: (response: Response) => void;
    appFetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { finishOld = resolve; }));
    const { result } = renderHook(() => useResultsCapabilities());
    const signal = appFetchMock.mock.calls[0][1].signal as AbortSignal;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(result.current).toMatchObject({ resolved: false, error: "timeout" });
    expect(signal.aborted).toBe(true);

    appFetchMock.mockResolvedValueOnce(negotiated({ complete_list: true, scale_nav: true }));
    await act(() => retryResultsCapabilities());
    await act(async () => { finishOld(negotiated({ workspace: false })); });
    expect(result.current).toMatchObject({ resolved: true, workspace: true, scaleNav: true, error: null });
  });

  it("automatically retries a cold timeout after the short backoff", async () => {
    vi.useFakeTimers();
    appFetchMock
      .mockImplementationOnce(() => new Promise<Response>(() => {}))
      .mockResolvedValueOnce(negotiated({ complete_list: true, scale_nav: true }));
    const { result } = renderHook(() => useResultsCapabilities());

    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(result.current).toMatchObject({ resolved: false, error: "timeout" });
    expect(appFetchMock).toHaveBeenCalledTimes(1);

    await act(() => vi.advanceTimersByTimeAsync(4_999));
    expect(appFetchMock).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1));

    expect(appFetchMock).toHaveBeenCalledTimes(2);
    expect(result.current).toMatchObject({
      resolved: true,
      workspace: true,
      completeList: true,
      scaleNav: true,
      interactiveHtml: false,
      error: null,
    });
  });

  it("limits short failure retries before returning to the normal refresh cadence", async () => {
    vi.useFakeTimers();
    appFetchMock.mockRejectedValue(new TypeError("offline"));
    renderHook(() => useResultsCapabilities());
    await act(async () => { await Promise.resolve(); });
    expect(appFetchMock).toHaveBeenCalledTimes(1);

    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(appFetchMock).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(appFetchMock).toHaveBeenCalledTimes(3);
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(appFetchMock).toHaveBeenCalledTimes(4);

    await act(() => vi.advanceTimersByTimeAsync(24_999));
    expect(appFetchMock).toHaveBeenCalledTimes(4);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(appFetchMock).toHaveBeenCalledTimes(5);
  });

  it("cancels a scheduled failure retry when the authenticated scope changes", async () => {
    vi.useFakeTimers();
    const auth = (id: string) => ({
      status: "authenticated",
      me: { id, team_id: "team", auth_session_generation: "g1" },
    });
    authSessionMock.mockReturnValue(auth("a"));
    appFetchMock
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(negotiated({ complete_list: true, scale_nav: true }));
    const { result, rerender } = renderHook(() => useResultsCapabilities());
    await act(async () => { await Promise.resolve(); });
    expect(appFetchMock).toHaveBeenCalledTimes(1);

    authSessionMock.mockReturnValue(auth("b"));
    rerender();
    await act(async () => { await Promise.resolve(); });
    expect(result.current).toMatchObject({ resolved: true, workspace: true });
    expect(appFetchMock).toHaveBeenCalledTimes(2);

    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(appFetchMock).toHaveBeenCalledTimes(2);
  });

  it("aborts an unresolved request when the last reader unmounts", async () => {
    let resolveOld!: (response: Response) => void;
    appFetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => {
      resolveOld = resolve;
    }));
    const first = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(appFetchMock).toHaveBeenCalledTimes(1));
    const oldSignal = appFetchMock.mock.calls[0][1].signal as AbortSignal;

    first.unmount();
    expect(oldSignal.aborted).toBe(true);

    appFetchMock.mockResolvedValueOnce(negotiated({ complete_list: true, scale_nav: true }));
    const second = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(second.result.current.scaleNav).toBe(true));
    await act(async () => { resolveOld(negotiated({ workspace: false })); });

    expect(appFetchMock).toHaveBeenCalledTimes(2);
    expect(second.result.current).toMatchObject({
      resolved: true,
      workspace: true,
      scaleNav: true,
      interactiveHtml: false,
    });
  });

  it("does not call a failed legacy health check an explicit disable", async () => {
    appFetchMock.mockResolvedValueOnce(new Response(null, { status: 405 }))
      .mockResolvedValueOnce(new Response(null, { status: 502 }));
    const { result } = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(result.current.error).toBe("unavailable"));
    expect(result.current.resolved).toBe(false);
    expect(appFetchMock.mock.calls.map(([path]) => path)).toEqual(["/api/v1/results/capabilities", "/health"]);
  });

  it.each([401, 403])("discards a confirmed snapshot on HTTP %s and revalidates identity", async (status) => {
    const revalidateCapabilities = vi.fn();
    authSessionMock.mockReturnValue({ status: "authenticated", me: { id: "a", team_id: "team", auth_session_generation: "g1" }, revalidateCapabilities });
    appFetchMock.mockResolvedValueOnce(negotiated());
    const { result } = renderHook(() => useResultsCapabilities());
    await waitFor(() => expect(result.current.workspace).toBe(true));
    appFetchMock.mockResolvedValueOnce(new Response(null, { status }));
    await act(() => retryResultsCapabilities());
    expect(result.current).toMatchObject({ workspace: false, resolved: false, error: "auth" });
    expect(revalidateCapabilities).toHaveBeenCalledTimes(1);
    expect(appFetchMock).toHaveBeenCalledTimes(2);
  });

  it("fences an old principal's pending response when the authenticated scope changes", async () => {
    const auth = (id: string) => ({ status: "authenticated", me: { id, team_id: "team", auth_session_generation: "g1" } });
    authSessionMock.mockReturnValue(auth("a"));
    let finishOld!: (response: Response) => void;
    appFetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { finishOld = resolve; }));
    const { result, rerender } = renderHook(() => useResultsCapabilities());
    const signal = appFetchMock.mock.calls[0][1].signal as AbortSignal;
    authSessionMock.mockReturnValue(auth("b"));
    appFetchMock.mockResolvedValueOnce(negotiated({ workspace: false }));
    rerender();
    await waitFor(() => expect(result.current.resolved).toBe(true));
    await act(async () => { finishOld(negotiated()); });
    expect(signal.aborted).toBe(true);
    expect(result.current.workspace).toBe(false);
  });

  it.each(["id", "team_id", "auth_session_generation"])("masks a cached capability in the first render after %s changes", async (field) => {
    const me = { id: "a", team_id: "team", auth_session_generation: "g1" };
    authSessionMock.mockReturnValue({ status: "authenticated", me });
    appFetchMock.mockResolvedValueOnce(negotiated());
    const renders: boolean[] = [];
    const { result, rerender } = renderHook(() => {
      const value = useResultsCapabilities();
      renders.push(value.workspace);
      return value;
    });
    await waitFor(() => expect(result.current.workspace).toBe(true));
    renders.length = 0;
    authSessionMock.mockReturnValue({ status: "authenticated", me: { ...me, [field]: "new" } });
    appFetchMock.mockImplementationOnce(() => new Promise<Response>(() => {}));
    rerender();
    expect(renders.length).toBeGreaterThan(0);
    expect(renders.every((workspace) => workspace === false)).toBe(true);
    expect(result.current.resolved).toBe(false);
  });

  it("applies the same deadline to the legacy health response body", async () => {
    vi.useFakeTimers();
    appFetchMock.mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(new Response(new ReadableStream()));
    const { result } = renderHook(() => useResultsCapabilities());
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(appFetchMock).toHaveBeenCalledTimes(2);
    expect(result.current).toMatchObject({ resolved: false, error: "timeout" });
  });

  it("refreshes every 60 seconds and stops polling when all readers unmount", async () => {
    vi.useFakeTimers();
    appFetchMock.mockImplementation(() => Promise.resolve(negotiated()));
    const { unmount } = renderHook(() => useResultsCapabilities());
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(appFetchMock).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(appFetchMock).toHaveBeenCalledTimes(2);
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(appFetchMock).toHaveBeenCalledTimes(2);
  });
});

function installMemoryStorage(): void {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => {
        values.set(key, value);
      },
      removeItem: (key: string) => {
        values.delete(key);
      },
      clear: () => values.clear(),
      key: (index: number) => [...values.keys()][index] ?? null,
      get length() {
        return values.size;
      },
    } satisfies Storage,
  });
}
