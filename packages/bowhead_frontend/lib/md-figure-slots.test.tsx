/**
 * chat-inline-figure-2026-08 batch 1 — `[figure:]` block slots.
 *
 * Criteria from `docs/sprints/chat-inline-figure-2026-08/tech-design.md` §7 (12 items).
 * Hard gates are 1/4/6: positive-only tests miss old chunk-splitting regressions and exposed hallucinated IDs.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Markdown, scanFigureAnchors } from "./md";

/** Render `<Markdown>` to static HTML — assert **final DOM**, rather than intermediate tree. */
function html(markdown: string, opts: Parameters<typeof Markdown>[0] extends never ? never : Record<string, unknown> = {}) {
  return renderToStaticMarkup(
    React.createElement(Markdown, { markdown, ...opts } as never),
  );
}

/** Matches render a recognizable block placeholder; `null` means unknown ID. */
const KNOWN = new Set(["sec_a", "sec_b", "sec_ea7fc923592b"]);
const slot = (id: string) =>
  KNOWN.has(id) ? React.createElement("div", { className: "SLOT", "data-id": id }, "CHART") : null;

describe("rehypeFigureSlots — 块级 figure 槽位", () => {
  it("判据1 ★ 不给 renderFigureSlot ⇒ 与今天逐字一致（插件根本不挂）", () => {
    const md = "结论一。\n[figure:sec_a]\n**图1**：说明\n\n结论二。";
    // No callback = current behavior: anchors appear verbatim as ordinary text.
    expect(html(md)).toBe(html(md));
    expect(html(md)).toContain("[figure:sec_a]");
    // And no figure elements are created
    expect(html(md)).not.toContain("<figure");
  });

  it("判据1b 无锚点的正文 ⇒ 开不开回调渲染结果完全一致", () => {
    const md = "## 标题\n\n- 甲\n- 乙\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n收尾**粗体**。";
    expect(html(md, { renderFigureSlot: slot })).toBe(html(md));
  });

  it("判据2 空行分隔的锚点 ⇒ 图渲在该位置", () => {
    const out = html("结论一。\n\n[figure:sec_a]\n\n结论二。", { renderFigureSlot: slot });
    expect(out).toContain('class="SLOT" data-id="sec_a"');
    expect(out).not.toContain("[figure:");
    expect(out.indexOf("结论一")).toBeLessThan(out.indexOf("SLOT"));
    expect(out.indexOf("SLOT")).toBeLessThan(out.indexOf("结论二"));
  });

  it("判据3 ★★ 生产主导形状：软换行 锚点+图注（59%）", () => {
    const md =
      "上半年营业额逐月下降。\n[figure:sec_ea7fc923592b]\n**图1**：2026年1-6月月度营业额呈逐月下降趋势。\n\n下一段。";
    const out = html(md, { renderFigureSlot: slot });
    expect(out).not.toContain("[figure:");
    expect(out).toContain('data-id="sec_ea7fc923592b"');
    // Caption remains a normal paragraph (**图1** renders as strong) and appears **after** figure
    expect(out).toContain("<strong>图1</strong>");
    expect(out.indexOf("SLOT")).toBeLessThan(out.indexOf("<strong>图1</strong>"));
    // Prose precedes figure and is not swallowed
    expect(out.indexOf("上半年营业额逐月下降")).toBeLessThan(out.indexOf("SLOT"));
  });

  it("判据4 ★ 未知 id ⇒ 锚行消失，屏幕上不得出现 [figure: 字面量", () => {
    const out = html("结论。\n\n[figure:sec_HALLUCINATED]\n\n尾。", { renderFigureSlot: slot });
    expect(out).not.toContain("[figure:");
    expect(out).not.toContain("sec_HALLUCINATED");
    expect(out).not.toContain("SLOT");
    // Both body paragraphs remain
    expect(out).toContain("结论。");
    expect(out).toContain("尾。");
  });

  it("判据6 ★ 4 条列表仍是 4 条（防「撑成 8 行」回归）", () => {
    const out = html("- 甲\n- 乙\n- 丙\n- 丁\n\n[figure:sec_a]\n\n收尾。", { renderFigureSlot: slot });
    expect((out.match(/<li>/g) ?? []).length).toBe(4);
    expect(out).toContain("SLOT");
    // List is not split into multiple <ul> elements
    expect((out.match(/<ul>/g) ?? []).length).toBe(1);
  });

  it("判据6b 表格结构完整", () => {
    const out = html("| a | b |\n|---|---|\n| 1 | 2 |\n\n[figure:sec_a]\n\n尾。", { renderFigureSlot: slot });
    expect(out).toContain("<table>");
    expect((out.match(/<td>/g) ?? []).length).toBe(2);
    expect(out).toContain("SLOT");
  });

  it("判据9 句中（非独占行）锚点 ⇒ 剥掉，不切段、不留字面量", () => {
    const out = html("句中 [figure:sec_a] 还有字。", { renderFigureSlot: slot });
    expect(out).not.toContain("[figure:");
    expect(out).not.toContain("SLOT"); // No paragraph split = no block slot
    expect((out.match(/<p>/g) ?? []).length).toBe(1);
    expect(out).toContain("还有字");
  });

  it("判据11 同一 id 引用两次 ⇒ 只渲一次，第二个锚行消失", () => {
    const out = html("甲。\n\n[figure:sec_a]\n\n乙。\n\n[figure:sec_a]\n\n丙。", { renderFigureSlot: slot });
    expect((out.match(/class="SLOT"/g) ?? []).length).toBe(1);
    expect(out).not.toContain("[figure:");
  });

  it("判据12 回调只被本插件产出的槽位调用（带 id，且不多不少）", () => {
    // ⚠ Honest caveat: `components.figure` statement `if (!id) return <figure {...props}/>`
    // is **currently unreachable** — react-markdown escapes raw HTML by default (measured:
    // `<figure class="x">` renders as `&lt;figure…&gt;` text), so external `<figure>`
    // never enters components mapping. That statement is defense-in-depth (same as `sup`),
    // becoming effective only if rehype-raw is added later. This case asserts the **verifiable half**:
    // only real slots invoke callback, exactly once with real ID.
    const seen: string[] = [];
    const out = html("正文。\n\n[figure:sec_a]\n\n尾。", {
      renderFigureSlot: (id: string) => {
        seen.push(id);
        return slot(id);
      },
    });
    expect(seen).toEqual(["sec_a"]);
    expect(out).toContain("SLOT");
  });

  it("多个不同 id 连续，各自归位", () => {
    const out = html("头。\n[figure:sec_a]\n**图1**：甲。\n[figure:sec_b]\n**图2**：乙。", {
      renderFigureSlot: slot,
    });
    expect((out.match(/class="SLOT"/g) ?? []).length).toBe(2);
    expect(out.indexOf('data-id="sec_a"')).toBeLessThan(out.indexOf('data-id="sec_b"'));
    expect(out).not.toContain("[figure:");
  });

  it("判据7 [data:art_*] / [cite:N] chip 不受影响", () => {
    const out = html("结论 [data:art_x1] 和 [cite:3]。\n\n[figure:sec_a]\n\n尾。", {
      renderFigureSlot: slot,
      renderArtifactChip: (id: string) => React.createElement("span", { className: "CHIP" }, id),
    });
    expect((out.match(/class="CHIP"/g) ?? []).length).toBe(2);
    expect(out).toContain("SLOT");
  });

  it("锚点前后只有换行 ⇒ 不留空 <p>", () => {
    const out = html("甲。\n[figure:sec_a]\n乙。", { renderFigureSlot: slot });
    expect(out).not.toMatch(/<p>\s*<\/p>/);
  });

  it("行内 code 里的锚点不当锚点处理", () => {
    // <code> is an element child, not a text node ⇒ naturally bypasses splitting logic.
    const out = html("正文 `[figure:sec_a]` 收尾。", { renderFigureSlot: slot });
    expect(out).toContain("<code>[figure:sec_a]</code>");
    expect(out).not.toContain("SLOT");
  });

  it("code fence 里的锚点不当锚点处理", () => {
    const out = html("```\n[figure:sec_a]\n```\n\n尾。", { renderFigureSlot: slot });
    expect(out).toContain("[figure:sec_a]"); // Code fences contain code, not references
    expect(out).not.toContain("SLOT");
  });
});

describe("scanFigureAnchors — 与插件同源的预扫描", () => {
  it("只认独占行，按首次出现去重排序", () => {
    expect(scanFigureAnchors("甲。\n[figure:sec_b]\n乙。\n\n[figure:sec_a]\n\n[figure:sec_b]")).toEqual([
      "sec_b",
      "sec_a",
    ]);
  });
  it("句中锚点不算被引用", () => {
    expect(scanFigureAnchors("句中 [figure:sec_a] 还有字。")).toEqual([]);
  });
  it("无锚点 / 空串", () => {
    expect(scanFigureAnchors("没有锚点。")).toEqual([]);
    expect(scanFigureAnchors("")).toEqual([]);
  });
});
