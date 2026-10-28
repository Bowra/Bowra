/**
 * chat-ux-2026-08 batch D — inline citation badges.
 *
 * DOM observed on 116 prod v1.0.71 (356px column):
 *
 *   div.lead-narrative-md
 *     div.v2-md              [356x22]   ← one list item
 *     span.bh-artifact-chip  [35x20]    ← badge became a **block sibling**, occupying its own row
 *     div.v2-md              [356x22]   ← next list item
 *     span.bh-artifact-chip  [35x20]
 *
 * A four-item list expanded to eight lines. `renderFinalized` split markdown at every token,
 * placing chips between two block-level `.v2-md` elements — **CSS alone cannot fix it**.
 *
 * Assert **structure** (chips actually inside paragraphs/list items/table cells),
 * not CSS text: jsdom has no layout or geometry, but whether a chip's parent is li
 * is a structural fact and precisely the bug itself.
 *
 * Mutation guard: restore renderFinalized's splitting → all inside-X assertions must fail.
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "@/messages/zh-CN.json";
import { LeadNarrative, CitationsProvider } from "./LeadNarrative";

afterEach(() => cleanup());

// ArtifactChip silently drops **unregistered** citations (existing 0.39.8 #2 decision:
// render nothing rather than a「[数据]」placeholder in prose). Tests must register citations,
// otherwise they test dropping instead of inline placement.
const CITES = [
  { seq: 1, artifactId: "art_abc123", label: "近30天营业额" },
  { seq: 4, artifactId: "art_def456", label: "退款率均值" },
] as never;

function mount(md: string) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <CitationsProvider citations={CITES}>
        <LeadNarrative text={md} isStreaming={false} />
      </CitationsProvider>
    </NextIntlClientProvider>,
  );
}

const TOK = "[data:art_abc123]";

describe("引用角标 — 落在行内，不再是块级兄弟", () => {
  it("★ 列表项内的角标在 <li> 内部（旧实现会把它甩到 li 外面）", () => {
    const { container } = mount(
      `- 营业额：约 9.10 亿，同比微增 +0.13% ${TOK}\n- 利润：约 1.57 亿 ${TOK}`,
    );
    const li = container.querySelectorAll("li");
    expect(li.length).toBe(2);
    // Each list item contains **its own** chip.
    for (const item of Array.from(li)) {
      expect(item.querySelector(".bh-artifact-chip")).not.toBeNull();
    }
    // No chip is a direct child of .lead-narrative-md (the old structure).
    const root = container.querySelector(".lead-narrative-md")!;
    const strayChips = Array.from(root.children).filter((c) =>
      c.classList.contains("bh-artifact-chip"),
    );
    expect(strayChips.length).toBe(0);
  });

  it("句中的角标在 <p> 内部", () => {
    const { container } = mount(`退款率高达 79.2% ${TOK}，远超均值。`);
    const p = container.querySelector("p");
    expect(p).not.toBeNull();
    expect(p!.querySelector(".bh-artifact-chip")).not.toBeNull();
  });

  it("表格单元格内的角标在 <td> 内部", () => {
    const { container } = mount(
      `| 品牌 | 退款率 |\n| --- | --- |\n| 中国黄金 | 79.2% ${TOK} |`,
    );
    const td = container.querySelectorAll("td");
    expect(td.length).toBeGreaterThan(0);
    const withChip = Array.from(td).some((c) => c.querySelector(".bh-artifact-chip"));
    expect(withChip).toBe(true);
  });

  it("整段只渲**一个** .v2-md 容器（旧实现每个 token 切一个）", () => {
    const { container } = mount(`第一句 ${TOK} 第二句 ${TOK} 第三句 ${TOK}`);
    expect(container.querySelectorAll(".v2-md").length).toBe(1);
  });

  it("没有 token 时结构不变（回归面）", () => {
    const { container } = mount("- 一\n- 二");
    expect(container.querySelectorAll("li").length).toBe(2);
    expect(container.querySelectorAll(".bh-artifact-chip").length).toBe(0);
  });

  it("代码块里的方括号不被当成引用（与 BE 的 fence 保留口径一致）", () => {
    const { container } = mount("```python\nx = a[1]\ny = b[data:art_x]\n```");
    expect(container.querySelector("code")).not.toBeNull();
    expect(container.querySelectorAll(".bh-artifact-chip").length).toBe(0);
  });

  it("[cite:N] 不再被兜底剥离——启用行内角标时由调用方解析成 chip", () => {
    const { container } = mount("三者均值是整体的 5.9 倍 [cite:4]。");
    // The raw literal must not remain in prose…
    expect(container.textContent).not.toContain("[cite:4]");
    // …and it must actually become a chip (the old stripInlineNumMarkers path removed it entirely,
    // silently losing the citation).
    const p = container.querySelector("p");
    expect(p!.querySelector(".bh-artifact-chip")).not.toBeNull();
  });
});
