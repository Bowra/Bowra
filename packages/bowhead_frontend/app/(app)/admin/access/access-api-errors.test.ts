/** Access control API failures: raw detail stays out of message; the page chooses a localized reason from the status code and known detail. */
import { afterEach, describe, expect, it, vi } from "vitest";

import { AdminAccessError, adminAccessErrorKey, createAccessGroup } from "@/lib/admin-access-api";

const REQUEST_ID = "0123456789abcdef0123456789abcdef";

function stubFailure(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "x-request-id": REQUEST_ID } })),
  );
}

async function failureOf(status: number, body: unknown): Promise<AdminAccessError> {
  stubFailure(status, body);
  const error = await createAccessGroup({ name: "财务组" }).catch((cause: unknown) => cause);
  expect(error).toBeInstanceOf(AdminAccessError);
  return error as AdminAccessError;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("admin access API errors", () => {
  it("keeps the raw detail out of the message but keeps status, reason and request id for diagnostics", async () => {
    const error = await failureOf(409, { detail: "group already exists" });

    expect(error.message).toBe("");
    expect(error.status).toBe(409);
    expect(error.reason).toBe("group already exists");
    expect(error.requestId).toBe(REQUEST_ID);
  });

  it.each([
    [409, { detail: "group already exists" }, "error_conflict"],
    [404, { detail: "user not found" }, "error_not_found_user"],
    [404, { detail: "group not found" }, "error_not_found_group"],
    [404, { detail: "group member not found" }, "error_not_found_member"],
    [422, { detail: { error: { code: "E_SKILL_APPROVAL_REQUIRED", message: "team skill not approved" } } }, "error_skill_approval_required"],
    [404, { detail: "workspace not found" }, "error_not_found_space"],
    [404, { detail: "kb file not found" }, "error_not_found"],
    [422, { detail: "invalid principal_type" }, "error_invalid"],
    [500, { detail: "boom" }, "action_failed"],
  ] as const)("maps %i %j to %s", async (status, body, key) => {
    expect(adminAccessErrorKey(await failureOf(status, body))).toBe(key);
  });

  it("falls back to the generic sentence for anything that is not an access API error", () => {
    expect(adminAccessErrorKey(new Error("group already exists"))).toBe("action_failed");
  });
});
