/**
 * Strip provenance tags in **both branches** of chat rendering.
 *
 * Lesson from 116, 2026-08-26: the first version stripped in `stripInlineNumMarkers`, but Lead prose
 * (`LeadNarrative`) passed `renderArtifactChip` and used **the other branch** (a separate
 * `stripLegacy` path), leaving the leaking path untested. The same mistake happened twice: first wiring the wrong file
 * (`formatInlineTokens`), then the wrong branch. Therefore write this gate by **branch**, not by function.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "./md";

const TAGGED = "上面是通用财务概念（[llm-std:]）。营收 12 亿 [bi:art_001] 明显。";

describe("chat 渲染面剥 provenance 标签", () => {
  it("★不渲 chip 的那支", () => {
    const { container } = render(<Markdown markdown={TAGGED} />);
    expect(container.textContent).not.toContain("[llm-std:");
    expect(container.textContent).not.toContain("[bi:");
    expect(container.textContent).toContain("通用财务概念");
  });

  it("★★渲 chip 的那支——Lead 散文走的就是这条，泄漏也发生在这条", () => {
    const { container } = render(
      <Markdown markdown={TAGGED} renderArtifactChip={(id: string) => <span>{id}</span>} />,
    );
    expect(container.textContent).not.toContain("[llm-std:");
    expect(container.textContent).not.toContain("[bi:");
    expect(container.textContent).toContain("通用财务概念");
  });

  it("★产物 chip 语法不许被误删", () => {
    const md = "见 [data:art_abc123] 这张表";
    const plain = render(<Markdown markdown={md} />);
    expect(plain.container.textContent).toContain("art_abc123");
  });
});
