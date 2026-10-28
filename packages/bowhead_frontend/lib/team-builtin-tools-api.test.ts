import { afterEach, describe, expect, it, vi } from "vitest";

import { configureTavily, createDatarcTool } from "@/lib/team-builtin-tools-api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("team built-in tools API errors", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("carries the backend code for the {detail:{code}} shape", async () => {
    // Actual shape emitted by Gateway error_handler (observed with register_error_handlers on 2026-09-24).
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(422, { detail: { code: "datarc_connection_key_invalid" } })));
    await expect(createDatarcTool({ name: "n", endpoint: "https://demo.datarc.cn", connection_key: "k" })).rejects.toMatchObject({
      status: 422,
      code: "datarc_connection_key_invalid",
    });
  });

  it("keeps the legacy string-detail code reachable through the message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(422, { detail: "tavily_api_key_invalid" })));
    await expect(configureTavily("tvly-x", true)).rejects.toMatchObject({ status: 422, message: "tavily_api_key_invalid" });
  });
});
