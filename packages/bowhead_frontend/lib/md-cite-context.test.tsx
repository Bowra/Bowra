/**
 * Body text preceding an inline citation (knowledge base source cards use it to locate and highlight original text). Adjacent citations reference the same sentence;
 * the second must not receive an empty string.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "@/lib/md";

function contexts(markdown: string): Record<string, string | undefined> {
  const seen: Record<string, string | undefined> = {};
  render(
    <Markdown
      markdown={markdown}
      renderArtifactChip={(id, context) => {
        seen[id] = context;
        return <span>{id}</span>;
      }}
    />,
  );
  return seen;
}

describe("rehypeArtifactChips · 角标前正文", () => {
  it("相邻角标（中间没有正文或只隔空白）沿用同一句正文", () => {
    expect(contexts("华东区毛利率为 32%[cite:1][cite:2]。")).toEqual({
      "cite:1": "华东区毛利率为 32%",
      "cite:2": "华东区毛利率为 32%",
    });
    expect(contexts("付款周期为 45 天[cite:1] [cite:3]。")["cite:3"]).toBe("付款周期为 45 天");
  });

  it("中间有正文时各取各的（对照）", () => {
    expect(contexts("华东区毛利率为 32%[cite:1]，华南区为 28%[cite:2]。")).toEqual({
      "cite:1": "华东区毛利率为 32%",
      "cite:2": "，华南区为 28%",
    });
  });
});
