import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api-client";
import { fetchKbFiles, searchKb } from "@/lib/kb-api";

describe("fetchKbFiles errors", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces a nested gateway error code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: { error: { code: "workspace_required", workspace_id: "" } },
          }),
          { status: 400, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const error = await fetchKbFiles({ scope: "personal" }).catch((cause) => cause);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 400,
      code: "workspace_required",
    });
    expect((error as Error).message).not.toContain("workspace_required");
  });

  it("preserves a direct code for finite page mapping", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: {
              code: "kb_service_unavailable",
              message: "RAW_STORE_AND_DATABASE_DIAGNOSTIC",
            },
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const error = await fetchKbFiles({ scope: "personal" }).catch((cause) => cause);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 503,
      code: "kb_service_unavailable",
    });
  });
});

describe("knowledge search count policy", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([undefined, 30])("uses the Team default unless a count is explicitly requested (%s)", async (top_k) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ chunks: [], total_candidates: 0, rerank_used: false, latency_ms: 0 }), {
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    await searchKb("business risks", { top_k, scope_filter: ["space"] });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).toEqual({ query: "business risks", scope_filter: ["space"], ...(top_k !== undefined ? { top_k } : {}) });
  });
});
