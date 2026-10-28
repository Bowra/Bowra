/**
 * A send-init 402/403 denial must be visible instead of becoming a
 * silently-frozen/vanishing turn. The FinalFailBanner cannot render it because
 * there is no turnId, so the runner exposes both an immediate toast and a
 * durable sendErrorReason for the page-level strip.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { useTurnRunner } from "./use-turn-runner";
import { _clearAllToasts, useToasts } from "./use-toast";
import { _resetEntitlement, useEntitlement } from "./entitlement-store";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  _clearAllToasts();
  _resetEntitlement();
});
afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
  _clearAllToasts();
  _resetEntitlement();
});

function _resp(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("send-init entitlement denial surfacing", () => {
  it("a 402 on /chat/send pushes an error toast (denial is visible)", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return _resp(402, { detail: { error: { code: "over_grace", message: "license 已过期，请续期" } } });
      }
      return _resp(200, {});
    }) as typeof fetch;

    const toastsHook = renderHook(() => useToasts());
    const runnerHook = renderHook(() =>
      useTurnRunner({ mode: "sse", message: "hello", workspaceId: "w", memberUid: "m", sendTick: 1 }),
    );

    await waitFor(() => expect(toastsHook.result.current.length).toBeGreaterThan(0));
    expect(toastsHook.result.current[0].kind).toBe("error");
    expect(toastsHook.result.current[0].text).toContain("授权");  // entitlement_denied copy
    expect(runnerHook.result.current.sendErrorReason).toBe("over_grace");
  });

  it("a 402 over_grace on /chat/send also publishes the read-only banner state", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return _resp(402, { detail: { error: { code: "over_grace", message: "license 已过期，请续期" } } });
      }
      return _resp(200, {});
    }) as typeof fetch;

    const entHook = renderHook(() => useEntitlement());
    renderHook(() =>
      useTurnRunner({ mode: "sse", message: "hello", workspaceId: "w", memberUid: "m", sendTick: 1 }),
    );

    await waitFor(() => expect(entHook.result.current.health).toBe("over_grace"));
    expect(entHook.result.current.readOnly).toBe(true);
  });
});

/**
 * credit-billing P1-3 — exhausted credit and invalid licenses are independent mechanisms (chat.py's balance preflight runs
 * before the entitlement gate), but both return 402. Previously all used entitlement_denied's Authorization Denied copy, calling
 * credit exhaustion an authorization issue; this misdirected the entire 2026-07-23 dev diagnosis toward licensing.
 */
describe("send-init credit exhaustion is distinguished from an entitlement denial", () => {
  it("402 user_credit_exhausted → 额度文案, 不再说「授权」", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return _resp(402, {
          detail: { error: { code: "user_credit_exhausted", message: "本月额度用完, 联系 admin 升级 tier 或加配额" } },
        });
      }
      return _resp(200, {});
    }) as typeof fetch;

    const toastsHook = renderHook(() => useToasts());
    const runnerHook = renderHook(() =>
      useTurnRunner({ mode: "sse", message: "hello", workspaceId: "w", memberUid: "m", sendTick: 1 }),
    );

    await waitFor(() => expect(toastsHook.result.current.length).toBeGreaterThan(0));
    const text = toastsHook.result.current[0].text;
    expect(toastsHook.result.current[0].kind).toBe("error");
    expect(text).toContain("额度");
    expect(text).not.toContain("授权");
    expect(runnerHook.result.current.sendErrorReason).toBe("user_credit_exhausted");
  });

  it("402 team_pool_exhausted → 团队额度文案（与个人额度可区分）", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return _resp(402, {
          detail: { error: { code: "team_pool_exhausted", message: "公司本月总额度用完, 联系 admin" } },
        });
      }
      return _resp(200, {});
    }) as typeof fetch;

    const toastsHook = renderHook(() => useToasts());
    renderHook(() =>
      useTurnRunner({ mode: "sse", message: "hello", workspaceId: "w", memberUid: "m", sendTick: 1 }),
    );

    await waitFor(() => expect(toastsHook.result.current.length).toBeGreaterThan(0));
    const text = toastsHook.result.current[0].text;
    expect(text).toContain("团队");
    expect(text).not.toContain("授权");
  });

  it("credit 码不触发 license 只读 banner（额度耗尽不是授权失效）", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes("/chat/send")) {
        return _resp(402, { detail: { error: { code: "user_credit_exhausted", message: "本月额度用完" } } });
      }
      return _resp(200, {});
    }) as typeof fetch;

    const entHook = renderHook(() => useEntitlement());
    const toastsHook = renderHook(() => useToasts());
    renderHook(() =>
      useTurnRunner({ mode: "sse", message: "hello", workspaceId: "w", memberUid: "m", sendTick: 1 }),
    );

    await waitFor(() => expect(toastsHook.result.current.length).toBeGreaterThan(0));
    expect(entHook.result.current.readOnly).toBe(false);
  });
});
