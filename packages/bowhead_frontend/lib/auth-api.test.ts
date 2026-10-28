import { PRODUCT_NAME } from "@/lib/product-identity";
/**
 * §C (real-auth) — auth-api contract for email-verification enforcement (Task #8 FE).
 *
 * Covers the new structured-detail parsing (403 email_not_verified → AuthError.reasonCode),
 * the requires_verification register shape, and the public resend endpoint.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AuthError,
  fetchMe,
  invalidateMeCache,
  localActivationRequired,
  loginUser,
  oidcLoginRedirect,
  refreshAccessToken,
  registerUser,
  resendVerificationEmail,
  resendCurrentUserVerificationEmail,
  setLastTeam,
} from "./auth-api";
import {
  beginEffectiveThemeRequest,
  resetEffectiveThemeRequestGuardForTests,
} from "./effective-theme-request-guard";

const ORIGINAL_FETCH = globalThis.fetch;
afterEach(() => {
  invalidateMeCache();
  resetEffectiveThemeRequestGuardForTests();
  globalThis.fetch = ORIGINAL_FETCH;
  document.documentElement.lang = "";
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

function _textResp(
  status: number,
  body: string,
  headers: Record<string, string> = {},
): Response {
  return new Response(body, {
    status,
    headers: { "content-type": "text/plain", ...headers },
  });
}

describe("auth-api email-verify", () => {
  it("only a pristine local deployment requires first-run activation", () => {
    expect(localActivationRequired({
      required: true,
      deployment_mode: "local",
    })).toBe(true);
    expect(localActivationRequired({
      required: false,
      deployment_mode: "local",
    })).toBe(false);
    expect(localActivationRequired({
      required: true,
      deployment_mode: "saas",
    })).toBe(false);
    expect(localActivationRequired(null)).toBe(false);
  });

  it("loginUser throws AuthError with reasonCode on 403 email_not_verified", async () => {
    document.documentElement.lang = "en-US";
    globalThis.fetch = vi.fn(async () =>
      _resp(403, { detail: { code: "email_not_verified", email: "u@x.com" } }),
    ) as typeof fetch;
    await expect(loginUser("u@x.com", "pw")).rejects.toMatchObject({
      status: 403,
      reasonCode: "email_not_verified",
      message:
        "Something went wrong. Please try again. If the problem continues, contact your administrator.",
    });
  });

  it("loginUser uses detail.message for the AuthError message when present", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(403, { detail: { code: "email_not_verified", message: "verify first" } }),
    ) as typeof fetch;
    await expect(loginUser("u@x.com", "pw")).rejects.toThrowError("verify first");
  });

  it("loginUser separates legacy invalid-credentials detail from the user message", async () => {
    document.documentElement.lang = "en-US";
    globalThis.fetch = vi.fn(async () => _resp(401, { detail: "invalid credentials" })) as typeof fetch;
    let caught: unknown;
    try {
      await loginUser("u@x.com", "pw");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(AuthError);
    expect((caught as AuthError).status).toBe(401);
    expect((caught as AuthError).message).toBe(
      "Something went wrong. Please try again. If the problem continues, contact your administrator.",
    );
    expect((caught as AuthError).reasonCode).toBe("invalid_credentials");
    expect((caught as AuthError).technicalMessage).toBe("invalid credentials");
  });

  it("loginUser preserves a published 421 diagnostic behind a stable reason code", async () => {
    document.documentElement.lang = "en-US";
    globalThis.fetch = vi.fn(async () =>
      _textResp(
        421,
        "browser public URL is not registered",
        { "x-request-id": "req_public_entry_1" },
      ),
    ) as typeof fetch;

    await expect(loginUser("u@x.com", "pw")).rejects.toMatchObject({
      status: 421,
      reasonCode: "public_entry_not_registered",
      technicalMessage: "browser public URL is not registered",
      requestId: "req_public_entry_1",
      message:
        "Something went wrong. Please try again. If the problem continues, contact your administrator.",
    });
  });

  it("loginUser validates versioned PublicEntry diagnostics and merges the request ID", async () => {
    document.documentElement.lang = "en-US";
    globalThis.fetch = vi.fn(async () =>
      _resp(
        421,
        {
          detail: {
            code: "public_entry_not_registered",
            message: "This public entry is not registered.",
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

    await expect(loginUser("u@x.com", "pw")).rejects.toMatchObject({
      status: 421,
      reasonCode: "public_entry_not_registered",
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
    });
  });

  it("does not publish an unknown fetch exception as a transport diagnosis", async () => {
    const failure = new TypeError("Cannot read private configuration");
    globalThis.fetch = vi.fn().mockRejectedValue(failure);
    await expect(loginUser("u@x.com", "pw")).rejects.toBe(failure);
  });

  it("projects a fetch rejection without exposing the browser error message", async () => {
    document.documentElement.lang = "en-US";
    globalThis.fetch = vi.fn(async () => {
      throw new TypeError(
        "Failed to fetch",
      );
    }) as typeof fetch;

    let caught: unknown;
    try {
      await loginUser("u@x.com", "pw");
    } catch (error) {
      caught = error;
    }

    expect(caught).toMatchObject({
      name: "AuthTransportError",
      reasonCode: "service_unreachable",
      message:
        `${PRODUCT_NAME} could not be reached. Check your connection and try again.`,
      diagnostics: {
        schemaVersion: 1,
        disclosure: "public-neutral",
        code: "service_unreachable",
        safeDetail: "phase=receive",
        retryable: true,
        serverRequestIdUnavailable: true,
      },
    });
    expect(JSON.stringify(caught)).not.toContain("internal.example");
    expect(JSON.stringify(caught)).not.toContain("secret");
  });

  it("loginUser does not publish arbitrary plain-text or HTML diagnostics", async () => {
    globalThis.fetch = vi.fn(async () =>
      _textResp(500, "<html>upstream stack and secret</html>"),
    ) as typeof fetch;

    let caught: unknown;
    try {
      await loginUser("u@x.com", "pw");
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(AuthError);
    expect((caught as AuthError).technicalMessage).toBeUndefined();
    expect((caught as AuthError).message).not.toContain("secret");
  });

  it("registerUser surfaces requires_verification (no token) on 200", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(200, {
        requires_verification: true,
        user: { id: "u", email: "u@x.com", tier: "ultra", email_verified: false },
      }),
    ) as typeof fetch;
    const resp = await registerUser({ email: "u@x.com", password: "pw" });
    expect(resp.requires_verification).toBe(true);
    expect(resp.token).toBeUndefined();
  });

  it("registerUser returns token on auto-login (flag off)", async () => {
    globalThis.fetch = vi.fn(async () =>
      _resp(200, { token: "jwt", user: { id: "u", email: "u@x.com", tier: "ultra" } }),
    ) as typeof fetch;
    const resp = await registerUser({ email: "u@x.com", password: "pw" });
    expect(resp.token).toBe("jwt");
    expect(resp.requires_verification).toBeUndefined();
  });

  it("signed-in resend uses the account endpoint and reports rate limits", async () => {
    const spy = vi.fn(async () => _resp(200, { ok: true }));
    globalThis.fetch = spy as typeof fetch;
    expect(await resendCurrentUserVerificationEmail()).toEqual({ ok: true });
    expect(spy).toHaveBeenCalledWith("/api/v1/auth/resend-verify", expect.objectContaining({ method: "POST", credentials: "include" }));
    spy.mockImplementationOnce(async () => _resp(429, { detail: { code: "resend_too_frequent", wait_seconds: 60 } }));
    await expect(resendCurrentUserVerificationEmail()).rejects.toMatchObject({ status: 429, reasonCode: "resend_too_frequent" });
  });

  it("resendVerificationEmail POSTs the email to the public endpoint", async () => {
    const spy = vi.fn(async () => _resp(200, { ok: true }));
    globalThis.fetch = spy as typeof fetch;
    const out = await resendVerificationEmail("U@X.com");
    expect(out).toEqual({ ok: true });
    expect(spy).toHaveBeenCalledWith(
      "/api/v1/auth/resend-verify-public",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({ email: "U@X.com" }),
      }),
    );
  });

  it("fetchMe backfills credit fields for legacy /auth/me payloads", async () => {
    let requestHeaders: HeadersInit | undefined;
    globalThis.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      requestHeaders = init?.headers;
      return _resp(200, {
        id: "u_1",
        email: "u@x.com",
        display_name: null,
        avatar_url: null,
        team_id: "team_1",
        role: "member",
        status: "active",
        email_verified: true,
        locale: "zh-CN",
        timezone: "Asia/Shanghai",
        is_team_admin: false,
        workspace_count: 1,
        needs_onboarding: false,
      });
    },
    ) as typeof fetch;
    const me = await fetchMe();
    expect(me.tier).toBe("pro");
    expect(me.credit_balance).toBe(0);
    expect(me.credit_quota).toBe(10_000);
    expect(new Headers(requestHeaders).get(
      "X-Bowhead-Terminal-Protocol-Capability",
    )).toBe("1");
  });

  it("deduplicates concurrent /me reads and briefly reuses the settled identity", async () => {
    const spy = vi.fn(async () => _resp(200, {
      id: "u_1", email: "u@x.com", team_id: "team_1", tier: "ultra",
      credit_balance: 100, credit_quota: 30_000,
    }));
    globalThis.fetch = spy as typeof fetch;

    const [first, second] = await Promise.all([fetchMe(), fetchMe()]);
    const third = await fetchMe();

    expect(first.id).toBe("u_1");
    expect(second).toBe(first);
    expect(third).toBe(first);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("drops identity cached while a team switch is still in flight", async () => {
    let finishTeamSwitch!: () => void;
    let teamSwitchFinished = false;
    let meCalls = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/auth/me/last-team")) {
        return new Promise<Response>((resolve) => {
          finishTeamSwitch = () => {
            teamSwitchFinished = true;
            resolve(_resp(200, { ok: "ok", team_id: "team_2" }));
          };
        });
      }
      meCalls += 1;
      return _resp(200, {
        id: "u_1", email: "u@x.com",
        team_id: teamSwitchFinished ? "team_2" : "team_1",
        tier: "ultra", credit_balance: 100, credit_quota: 30_000,
      });
    }) as typeof fetch;

    const staleThemeRequest = beginEffectiveThemeRequest();
    const switching = setLastTeam("team_2");
    expect(staleThemeRequest.signal.aborted).toBe(true);
    expect((await fetchMe()).team_id).toBe("team_1");
    finishTeamSwitch();
    await switching;

    expect((await fetchMe()).team_id).toBe("team_2");
    expect(meCalls).toBe(2);
  });

  it("invalidates target-team theme requests when a team switch fails", async () => {
    let rejectTeamSwitch!: (reason: Error) => void;
    globalThis.fetch = vi.fn(
      () =>
        new Promise<Response>((_resolve, reject) => {
          rejectTeamSwitch = reject;
        }),
    ) as typeof fetch;

    const switching = setLastTeam("team_2");
    const targetThemeRequest = beginEffectiveThemeRequest("team_2");
    rejectTeamSwitch(new TypeError("Failed to fetch"));

    await expect(switching).rejects.toMatchObject({
      name: "AuthTransportError",
      reasonCode: "service_unreachable",
    });
    expect(targetThemeRequest.signal.aborted).toBe(true);
    targetThemeRequest.complete();
  });

  it("does not cache an in-flight /me response after invalidation", async () => {
    let resolveStaleRequest!: (response: Response) => void;
    let meCalls = 0;
    globalThis.fetch = vi.fn(async () => {
      meCalls += 1;
      if (meCalls === 1) {
        return new Promise<Response>((resolve) => {
          resolveStaleRequest = resolve;
        });
      }
      return _resp(200, {
        id: "u_1", email: "u@x.com", team_id: "team_2", tier: "ultra",
        credit_balance: 100, credit_quota: 30_000,
      });
    }) as typeof fetch;

    const staleRequest = fetchMe();
    invalidateMeCache();
    resolveStaleRequest(_resp(200, {
      id: "u_1", email: "u@x.com", team_id: "team_1", tier: "ultra",
      credit_balance: 100, credit_quota: 30_000,
    }));

    expect((await staleRequest).team_id).toBe("team_1");
    expect((await fetchMe()).team_id).toBe("team_2");
    expect(meCalls).toBe(2);
  });

  // followup-context-deck #3 — expired 1h access token + surviving 30d refresh cookie → automatic renewal.
  it("fetchMe refreshes on 401 then retries /me (no bounce to /login)", async () => {
    let meCalls = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/auth/refresh")) {
        return _resp(200, { ok: true, token: "new", user: {} });
      }
      meCalls += 1;
      if (meCalls === 1) return _resp(401, { detail: "auth required" });
      return _resp(200, {
        id: "u_1", email: "u@x.com", team_id: "team_1", tier: "ultra",
        credit_balance: 100, credit_quota: 30_000,
      });
    }) as typeof fetch;
    const me = await fetchMe();
    expect(me.id).toBe("u_1");
    expect(meCalls).toBe(2); // Initial 401 + retry after refresh.
  });

  // review-r2 (CRITICAL): backend /refresh uses one-time rotation; a second concurrent request with the same cookie triggers
  // revoke_all (logout on all devices). Concurrent mount-time fetchMe calls must share the entire /me → /refresh → /me chain.
  it("concurrent fetchMe calls share the complete refresh chain", async () => {
    let refreshCalls = 0;
    let meCalls = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/auth/refresh")) {
        refreshCalls += 1;
        await new Promise((r) => setTimeout(r, 5));
        return _resp(200, { ok: true, token: "new", user: {} });
      }
      meCalls += 1;
      if (meCalls === 1) return _resp(401, { detail: "auth required" });
      return _resp(200, {
        id: "u_1", email: "u@x.com", team_id: "team_1", tier: "ultra",
        credit_balance: 100, credit_quota: 30_000,
      });
    }) as typeof fetch;
    const [a, b] = await Promise.all([fetchMe(), fetchMe()]);
    expect(a.id).toBe("u_1");
    expect(b.id).toBe("u_1");
    expect(refreshCalls).toBe(1); // Concurrent calls share one rotation, avoiding backend replay defense.
    expect(meCalls).toBe(2); // Initial 401 + one retry after refresh, shared across components.
  });

  it("fetchMe throws 401 when refresh also fails (truly unauthenticated → /login)", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      // /me always returns 401, as does /refresh (missing/expired refresh cookie).
      void String(input);
      return _resp(401, { detail: "auth required" });
    }) as typeof fetch;
    await expect(fetchMe()).rejects.toMatchObject({ status: 401 });
  });

  // B13: another tab rotated the shared refresh cookie 40 ms earlier. The
  // superseded 401 must lead to a retry with the renewed cookie, never to the
  // /logout cleanup that would revoke the successor every other tab uses.
  it("retries a superseded refresh instead of logging every tab out", async () => {
    const calls: string[] = [];
    let refreshCalls = 0;
    let meCalls = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/v1/auth/refresh")) {
        refreshCalls += 1;
        if (refreshCalls === 1) {
          return _resp(401, {
            detail: { code: "refresh_superseded", message: "Renewed by another tab." },
          });
        }
        return _resp(200, { ok: true, token: "new", user: {} });
      }
      if (url.includes("/api/v1/auth/logout")) return _resp(200, { ok: true });
      meCalls += 1;
      if (meCalls === 1) return _resp(401, { detail: "auth required" });
      return _resp(200, {
        id: "u_1", email: "u@x.com", team_id: "team_1", tier: "ultra",
        credit_balance: 100, credit_quota: 30_000,
      });
    }) as typeof fetch;

    const me = await fetchMe();

    expect(me.id).toBe("u_1");
    expect(refreshCalls).toBe(2);
    expect(calls.some((url) => url.includes("/api/v1/auth/logout"))).toBe(false);
  });

  it("serializes refreshes across tabs through the Web Locks API", async () => {
    const lockNames: string[] = [];
    const request = vi.fn(async (name: string, task: () => Promise<unknown>) => {
      lockNames.push(name);
      return task();
    });
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: { request },
    });
    try {
      globalThis.fetch = vi.fn(async () =>
        _resp(200, { ok: true, token: "new", user: {} }),
      ) as typeof fetch;
      await refreshAccessToken();
      expect(lockNames).toEqual(["bowhead-auth-refresh"]);
    } finally {
      delete (navigator as { locks?: unknown }).locks;
    }
  });

  it("does not retry a refresh rejected for a revoked session", async () => {
    let refreshCalls = 0;
    globalThis.fetch = vi.fn(async () => {
      refreshCalls += 1;
      return _resp(401, { detail: "refresh token invalid / revoked / expired" });
    }) as typeof fetch;
    await expect(refreshAccessToken()).rejects.toMatchObject({ status: 401 });
    expect(refreshCalls).toBe(1);
  });

  // followup-context-deck #11 — stalled /me must make fetchMe reject on timeout, rather than leave the loading spinner running forever.
  it("fetchMe rejects on timeout instead of hanging when /me stalls", async () => {
    vi.useFakeTimers();
    // fetch never resolves but responds to abort signal (simulate /me stalled behind proxy).
    globalThis.fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")));
      }),
    ) as typeof fetch;
    try {
      const p = fetchMe();
      const assertion = expect(p).rejects.toMatchObject({
        name: "AuthTransportError",
        reasonCode: "request_timed_out",
        message: "等待响应超时，操作结果尚未确认。请先核对当前状态，再决定是否重试。",
        diagnostics: {
          code: "request_timed_out",
          safeDetail: "phase=receive",
          serverRequestIdUnavailable: true,
        },
      });
      await vi.advanceTimersByTimeAsync(21_000);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the /me timeout active while the response body stalls", async () => {
    vi.useFakeTimers();
    globalThis.fetch = vi.fn(async (_input, init) => new Response(
      new ReadableStream({
        start(controller) {
          init?.signal?.addEventListener("abort", () => {
            controller.error(init.signal?.reason);
          }, { once: true });
        },
      }),
      { status: 200 },
    )) as typeof fetch;
    try {
      const pending = fetchMe();
      const assertion = expect(pending).rejects.toMatchObject({
        name: "AuthTransportError",
        reasonCode: "request_timed_out",
      });
      await vi.advanceTimersByTimeAsync(20_000);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });
});

 it("carries the browser entry when initiating OIDC before full navigation", async () => {
  const originalContext = window.__BOWHEAD_PUBLIC_CONTEXT__;
  window.__BOWHEAD_PUBLIC_CONTEXT__ = { entryId: null, teamId: null, baseUrl: window.location.origin + "/bowhead", basePath: "/bowhead", matchSource: "browser_hint" };
  const fetchMock = vi.fn().mockResolvedValue(_resp(200, { redirect_url: "https://identity.example/authorize" }));
  globalThis.fetch = fetchMock;
  try {
    expect((await oidcLoginRedirect("/reports/")).redirect_url).toBe("https://identity.example/authorize");
    expect(fetchMock.mock.calls[0][0]).toBe("/bowhead/api/v1/auth/oidc/start?format=json&next=%2Freports%2F");
    const headers = new Headers(fetchMock.mock.calls[0][1].headers);
    expect(headers.get("X-Bowhead-Public-Base")).toBe(window.location.origin + "/bowhead");
  } finally { window.__BOWHEAD_PUBLIC_CONTEXT__ = originalContext; }
 });
