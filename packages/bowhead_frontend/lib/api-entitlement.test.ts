/**
 * Licensing FE slice A (§13.2) — resumeTurnAfterClarification must surface a 402/403 entitlement denial:
 * extract the nested-dict {error:{code,message}} (not render "[object Object]") and attach .status so the
 * caller (TurnContainer) can branch and stop the silent re-send.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { resumeTurnAfterClarification } from "./api";

const ORIGINAL_FETCH = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

function _resp(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("resumeTurnAfterClarification entitlement-denial surfacing", () => {
  it("extracts the nested-dict gate message + attaches .status on 402 (not [object Object])", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(402, { detail: { error: { code: "over_grace", message: "license 已过期，请续期" } } }),
    ) as typeof fetch;
    let caught: unknown;
    try {
      await resumeTurnAfterClarification("req1");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toContain("license 已过期");
    expect((caught as Error).message).not.toContain("[object Object]");
    expect((caught as { status?: number }).status).toBe(402);
  });

  it("still handles a legacy bare-string detail (+ attaches status)", async () => {
    globalThis.fetch = vi.fn(async () => _resp(409, { detail: "turn already running" })) as typeof fetch;
    let caught: unknown;
    try {
      await resumeTurnAfterClarification("req1");
    } catch (e) {
      caught = e;
    }
    expect((caught as Error).message).toContain("turn already running");
    expect((caught as { status?: number }).status).toBe(409);
  });

  it("returns the body on success", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(200, { ok: true, request_id: "r", turn_id: "t1", stream_url: "/api/v1/chat/stream/t1" }),
    ) as typeof fetch;
    const r = await resumeTurnAfterClarification("req1");
    expect(r.turn_id).toBe("t1");
  });
});
