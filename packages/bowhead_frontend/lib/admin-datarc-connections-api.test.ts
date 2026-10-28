/**
 * Admin datarc-connections API wrapper tests.
 * Asserts request shape, secret boundaries, typed errors, and diagnostics
 * projection. ApiError keeps legacy response fields for branching, but pages
 * must render only the audited ErrorPresentation projection.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createConnection,
  listConnections,
  rotateConnectionKey,
  setConnectionStatus,
  validateConnection,
} from "./admin-datarc-connections-api";
import { ApiError } from "./api-client";
import { genericUserFacingErrorMessage } from "./user-facing-error";

const ORIGINAL_FETCH = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

function _resp(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

const _CONN = {
  id: "dc_1", team_id: "team_a", name: "prod", base_url: "https://demo.datarc.cn",
  account_uid: "acc", key_hint: "wxyz", status: "active",
  created_at: "t", updated_at: "t",
};

describe("admin-datarc-connections-api", () => {
  it("listConnections GETs the collection (team-bound, credentials) and unwraps items", async () => {
    const spy = vi.fn(async () => _resp(200, { items: [_CONN] }));
    globalThis.fetch = spy as typeof fetch;
    const out = await listConnections();
    expect(spy).toHaveBeenCalledWith(
      "/api/v1/datarc-connections",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(out).toHaveLength(1);
    expect(out[0].key_hint).toBe("wxyz");
  });

  it("listConnections(true) adds ?include_archived=true", async () => {
    const spy = vi.fn(async () => _resp(200, { items: [] }));
    globalThis.fetch = spy as typeof fetch;
    await listConnections(true);
    expect(String((spy.mock.calls[0] as unknown[])[0])).toBe(
      "/api/v1/datarc-connections?include_archived=true",
    );
  });

  it("createConnection POSTs the body and never sends a stored secret back-channel", async () => {
    let url = "";
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
      url = String(u);
      body = JSON.parse(String(init?.body ?? "{}"));
      return _resp(200, _CONN);
    }) as typeof fetch;
    const out = await createConnection({
      name: "prod", base_url: "https://demo.datarc.cn", auth_key: "supersecret-wxyz", status: "archived",
    });
    expect(url).toBe("/api/v1/datarc-connections");
    expect(body.auth_key).toBe("supersecret-wxyz"); // sent on create...
    expect(body.status).toBe("archived");
    expect(out).not.toHaveProperty("auth_key"); // ...but the response carries only key_hint
    expect(out.key_hint).toBe("wxyz");
  });

  it("rotateConnectionKey PATCHes {auth_key} at the id", async () => {
    let url = ""; let method = ""; let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
      url = String(u); method = String(init?.method); body = JSON.parse(String(init?.body ?? "{}"));
      return _resp(200, { ..._CONN, key_hint: "1234" });
    }) as typeof fetch;
    const out = await rotateConnectionKey("dc_1", "new-key-1234");
    expect(method).toBe("PATCH");
    expect(url).toBe("/api/v1/datarc-connections/dc_1");
    expect(body).toEqual({ auth_key: "new-key-1234" });
    expect(out.key_hint).toBe("1234");
  });

  it("setConnectionStatus PATCHes {status}", async () => {
    let body: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      body = JSON.parse(String(init?.body ?? "{}"));
      return _resp(200, { ..._CONN, status: "archived" });
    }) as typeof fetch;
    const out = await setConnectionStatus("dc_1", "archived");
    expect(body).toEqual({ status: "archived" });
    expect(out.status).toBe("archived");
  });

  it("validateConnection POSTs /validate and returns the dry-run result", async () => {
    let url = "";
    globalThis.fetch = vi.fn(async (u: RequestInfo | URL) => {
      url = String(u);
      return _resp(200, { ok: true, workspace_count: 2, workspaces: [{ id: "1", name: "S" }] });
    }) as typeof fetch;
    const out = await validateConnection({ base_url: "https://demo.datarc.cn", auth_key: "k" });
    expect(url).toBe("/api/v1/datarc-connections/validate");
    expect(out.ok).toBe(true);
    expect(out.workspace_count).toBe(2);
  });

  it("keeps code and typed diagnostics without promoting raw detail", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(
        422,
        {
          detail: {
            code: "datarc_connection_url_rejected",
            diagnostics: {
              schema_version: 1,
              disclosure: "admin",
              diagnostic_code: "datarc_connection_failure",
              safe_detail: "phase=validate_input; failure_class=policy",
              retryable: false,
            },
          },
        },
        { "x-request-id": "0123456789abcdef0123456789abcdef" },
      ),
    ) as typeof fetch;
    const error = await createConnection({
      name: "x",
      base_url: "https://127.0.0.1",
      auth_key: "k",
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      code: "datarc_connection_url_rejected",
      requestId: "0123456789abcdef0123456789abcdef",
      diagnostics: {
        disclosure: "admin",
        diagnosticCode: "datarc_connection_failure",
        requestId: "0123456789abcdef0123456789abcdef",
        safeDetail: "phase=validate_input; failure_class=policy",
      },
    });
  });

  it("turns a semantic validation failure into a safe typed ApiError", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(
        200,
        {
          ok: false,
          error: "Authorization: Bearer upstream-secret",
          error_code: "datarc_connection_auth_rejected",
          diagnostics: {
            schema_version: 1,
            disclosure: "admin",
            diagnostic_code: "datarc_connection_failure",
            safe_detail: "phase=validate; failure_class=access",
            retryable: false,
          },
        },
        { "x-request-id": "fedcba9876543210fedcba9876543210" },
      ),
    ) as typeof fetch;

    const error = await validateConnection({
      base_url: "https://demo.datarc.cn",
      auth_key: "client-secret",
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 200,
      code: "datarc_connection_auth_rejected",
      requestId: "fedcba9876543210fedcba9876543210",
      diagnostics: {
        code: "datarc_connection_auth_rejected",
        disclosure: "admin",
        diagnosticCode: "datarc_connection_failure",
        requestId: "fedcba9876543210fedcba9876543210",
        safeDetail: "phase=validate; failure_class=access",
      },
    });
    expect((error as ApiError).message).toBe(
      genericUserFacingErrorMessage(),
    );
    expect((error as ApiError).message).not.toContain("upstream-secret");
  });

  it("handles structured detail without rendering objects or raw code", async () => {
    // FastAPI HTTPException(403, {code, message}) → detail is an object. Caught live by the
    // Playwright pass (panel showed "[object Object]"); extract .message, never String(obj).
    globalThis.fetch = vi.fn(async () =>
      _resp(403, { detail: { code: "not_ws_admin", message: "workspace admin required" } }),
    ) as typeof fetch;
    await expect(listConnections()).rejects.toThrow("workspace admin required");
    // A code-only detail has no audited user-facing prose. Keep the code for
    // branching, but use the current locale's safe fallback in Error.message.
    globalThis.fetch = vi.fn(async () =>
      _resp(401, { detail: { code: "user_not_active" } }),
    ) as typeof fetch;
    await expect(listConnections()).rejects.toThrow(
      genericUserFacingErrorMessage(),
    );
  });
});
