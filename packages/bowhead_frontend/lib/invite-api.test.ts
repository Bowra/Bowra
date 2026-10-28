import { afterEach, describe, expect, it, vi } from "vitest";

import { InviteApiError, exchangeInviteIntent } from "./invite-api";

const ORIGINAL_FETCH = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  document.documentElement.lang = "";
  vi.restoreAllMocks();
});

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("invite API errors", () => {
  it("retains a terminal code without exposing it as message", async () => {
    document.documentElement.lang = "en-US";
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(410, {
        detail: {
          code: "invite_expired",
        },
      }),
    ) as typeof fetch;

    let caught: unknown;
    try {
      await exchangeInviteIntent("token");
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(InviteApiError);
    expect(caught).toMatchObject({
      status: 410,
      code: "invite_expired",
      message:
        "Something went wrong. Please try again. If the problem continues, contact your administrator.",
    });
  });

  it("uses a localized backend message when supplied", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse(409, {
        detail: {
          code: "policy_blocked",
          message: "当前团队策略不允许接受邀请。",
        },
      }),
    ) as typeof fetch;

    await expect(exchangeInviteIntent("token")).rejects.toMatchObject({
      code: "policy_blocked",
      message: "当前团队策略不允许接受邀请。",
    });
  });
});
