import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useErrorDiagnosticsCapability } from "./use-error-diagnostics-capability";

const { appFetchMock } = vi.hoisted(() => ({
  appFetchMock: vi.fn(),
}));

vi.mock("@/lib/base-path", () => ({
  appFetch: appFetchMock,
}));

describe("useErrorDiagnosticsCapability", () => {
  beforeEach(() => {
    installMemoryStorage();
    appFetchMock.mockReset();
  });

  it("fails closed until health explicitly enables diagnostics", async () => {
    appFetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          feature_flags: { error_diagnostics: true },
        }),
        { status: 200 },
      ),
    );

    const { result } = renderHook(() =>
      useErrorDiagnosticsCapability(),
    );
    expect(result.current).toBe(false);
    await waitFor(() => expect(result.current).toBe(true));
    expect(appFetchMock).toHaveBeenCalledWith(
      "/health",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("stays disabled when health is unavailable or omits the flag", async () => {
    appFetchMock.mockResolvedValue(
      new Response(JSON.stringify({ feature_flags: {} }), {
        status: 200,
      }),
    );

    const { result } = renderHook(() =>
      useErrorDiagnosticsCapability(),
    );
    await waitFor(() => expect(appFetchMock).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });

  it("allows an explicit non-production override without a request", async () => {
    window.localStorage.setItem(
      "bowhead.feature.error_diagnostics",
      "1",
    );

    const { result } = renderHook(() =>
      useErrorDiagnosticsCapability(),
    );
    await waitFor(() => expect(result.current).toBe(true));
    expect(appFetchMock).not.toHaveBeenCalled();
  });

  it("reuses a capability already resolved by a parent adapter", async () => {
    const { result, rerender } = renderHook(
      ({ forced }) => useErrorDiagnosticsCapability(forced),
      { initialProps: { forced: true } },
    );

    expect(result.current).toBe(true);
    expect(appFetchMock).not.toHaveBeenCalled();
    rerender({ forced: false });
    await waitFor(() => expect(result.current).toBe(false));
    expect(appFetchMock).not.toHaveBeenCalled();
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
