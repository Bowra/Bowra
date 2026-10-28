import { afterEach, describe, expect, it } from "vitest";

import {
  aiRewriteDraftKey,
  readAiRewriteDraft,
  resetAiRewriteDrafts,
  sectionIdAtResolvedPos,
  writeAiRewriteDraft,
} from "./ai-rewrite-drafts";

afterEach(() => resetAiRewriteDrafts());

describe("AI 改写草稿（按会话 + 章节）", () => {
  it("写入后再读仍在；清空后没有", () => {
    const key = aiRewriteDraftKey("s_1", "sec_a");
    writeAiRewriteDraft(key, "改得更简洁");
    expect(readAiRewriteDraft(key)).toBe("改得更简洁");
    writeAiRewriteDraft(key, "");
    expect(readAiRewriteDraft(key)).toBe("");
  });

  it("不同章节、不同会话互不影响", () => {
    writeAiRewriteDraft(aiRewriteDraftKey("s_1", "sec_a"), "A");
    expect(readAiRewriteDraft(aiRewriteDraftKey("s_1", "sec_b"))).toBe("");
    expect(readAiRewriteDraft(aiRewriteDraftKey("s_2", "sec_a"))).toBe("");
  });

  it("没有章节时不存（键为 null）", () => {
    expect(aiRewriteDraftKey("s_1", null)).toBeNull();
    writeAiRewriteDraft(null, "x");
    expect(readAiRewriteDraft(null)).toBe("");
  });

  it("从选区位置往上找章节 id", () => {
    const nodes = [
      { attrs: {}, type: { name: "doc" } },
      { attrs: { sectionId: "sec_n" }, type: { name: "section_narrative" } },
      { attrs: {}, type: { name: "paragraph" } },
    ];
    expect(sectionIdAtResolvedPos({ depth: 2, node: (d) => nodes[d] })).toBe("sec_n");
    expect(sectionIdAtResolvedPos({ depth: 0, node: (d) => nodes[d] })).toBeNull();
  });
});
