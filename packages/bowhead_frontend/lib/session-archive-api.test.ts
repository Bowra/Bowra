import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  archiveSession,
  fetchSessions,
  restoreSession,
  SessionArchiveError,
} from "./api";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  installMemoryStorage();
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

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

describe("session archive API", () => {
  it("scopes active and archived lists to the current space", async () => {
    window.localStorage.setItem("bowhead.space_id", "ws_sales");
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({ groups: [], archived_count: 3 }),
        { status: 200 },
      ),
    );
    globalThis.fetch = fetchMock as typeof fetch;

    const result = await fetchSessions({
      view: "archived",
      query: "pricing",
      limit: 25,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/sessions?view=archived&q=pricing&limit=25&workspace_id=ws_sales",
      expect.objectContaining({ cache: "no-store" }),
    );
    // appFetch normalizes headers into a Headers instance (d59ce0c12).
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    const headers = new Headers(init.headers);
    expect(headers.get("X-Space-Id")).toBe("ws_sales");
    expect(headers.get("X-Workspace-Id")).toBe("ws_sales");
    expect(result.archived_count).toBe(3);
  });

  it("preserves the stable running-session error code", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          detail: {
            error: { code: "session_archive_active_turn" },
          },
        }),
        { status: 409 },
      ),
    ) as typeof fetch;

    await expect(archiveSession("s_running")).rejects.toMatchObject({
      status: 409,
      code: "session_archive_active_turn",
    });
  });

  it("uses idempotent DELETE for restore", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    globalThis.fetch = fetchMock as typeof fetch;

    await restoreSession("s_old");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/sessions/s_old/archive",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("falls back to a stable error when a proxy returns non-JSON", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response("upstream failed", { status: 502 }),
    ) as typeof fetch;

    let caught: unknown;
    try {
      await archiveSession("s_1");
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(SessionArchiveError);
    expect(caught).toMatchObject({
      status: 502,
      code: "session_archive_failed",
    });
  });
});
