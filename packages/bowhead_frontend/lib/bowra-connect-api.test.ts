// @vitest-environment node
import { beforeEach, expect, it, vi } from "vitest";

import { completeBowraConnection } from "./bowra-connect-api";
import { fetchWithRefresh } from "./auth-api";
import { ApiError } from "./api-client";

vi.mock("./auth-api", () => ({ fetchWithRefresh: vi.fn() }));

beforeEach(() => vi.mocked(fetchWithRefresh).mockReset());

it("completes through the shared session-refresh client without supplying an exchange target or verifier", async () => {
  vi.mocked(fetchWithRefresh).mockResolvedValue(Response.json({ bowra_import: "one-use-import" }));
  const result = await completeBowraConnection({ state: "one-use", code: "temporary-code" });
  expect(result).toEqual({ bowra_import: "one-use-import" });
  expect(fetchWithRefresh).toHaveBeenCalledExactlyOnceWith("/api/v1/admin/llm-config/bowra/complete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state: "one-use", code: "temporary-code" }),
  });
});

it("returns the failed-session error to the callback instead of navigating through login or retrying an exchange", async () => {
  vi.mocked(fetchWithRefresh).mockResolvedValue(Response.json({ detail: "unauthorized" }, { status: 401 }));
  await expect(completeBowraConnection({ state: "one-use", code: "temporary-code" })).rejects.toBeInstanceOf(ApiError);
  expect(fetchWithRefresh).toHaveBeenCalledTimes(1);
});
