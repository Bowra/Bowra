import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionArchiveCapability } from "./use-session-archive-capability";

const { appFetchMock } = vi.hoisted(() => ({
  appFetchMock: vi.fn(),
}));

vi.mock("@/lib/base-path", () => ({
  appFetch: appFetchMock,
}));

describe("useSessionArchiveCapability", () => {
  beforeEach(() => {
    installMemoryStorage();
    appFetchMock.mockReset();
  });

  it("fails closed until health explicitly enables the capability", async () => {
    appFetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          feature_flags: { session_archive: true },
        }),
        { status: 200 },
      ),
    );

    const { result } = renderHook(() =>
      useSessionArchiveCapability(),
    );
    expect(result.current).toBe(false);
    await waitFor(() => expect(result.current).toBe(true));
    expect(appFetchMock).toHaveBeenCalledWith(
      "/health",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("stays disabled when health omits the capability", async () => {
    appFetchMock.mockResolvedValue(
      new Response(JSON.stringify({ feature_flags: {} }), {
        status: 200,
      }),
    );

    const { result } = renderHook(() =>
      useSessionArchiveCapability(),
    );
    await waitFor(() => expect(appFetchMock).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });

  it("allows an explicit non-production override without a request", async () => {
    window.localStorage.setItem(
      "bowhead.feature.session_archive",
      "1",
    );

    const { result } = renderHook(() =>
      useSessionArchiveCapability(),
    );
    await waitFor(() => expect(result.current).toBe(true));
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
