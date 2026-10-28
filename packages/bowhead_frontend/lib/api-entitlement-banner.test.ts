/**
 * Licensing FE slice A (§13.2 / §3.7) — resumeTurnAfterClarification feeds the entitlement banner:
 * a grace/degraded health on the 200 header, and a 402/403 deny code, both reach the store.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";

import { resumeTurnAfterClarification } from "./api";
import { _resetEntitlement, useEntitlement } from "./entitlement-store";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => _resetEntitlement());
afterEach(() => {
  // Unmount the React root created by renderHook: otherwise a scheduler callback after jsdom teardown
  // causes an uncaught `ReferenceError: window is not defined` (only exposed during parallel runs).
  cleanup();
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
  _resetEntitlement();
});

function view() {
  return renderHook(() => useEntitlement()).result.current;
}

function _resp(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

describe("resumeTurnAfterClarification → entitlement store", () => {
  it("a 200 with X-Entitlement-Health: grace publishes the grace banner", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(200, { ok: true, request_id: "r", turn_id: "t1", stream_url: "/s/t1" }, {
        "X-Entitlement-Health": "grace",
        "X-Entitlement-Grace-Until": "2026-06-30",
      }),
    ) as typeof fetch;
    await resumeTurnAfterClarification("req1");
    const v = view();
    expect(v.health).toBe("grace");
    expect(v.graceUntil).toBe("2026-06-30");
    expect(v.visible).toBe(true);
  });

  it("a 402 over_grace publishes the read-only banner", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(402, { detail: { error: { code: "over_grace", message: "license 已过期，请续期" } } }),
    ) as typeof fetch;
    try {
      await resumeTurnAfterClarification("req1");
    } catch {
      /* expected throw — the deny still publishes to the store */
    }
    const v = view();
    expect(v.health).toBe("over_grace");
    expect(v.readOnly).toBe(true);
  });

  it("a 200 active (no deny header) leaves the banner silent", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(200, { ok: true, request_id: "r", turn_id: "t1", stream_url: "/s/t1" }, {
        "X-Entitlement-Health": "active",
      }),
    ) as typeof fetch;
    await resumeTurnAfterClarification("req1");
    expect(view().visible).toBe(false);
  });

  it("SHADOW LEAK: a deny-state on the 200 header is ignored (stays silent)", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(200, { ok: true, request_id: "r", turn_id: "t1", stream_url: "/s/t1" }, {
        "X-Entitlement-Health": "over_grace",
      }),
    ) as typeof fetch;
    await resumeTurnAfterClarification("req1");
    expect(view().health).toBe("active");
    expect(view().visible).toBe(false);
  });
});
