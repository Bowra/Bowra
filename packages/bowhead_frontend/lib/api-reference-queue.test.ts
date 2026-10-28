import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cancelQueuedReferenceSubmission,
  fetchQueuedReferenceSubmissions,
} from "./api";

const originalFetch = globalThis.fetch;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  window.__BOWHEAD_BASE_PATH__ = undefined;
  vi.restoreAllMocks();
});

describe("reference follow-up queue API", () => {
  it("reads the ordered persistent projection without caching", async () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    let requested = "";
    let cache: RequestCache | undefined;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requested = String(input);
      cache = init?.cache;
      return jsonResponse(200, {
        items: [{
          client_submission_id: "a4cce43f-e75b-4ac8-b85e-168900aa5b5c",
          display_text: "@分析师 /杜邦分析 盈利能力",
          resources: [
            { kind: "agent", label: "分析师" },
            { kind: "skill", label: "杜邦分析" },
          ],
          attachment_count: 1,
          accepted_at: "2026-09-17T01:02:03+00:00",
        }],
      });
    }) as typeof fetch;

    await expect(fetchQueuedReferenceSubmissions("ws 1", "s/1"))
      .resolves.toHaveLength(1);
    expect(requested).toBe(
      "/bowhead/api/v1/chat/sessions/s%2F1/reference-queue?workspace_id=ws+1",
    );
    expect(cache).toBe("no-store");
  });

  it("rejects a response with an unknown resource kind", async () => {
    globalThis.fetch = vi.fn(async () => jsonResponse(200, {
      items: [{
        client_submission_id: "a4cce43f-e75b-4ac8-b85e-168900aa5b5c",
        display_text: "bad",
        resources: [{ kind: "write_tool", label: "unsafe" }],
        attachment_count: 0,
        accepted_at: "2026-09-17T01:02:03+00:00",
      }],
    })) as typeof fetch;

    await expect(fetchQueuedReferenceSubmissions("ws_1", "s_1"))
      .rejects.toThrow("reference queue resource is invalid");
  });

  it.each([
    ["client_submission_id", "not-a-uuid"],
    ["accepted_at", "2026-09-17"],
  ])("rejects an invalid %s", async (field, invalid) => {
    globalThis.fetch = vi.fn(async () => jsonResponse(200, {
      items: [{
        client_submission_id: "a4cce43f-e75b-4ac8-b85e-168900aa5b5c",
        display_text: "/杜邦分析 盈利能力",
        resources: [{ kind: "skill", label: "杜邦分析" }],
        attachment_count: 0,
        accepted_at: "2026-09-17T01:02:03+00:00",
        [field]: invalid,
      }],
    })) as typeof fetch;

    await expect(fetchQueuedReferenceSubmissions("ws_1", "s_1"))
      .rejects.toThrow("reference queue item is invalid");
  });

  it("cancels only the selected submission in its exact Session", async () => {
    let requested = "";
    let body: unknown;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requested = String(input);
      body = JSON.parse(String(init?.body));
      return jsonResponse(200, {
        client_submission_id: "a4cce43f-e75b-4ac8-b85e-168900aa5b5c",
        outcome: "cancelled",
      });
    }) as typeof fetch;

    await cancelQueuedReferenceSubmission(
      "ws_1",
      "s_1",
      "a4cce43f-e75b-4ac8-b85e-168900aa5b5c",
    );

    expect(requested).toBe(
      "/api/v1/chat/submissions/a4cce43f-e75b-4ac8-b85e-168900aa5b5c/cancel",
    );
    expect(body).toEqual({
      workspace_id: "ws_1",
      requested_session_id: "s_1",
    });
  });
});
