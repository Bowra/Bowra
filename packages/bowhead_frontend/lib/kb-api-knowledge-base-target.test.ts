// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import { promoteFileToKb } from "./api";
import { importKbFromGoogleDrive, searchKb } from "./kb-api";

function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sentBody(fetchMock: ReturnType<typeof stubFetch>): Record<string, unknown> {
  return JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
}

describe("knowledge base targets on existing KB requests", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("deep search sends a knowledge base only when one is chosen (today's body otherwise)", async () => {
    let fetchMock = stubFetch({ chunks: [], total_candidates: 0, rerank_used: false, latency_ms: 1 });
    await searchKb("refund", { knowledge_base_id: "kbase_1" });
    expect(sentBody(fetchMock)).toEqual({ query: "refund", knowledge_base_id: "kbase_1" });

    vi.unstubAllGlobals();
    fetchMock = stubFetch({ chunks: [], total_candidates: 0, rerank_used: false, latency_ms: 1 });
    await searchKb("refund", { scope_filter: ["space"] });
    expect(sentBody(fetchMock)).toEqual({ query: "refund", scope_filter: ["space"] });
  });

  it("Google Drive import names the target knowledge base", async () => {
    const fetchMock = stubFetch({ results: [] });
    await importKbFromGoogleDrive(["g_1"], { scope: "team", scope_id: "t_1", knowledge_base_id: "kbase_team" });
    expect(sentBody(fetchMock)).toEqual({ file_ids: ["g_1"], scope: "team", scope_id: "t_1", use_llm: false, knowledge_base_id: "kbase_team" });
  });

  it("Add from Files sends the target only when given", async () => {
    let fetchMock = stubFetch({ file_id: "kbf_1" });
    await promoteFileToKb("art_1", "space", "kbase_manuals");
    expect(sentBody(fetchMock)).toMatchObject({ artifact_id: "art_1", scope: "space", knowledge_base_id: "kbase_manuals" });

    vi.unstubAllGlobals();
    fetchMock = stubFetch({ file_id: "kbf_1" });
    await promoteFileToKb("art_1", "personal");
    expect(sentBody(fetchMock)).not.toHaveProperty("knowledge_base_id");
  });

  it("Add from Files keeps knowledge base codes instead of the status fallbacks", async () => {
    stubFetch({ error: { code: "knowledge_base_scope_mismatch", message: "x" } }, 422);
    await expect(promoteFileToKb("art_1", "space", "kbase_other")).rejects.toThrow("knowledge_base_scope_mismatch");
    vi.unstubAllGlobals();
    stubFetch({ error: { code: "knowledge_source_in_trash", message: "x" } }, 409);
    await expect(promoteFileToKb("art_1", "space", "kbase_manuals")).rejects.toThrow("knowledge_source_in_trash");
    vi.unstubAllGlobals();
    // Today's mapping is unchanged for every other code.
    stubFetch({ error: { code: "kb_unsupported_type", message: "x" } }, 422);
    await expect(promoteFileToKb("art_1", "personal")).rejects.toThrow("unsupported");
    vi.unstubAllGlobals();
    stubFetch({ error: { code: "kb_embedding_not_configured", message: "x" } }, 503);
    await expect(promoteFileToKb("art_1", "personal")).rejects.toThrow("kb_embedding_not_configured");
  });
});
