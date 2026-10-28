import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchFeishuSetupSession } from "./feishu-integration-api";

const ORIGINAL_FETCH = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

function _resp(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("feishu-integration-api setup sessions", () => {
  it("fetchFeishuSetupSession reads the admin setup session by id", async () => {
    const spy = vi.fn(async () => _resp(200, {
      id: "fsu_123",
      state: "pending",
      app_type: "internal_guided",
      tenant_key: null,
      workspace_id: null,
      expires_at: "2026-06-05T12:00:00Z",
      created_at: "2026-06-05T11:00:00Z",
      updated_at: "2026-06-05T11:00:00Z",
      setup_url: "http://localhost/team/settings/integrations/feishu/?stage=02&setup=fsu_123",
      qr_payload: "http://localhost/team/settings/integrations/feishu/?stage=02&setup=fsu_123",
      qr_svg_url: "/api/v1/integrations/feishu/admin/setup-sessions/fsu_123/qr.svg",
      test_phrase: "Bowhead Feishu setup test fsu_123",
    }));
    globalThis.fetch = spy as typeof fetch;

    const data = await fetchFeishuSetupSession("fsu_123");

    expect(spy).toHaveBeenCalledWith(
      "/api/v1/integrations/feishu/admin/setup-sessions/fsu_123",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(data.id).toBe("fsu_123");
    expect(data.app_type).toBe("internal_guided");
  });
});
