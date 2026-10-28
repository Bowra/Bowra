import { afterEach, describe, expect, it, vi } from "vitest";

import { headersExactly } from "@/__tests__/request-headers";
import { fetchApprovals } from "./api";

const ORIGINAL_FETCH = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("approvals API", () => {
  it("refreshes an expired session and retries the approvals request", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/auth/refresh") {
        return jsonResponse(200, { ok: true, token: "new-token", user: {} });
      }
      if (path === "/api/v1/approvals?scope=pending") {
        const approvalCalls = fetchSpy.mock.calls.filter(
          ([callInput]) => String(callInput) === path,
        ).length;
        return approvalCalls === 1
          ? jsonResponse(401, { detail: "expired" })
          : jsonResponse(200, { items: [] });
      }
      return jsonResponse(404, {});
    });
    globalThis.fetch = fetchSpy as typeof fetch;

    await expect(fetchApprovals()).resolves.toEqual([]);
    expect(fetchSpy.mock.calls).toEqual([
      ["/api/v1/approvals?scope=pending", { credentials: "include", headers: headersExactly({}) }],
      ["/api/v1/auth/refresh", expect.objectContaining({ credentials: "include" })],
      ["/api/v1/approvals?scope=pending", { credentials: "include", headers: headersExactly({}) }],
    ]);
  });
});
