import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchKbFileChunk } from "./kb-api";

function respond(status: number, body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })));
}

describe("fetchKbFileChunk", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("片段已不在（kb_chunk_not_found）⇒ null，来源卡显示「原文已更新」", async () => {
    respond(404, { detail: { error: { code: "kb_chunk_not_found" } } });
    await expect(fetchKbFileChunk("kbf_1", "chunk_gone")).resolves.toBeNull();
  });

  it("文件本身不可读的 404 照旧抛出（读取失败）", async () => {
    respond(404, { detail: { error: { code: "kb_file_not_found" } } });
    await expect(fetchKbFileChunk("kbf_1", "chunk_a")).rejects.toBeTruthy();
  });

  it("命中时按 id 取出那一段", async () => {
    respond(200, { chunks: [{ chunk_id: "chunk_a", content: "原文", page: null, section_title: null, role: "intro", chunk_index: 1 }], total: 1 });
    await expect(fetchKbFileChunk("kbf_1", "chunk_a")).resolves.toMatchObject({ chunk_id: "chunk_a", content: "原文" });
  });
});
