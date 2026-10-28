import { describe, expect, it } from "vitest";

import { citationFromPayload, mergeCitation } from "./citation-payload";

describe("citationFromPayload", () => {
  it("读 citation_seq，保留 kind 全集与六态", () => {
    expect(citationFromPayload({ citation_seq: 2, seq: 99, artifact_id: "x", kind: "web_clip", access_state: "linked_missing" })).toEqual({
      seq: 2,
      artifactId: "x",
      label: null,
      kind: "web_clip",
      accessState: "linked_missing",
      safeAnchorId: undefined,
    });
  });

  it("未知 kind 退回 data_ref；缺 seq 或 artifact_id 丢弃", () => {
    expect(citationFromPayload({ citation_seq: 1, artifact_id: "x", kind: "new_kind" })?.kind).toBe("data_ref");
    expect(citationFromPayload({ artifact_id: "x" })).toBeNull();
    expect(citationFromPayload({ citation_seq: 1 })).toBeNull();
  });

  it("kb_chunk 可查看时带知识库定位；缺文件或片段 id 时整组不要", () => {
    const full = citationFromPayload({
      citation_seq: 1,
      artifact_id: "source-ref-v1:abc",
      label: "门店日销售.csv",
      kind: "kb_chunk",
      access_state: "linked_accessible",
      kb_file_id: "kbf_1",
      kb_chunk_id: "chunk_a",
      kb_scope: "space",
      kb_space_id: "ws_1",
    });
    expect(full?.kb).toEqual({ fileId: "kbf_1", chunkId: "chunk_a", scope: "space", spaceId: "ws_1" });
    expect(citationFromPayload({ citation_seq: 1, artifact_id: "a", kind: "kb_chunk", kb_file_id: "kbf_1" })?.kb).toBeUndefined();
  });
});


it("keeps a refined source warning on replay without remapping the citation", () => {
  const payload = { citation_seq: 1, artifact_id: "kb:kbf_1:chunk_a", kind: "kb_chunk", kb_file_id: "kbf_1", kb_chunk_id: "chunk_a", kb_generation_id: "kbg_1" };
  const first = citationFromPayload({ ...payload, kb_source_check_state: "verified_binding" })!;
  const warning = citationFromPayload({ ...payload, kb_source_check_state: "source_temporarily_unavailable" })!;
  const merged = mergeCitation(first, warning);
  expect(merged.kb?.sourceCheckState).toBe("source_temporarily_unavailable");
  expect(mergeCitation(merged, warning)).toBe(merged);
  expect(mergeCitation(merged, { ...warning, artifactId: "another-file" })).toBe(merged);
});
