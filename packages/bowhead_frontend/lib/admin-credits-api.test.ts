/**
 * Licensing §9 — after retiring the teams.py routes, the admin credits tier-defaults UI must hit the
 * caller-team-bound admin_billing twins: GET /api/v1/admin/credits (read) + POST /api/v1/admin/team/tier-credits
 * (write, with a reason), NOT the old PUT /api/v1/teams/{id}/tier-credits.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AdminCreditsError,
  fetchTeamCredits,
  updateTeamCreditQuota,
  updateTierCredits,
} from "./admin-credits-api";

const ORIGINAL_FETCH = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

function _resp(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("admin-credits-api tier-credits (teams route retired → admin_billing twins)", () => {
  it("fetchTeamCredits reads GET /api/v1/admin/credits (caller-team-bound)", async () => {
    const spy = vi.fn(async () => _resp(200, { tier_credits: { pro: 5000, ultra: 30000 } }));
    globalThis.fetch = spy as typeof fetch;
    const data = await fetchTeamCredits();
    expect(spy).toHaveBeenCalledWith("/api/v1/admin/credits", expect.objectContaining({ credentials: "include" }));
    expect(data.tier_credits.pro).toBe(5000);
  });

  it("updateTierCredits POSTs the twin with {tier_credits, reason} (no team_id path param)", async () => {
    let calledUrl = "";
    let calledBody: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      calledUrl = String(url);
      calledBody = JSON.parse(String(init?.body ?? "{}"));
      return _resp(200, { ok: true, old_tier_credits: {}, new_tier_credits: { pro: 7000, ultra: 30000 } });
    }) as typeof fetch;

    const out = await updateTierCredits({ pro: 7000, ultra: 30000 });
    expect(calledUrl).toBe("/api/v1/admin/team/tier-credits");
    expect(calledBody.tier_credits).toEqual({ pro: 7000, ultra: 30000 });
    expect(typeof calledBody.reason).toBe("string");
    expect((calledBody.reason as string).length).toBeGreaterThanOrEqual(10); // twin requires reason ≥10
    expect(out.new_tier_credits.pro).toBe(7000);
    expect(calledUrl).not.toContain("/api/v1/teams/"); // legacy route is gone
  });

  it("updateTeamCreditQuota PUTs the caller-team-bound quota endpoint", async () => {
    let calledUrl = "";
    let calledMethod = "";
    let calledBody: Record<string, unknown> = {};
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      calledUrl = String(url);
      calledMethod = String(init?.method);
      calledBody = JSON.parse(String(init?.body ?? "{}"));
      return _resp(200, { ok: true, old_quota: 10_000, new_quota: 20_000 });
    }) as typeof fetch;

    await updateTeamCreditQuota({ new_quota: 20_000, reason: "Quarterly capacity increase" });
    expect(calledUrl).toBe("/api/v1/admin/credits/quota");
    expect(calledMethod).toBe("PUT");
    expect(calledBody).toEqual({
      new_quota: 20_000,
      reason: "Quarterly capacity increase",
    });
  });

  it("surfaces structured FastAPI errors while preserving retry-after", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify({
        detail: {
          code: "credits_cooldown",
          message: "Please wait before retrying",
        },
      }), {
        status: 429,
        headers: {
          "content-type": "application/json",
          "retry-after": "12",
        },
      }),
    ) as typeof fetch;

    let error: unknown;
    try {
      await fetchTeamCredits();
    } catch (caught) {
      error = caught;
    }

    expect(error).toBeInstanceOf(AdminCreditsError);
    expect((error as AdminCreditsError).status).toBe(429);
    expect((error as AdminCreditsError).retryAfter).toBe(12);
    expect((error as AdminCreditsError).code).toBe("credits_cooldown");
    expect((error as AdminCreditsError).message).toBe("Please wait before retrying");
  });
});
