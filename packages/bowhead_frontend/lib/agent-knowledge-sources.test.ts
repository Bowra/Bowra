import { describe, expect, it } from "vitest";

import { readKnowledgeAccess, selectedKnowledgeBaseIds } from "./agent-knowledge-sources";

const BASE = "kbase_" + "a".repeat(24);

describe("readKnowledgeAccess", () => {
  it("reads selected knowledge bases only as schema 2 with their own field", () => {
    const policy = { schema_version: 2, mode: "selected_knowledge_bases", knowledge_base_ids: [BASE], defaults: { top_k: 3 } };
    expect(readKnowledgeAccess(policy)).toEqual(policy);
    expect(selectedKnowledgeBaseIds(readKnowledgeAccess(policy))).toEqual([BASE]);
    expect(selectedKnowledgeBaseIds(readKnowledgeAccess({ schema_version: 1, mode: "selected_files", file_ids: ["kbf_a"] }))).toEqual([]);
  });
  it.each([
    { schema_version: 1, mode: "selected_knowledge_bases", knowledge_base_ids: [BASE] },
    { schema_version: 2, mode: "selected_knowledge_bases" },
    { schema_version: 2, mode: "selected_knowledge_bases", knowledge_base_ids: [7] },
    { schema_version: 2, mode: "selected_knowledge_bases", knowledge_base_ids: [BASE], file_ids: [] },
    { schema_version: 2, mode: "selected_files", file_ids: [] },
    { schema_version: 1, mode: "context_readable", knowledge_base_ids: [] },
    { schema_version: 1, mode: "selected_files", file_ids: [], knowledge_base_ids: [BASE] },
  ])("keeps a policy it cannot honor unchanged: %o", (policy) => {
    expect(readKnowledgeAccess(policy)).toBe("unsupported");
  });
});
