import { afterEach, describe, expect, it, vi } from "vitest";

const apiJson = vi.hoisted(() => vi.fn());
vi.mock("./api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api-client")>()),
  apiJson,
}));

import { ApiError } from "./api-client";
import {
  accessRequestErrorCode,
  accessRequestRetryAfter,
  decideAccessRequest,
  getAccessRequestStatus,
  requestAccess,
} from "./access-requests-api";

function sentBody(): Record<string, unknown> {
  return JSON.parse(String(apiJson.mock.calls.at(-1)?.[1]?.body));
}

afterEach(() => apiJson.mockReset());

describe("access-requests-api", () => {
  it("sends only {action} when no options are given", async () => {
    await decideAccessRequest("acr_1", "approve");
    expect(sentBody()).toEqual({ action: "approve" });
  });

  it("sends the note and only the option that matches the action", async () => {
    await decideAccessRequest("acr_1", "approve", { note: "  ok  ", grantExpiresInDays: 30, stopRequests: true });
    expect(sentBody()).toEqual({ action: "approve", note: "ok", grant_expires_in_days: 30 });
    await decideAccessRequest("acr_1", "deny", { note: "", grantExpiresInDays: 30, stopRequests: true });
    expect(sentBody()).toEqual({ action: "deny", stop_requests: true });
  });

  it("omits the message for a report without a reason", async () => {
    await requestAccess("report", "s1");
    expect(sentBody()).toEqual({ kind: "report", resource_id: "s1" });
  });

  it("asks for one item's status by kind and id", async () => {
    await getAccessRequestStatus("team_subagent", "osa 1");
    expect(apiJson.mock.calls.at(-1)?.[0]).toBe("/api/v1/access-requests/status?kind=team_subagent&resource_id=osa+1");
  });

  it("reads the coded reason and retry_after from a 409 or 429", () => {
    const error = new ApiError(409, "x", { body: { detail: { code: "access_request_cooldown", retry_after: 600 } } });
    expect(accessRequestErrorCode(error)).toBe("access_request_cooldown");
    expect(accessRequestRetryAfter(error)).toBe(600);
    expect(accessRequestErrorCode(new Error("x"))).toBeNull();
  });
});
