import { afterEach, describe, expect, it, vi } from "vitest";

import { headersExactly } from "@/__tests__/request-headers";
import { ApiError, apiJson } from "./api-client";

const ORIGINAL_FETCH = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  document.documentElement.lang = "";
  vi.restoreAllMocks();
});

function jsonResp(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

describe("api-client", () => {
  it("refreshes once on 401 and retries the original request", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/auth/refresh") {
        return jsonResp(200, { ok: true, token: "new-token", user: {} });
      }
      if (path === "/api/v1/settings") {
        return fetchSpy.mock.calls.filter(([callInput]) => String(callInput) === path).length === 1
          ? jsonResp(401, { detail: "expired" })
          : jsonResp(200, { ok: true });
      }
      return jsonResp(404, {});
    });
    globalThis.fetch = fetchSpy as typeof fetch;

    await expect(apiJson<{ ok: boolean }>("/api/v1/settings")).resolves.toEqual({ ok: true });

    expect(fetchSpy).toHaveBeenCalledTimes(3);
    expect(fetchSpy.mock.calls[0]).toEqual([
      "/api/v1/settings",
      // appFetch always sends a Headers instance carrying the public base (d59ce0c12).
      { credentials: "include", headers: headersExactly({}) },
    ]);
    expect(fetchSpy.mock.calls[1][0]).toBe("/api/v1/auth/refresh");
    expect(fetchSpy.mock.calls[2]).toEqual([
      "/api/v1/settings",
      // appFetch always sends a Headers instance carrying the public base (d59ce0c12).
      { credentials: "include", headers: headersExactly({}) },
    ]);
  });

  it("surfaces structured FastAPI error messages and codes", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResp(429, {
        detail: {
          code: "resend_cooldown",
          message: "Please wait before resending",
          retry_after_seconds: 30,
        },
      }),
    ) as typeof fetch;

    await expect(apiJson("/api/v1/settings")).rejects.toMatchObject({
      name: "ApiError",
      status: 429,
      code: "resend_cooldown",
      message: "Please wait before resending",
    } satisfies Partial<ApiError>);
  });

  it("keeps structured error details next to the code", async () => {
    const blocked = [{ kind: "skill", name: "Sales report", identity: "team:sk_1", reason_codes: ["skill_disabled"] }];
    globalThis.fetch = vi.fn(async () =>
      jsonResp(422, {
        detail: { error: { code: "agent_dependency_blocked", details: { reason_codes: ["skill_disabled"], blocked_items: blocked } } },
      }),
    ) as typeof fetch;

    const failure = await apiJson("/api/v1/agents/osa_1/working-copy").catch((cause: unknown) => cause);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).code).toBe("agent_dependency_blocked");
    expect((failure as ApiError).details).toEqual({ reason_codes: ["skill_disabled"], blocked_items: blocked });
  });

  it("parses finite FastAPI validation issues without Pydantic prose or input", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResp(422, {
        detail: {
          code: "request_validation_failed",
          message: "请检查标出的内容后重试。",
          field_errors: [
            {
              path: "email",
              code: "invalid_format",
              params: {},
              msg: "Value error, invalid email",
              input: "invalid-email",
            },
          ],
        },
      }),
    ) as typeof fetch;

    await expect(apiJson("/api/v1/admin/members")).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      code: "request_validation_failed",
      message: "请检查标出的内容后重试。",
      validationIssues: [
        {
          path: "email",
          code: "invalid_format",
          params: {},
        },
      ],
    } satisfies Partial<ApiError>);
  });

  it("never promotes legacy Pydantic detail arrays to the main message", async () => {
    document.documentElement.lang = "en-US";
    globalThis.fetch = vi.fn(async () =>
      jsonResp(422, {
        detail: [
          {
            type: "value_error",
            loc: ["body", "password"],
            msg: "Value error, password=hunter2",
            input: "hunter2",
          },
        ],
      }),
    ) as typeof fetch;

    await expect(apiJson("/api/v1/admin/members")).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      message:
        "Something went wrong. Please try again. If the problem continues, contact your administrator.",
      diagnostics: undefined,
      validationIssues: undefined,
    } satisfies Partial<ApiError>);
  });

  it("parses versioned diagnostics and merges authoritative HTTP facts", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResp(
        421,
        {
          detail: {
            code: "public_entry_not_registered",
            message: "当前公开入口未登记。",
            diagnostics: {
              schema_version: 1,
              disclosure: "public-neutral",
              diagnostic_code: "public_entry_registry_miss",
              retryable: false,
            },
          },
        },
        { "x-request-id": "0123456789abcdef0123456789abcdef" },
      ),
    ) as typeof fetch;

    await expect(apiJson("/api/v1/auth/login")).rejects.toMatchObject({
      name: "ApiError",
      status: 421,
      code: "public_entry_not_registered",
      requestId: "0123456789abcdef0123456789abcdef",
      diagnostics: {
        schemaVersion: 1,
        disclosure: "public-neutral",
        status: 421,
        code: "public_entry_not_registered",
        diagnosticCode: "public_entry_registry_miss",
        requestId: "0123456789abcdef0123456789abcdef",
        retryable: false,
      },
    } satisfies Partial<ApiError>);
  });

  it("surfaces top-level {error:{code,message}} bodies (gateway CodedError shape)", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResp(400, {
        error: {
          code: "E_SKILL_NAME_EXISTS",
          message: "skill 'foo' 已存在；确认覆盖后将替换旧版本",
        },
      }),
    ) as typeof fetch;

    await expect(apiJson("/api/v1/skills/upload")).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      code: "E_SKILL_NAME_EXISTS",
      message: "skill 'foo' 已存在；确认覆盖后将替换旧版本",
    } satisfies Partial<ApiError>);
  });

  it("surfaces top-level {error:{message}} bodies without code", async () => {
    globalThis.fetch = vi.fn(async () => jsonResp(400, { error: { message: "raw validation text" } })) as typeof fetch;

    await expect(apiJson("/api/v1/skills/upload")).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      message: "raw validation text",
    } satisfies Partial<ApiError>);
  });

  it("keeps a code for branching but never displays a code-only detail", async () => {
    document.documentElement.lang = "en-US";
    globalThis.fetch = vi.fn(async () =>
      jsonResp(409, {
        detail: {
          code: "team_not_found",
        },
      }),
    ) as typeof fetch;

    await expect(apiJson("/api/v1/admin/members")).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      code: "team_not_found",
      message:
        "Something went wrong. Please try again. If the problem continues, contact your administrator.",
    } satisfies Partial<ApiError>);
  });

  it("never stringifies an unknown structured error into the UI", async () => {
    document.documentElement.lang = "zh-CN";
    globalThis.fetch = vi.fn(async () =>
      jsonResp(500, {
        detail: {
          internal_state: "broken",
        },
      }),
    ) as typeof fetch;

    await expect(apiJson("/api/v1/settings")).rejects.toMatchObject({
      name: "ApiError",
      status: 500,
      message: "操作失败，请稍后重试；如问题持续，请联系管理员。",
    } satisfies Partial<ApiError>);
  });

  it("never exposes an upstream HTML error page to the UI", async () => {
    document.documentElement.lang = "zh-CN";
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          "<!doctype html><html><body><h1>502 Bad Gateway</h1></body></html>",
          {
            status: 502,
            headers: { "content-type": "text/html; charset=utf-8" },
          },
        ),
    ) as typeof fetch;

    await expect(apiJson("/api/v1/settings")).rejects.toMatchObject({
      name: "ApiError",
      status: 502,
      message: "操作失败，请稍后重试；如问题持续，请联系管理员。",
      body: null,
    } satisfies Partial<ApiError>);
  });

  it("keeps non-JSON response correlation without displaying raw text", async () => {
    document.documentElement.lang = "en-US";
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          "upstream proxy failed at https://internal.example?token=secret",
          {
            status: 502,
            headers: {
              "content-type": "text/plain",
              "x-request-id": "fedcba9876543210fedcba9876543210",
            },
          },
        ),
    ) as typeof fetch;

    await expect(apiJson("/api/v1/settings")).rejects.toMatchObject({
      name: "ApiError",
      status: 502,
      requestId: "fedcba9876543210fedcba9876543210",
      message:
        "Something went wrong. Please try again. If the problem continues, contact your administrator.",
      diagnostics: undefined,
    } satisfies Partial<ApiError>);
  });
});
