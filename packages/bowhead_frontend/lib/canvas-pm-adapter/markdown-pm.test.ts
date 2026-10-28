/**
 * #305: typography (font-family / font-size / font-weight) round-trip tests.
 * Spike verified: markdown-it html:true → Tiptap textStyle/FontFamily parsing → marks.
 * Tests actual bidirectional markdownToPMNodes / pmNodesToMarkdown conversion.
 */

import { describe, expect, it } from "vitest";

import { markdownToPMNodes, pmNodesToMarkdown } from "./markdown-pm";


describe("markdown-pm typography round-trip (#305)", () => {
  it("inline span font-size 解析为 textStyle mark", () => {
    const md = `段落 <span style="font-size: 18px">大字</span> 段尾`;
    const nodes = markdownToPMNodes(md);
    // Find text nodes containing a textStyle mark
    const flat: Array<{ text?: string; marks?: Array<{ type: string; attrs?: Record<string, unknown> }> }> = [];
    function walk(n: unknown): void {
      if (!n || typeof n !== "object") return;
      const node = n as Record<string, unknown>;
      if (node.type === "text") flat.push(node as never);
      const c = node.content;
      if (Array.isArray(c)) c.forEach(walk);
    }
    nodes.forEach(walk);
    const styled = flat.find((n) =>
      Array.isArray(n.marks) && n.marks.some((m) => m.type === "textStyle"),
    );
    expect(styled?.text).toBe("大字");
    const mark = styled?.marks?.find((m) => m.type === "textStyle");
    expect(mark?.attrs?.fontSize).toBe("18px");
  });

  it("font-family + font-size + font-weight 三 attr 同 span", () => {
    const md = `<span style="font-family: 'Source Han Serif'; font-size: 18px; font-weight: 500">styled</span>`;
    const nodes = markdownToPMNodes(md);
    let found: Record<string, unknown> | undefined;
    function walk(n: unknown): void {
      if (!n || typeof n !== "object") return;
      const node = n as Record<string, unknown>;
      if (node.type === "text" && Array.isArray(node.marks)) {
        const m = (node.marks as Array<{ type: string; attrs?: Record<string, unknown> }>)
          .find((mm) => mm.type === "textStyle");
        if (m?.attrs) found = m.attrs;
      }
      const c = node.content;
      if (Array.isArray(c)) c.forEach(walk);
    }
    nodes.forEach(walk);
    expect(found).toBeDefined();
    expect(found?.fontFamily).toContain("Source Han Serif");
    expect(found?.fontSize).toBe("18px");
    expect(found?.fontWeight).toBe("500");
  });

  it("无 typography markdown 走原 path 不挂 (regression)", () => {
    const md = "普通段落, **粗体**, 没有 span";
    const nodes = markdownToPMNodes(md);
    expect(nodes.length).toBeGreaterThan(0);
    expect(nodes[0].type).toBe("paragraph");
  });

  it("PM textStyle mark serialize 回 markdown 输出 raw <span>", () => {
    const nodes = [
      {
        type: "paragraph",
        content: [
          { type: "text", text: "段落 " },
          {
            type: "text",
            marks: [{ type: "textStyle", attrs: { fontSize: "18px" } }],
            text: "大字",
          },
          { type: "text", text: " 段尾" },
        ],
      },
    ];
    const md = pmNodesToMarkdown(nodes);
    expect(md).toContain(`<span style="font-size: 18px">大字</span>`);
  });

  it("round-trip: md → PM → md 保留 inline span style", () => {
    const original = `段落 <span style="font-size: 18px">大字</span> 段尾`;
    const nodes = markdownToPMNodes(original);
    const back = pmNodesToMarkdown(nodes);
    expect(back).toContain(`<span style=`);
    expect(back).toContain(`font-size: 18px`);
    expect(back).toContain("大字");
  });
});

describe("RC-8 stale-figure note layout (report_final_gate 产物解析)", () => {
  // Backend final-report gate inserts a blockquote warning after a stale figure block (anchor + caption), isolated by blank lines
  // (design review P1-1: without blank lines, CommonMark lazy continuation absorbs subsequent text into
  // the blockquote). Assert the actual gate output parses correctly: promoted figure_ref + caption as
  // a separate paragraph (outside the blockquote) + warning as a separate blockquote + body as a separate paragraph.
  const STALE_NOTE_TEXT = "⚠ 上图为本轮之前生成,数据未在本轮重算";

  function collectText(n: unknown): string {
    if (!n || typeof n !== "object") return "";
    const node = n as Record<string, unknown>;
    let s = typeof node.text === "string" ? node.text : "";
    const c = node.content;
    if (Array.isArray(c)) for (const ch of c) s += collectText(ch);
    return s;
  }

  it("锚+caption+警示+正文: caption 不被吸进 blockquote", () => {
    const md = [
      "[figure:sec_a1b2]",
      "**图1**:存款结构",
      "",
      "> " + STALE_NOTE_TEXT,
      "",
      "后续正文继续。",
    ].join("\n");
    const nodes = markdownToPMNodes(md);
    const types = nodes.map((n) => n.type);
    expect(types[0]).toBe("figure_ref");
    expect(nodes[0].attrs?.targetSectionId).toBe("sec_a1b2");
    const bqIdx = types.indexOf("blockquote");
    expect(bqIdx).toBeGreaterThan(0);
    const bqText = collectText(nodes[bqIdx]);
    expect(bqText).toContain(STALE_NOTE_TEXT);
    expect(bqText).not.toContain("图1"); // Caption was not absorbed into the warning block
    expect(bqText).not.toContain("后续正文"); // Body was not absorbed into the warning block
    const capIdx = nodes.findIndex((n) => collectText(n).includes("存款结构"));
    expect(capIdx).toBeGreaterThan(0);
    expect(capIdx).toBeLessThan(bqIdx); // Caption paragraph precedes the warning
    expect(nodes.some((n) => collectText(n).includes("后续正文继续"))).toBe(true);
  });

  it("无 caption 变体: 锚后直接警示,正文独立", () => {
    const md = ["[figure:sec_c3d4]", "", "> " + STALE_NOTE_TEXT, "", "正文。"].join("\n");
    const nodes = markdownToPMNodes(md);
    expect(nodes[0].type).toBe("figure_ref");
    const bq = nodes.find((n) => n.type === "blockquote");
    expect(bq).toBeDefined();
    expect(collectText(bq)).not.toContain("正文。");
  });
});

// ─── feedback-0713 #7 S1 spike regression guard ─────────────────────────
// Investigation of paragraphs disappearing on serif switch cleared serialization/parsing: var() token values, compound
// (font-family+font-size) spans, and even the messiest actual editor output interleaving emphasis markers with spans
// all preserve text. Typography round-trips previously tested only literal font names; these tests fix
// var() and compound shapes to prevent future markdown-it/generateJSON upgrades from silently breaking them.
describe("markdown-pm var() serif 复合 span 往返 (feedback-0713 #7)", () => {
  function textOf(nodes: unknown[]): string {
    const out: string[] = [];
    function walk(n: unknown): void {
      if (!n || typeof n !== "object") return;
      const node = n as Record<string, unknown>;
      if (node.type === "text" && typeof node.text === "string") out.push(node.text);
      if (Array.isArray(node.content)) node.content.forEach(walk);
    }
    nodes.forEach(walk);
    return out.join("");
  }

  it("var(--font-v2-serif)+font-size 复合 span 双向往返文字保真", () => {
    const nodes = [
      {
        type: "paragraph",
        content: [
          {
            type: "text",
            marks: [
              { type: "textStyle", attrs: { fontFamily: "var(--font-v2-serif)", fontSize: "17px" } },
            ],
            text: "本季度华东区毛利率显著下滑。",
          },
        ],
      },
    ];
    const md = pmNodesToMarkdown(nodes);
    expect(md).toContain("font-family: var(--font-v2-serif)");
    const back = markdownToPMNodes(md);
    expect(textOf(back)).toContain("本季度华东区毛利率显著下滑");
    // Marks also returned (beyond merely retaining text)
    const flat = JSON.stringify(back);
    expect(flat).toContain("var(--font-v2-serif)");
    expect(flat).toContain("17px");
  });

  it("真实编辑器产出的强调符与 span 交错形状不吞文字", () => {
    // PATCH payload shape captured by the device S1 spike (three spans interleaved with ** emphasis markers)
    const md =
      '**<span style="font-family: var(--font-v2-sans)">本季度华东区毛利率显著下滑，主要受客</span>**' +
      '<span style="font-family: var(--font-v2-sans); font-size: 18px">流</span>' +
      '**<span style="font-family: var(--font-v2-sans)">减少影响。</span>**';
    const back = markdownToPMNodes(md);
    const text = textOf(back);
    expect(text).toContain("本季度华东区毛利率显著下滑");
    expect(text).toContain("流");
    expect(text).toContain("减少影响");
  });

  it("整段 span 套粗体标记不吞文字", () => {
    const md =
      '<span style="font-family: var(--font-v2-serif); font-size: 18px">**整段加粗**且衬线的文字。</span>';
    expect(textOf(markdownToPMNodes(md))).toContain("且衬线的文字");
  });
});


// ─── Batch 1 2.1 (chart-table-ux): caption decorations promise zero document changes ───────────
// Decorations are view-only (figure-caption.ts produces Decoration without changing the doc):
// narrative containing a [figure:] anchor + "图N：" caption must round-trip **byte-for-byte unchanged**.
// If someone puts captions into figure_ref attrs (rejected option 1), this fails first.
describe("批1 2.1 图注往返字节不变（decoration 零文档变更）", () => {
  it("[figure:x] + **图1**：caption 的 narrative 往返字节级不变", () => {
    const md = [
      "前文段落。",
      "",
      "[figure:sec_x1]",
      "",
      "**图1**：营业额自 1 月高点一路走低",
      "",
      "后续正文。",
    ].join("\n");
    const back = pmNodesToMarkdown(markdownToPMNodes(md));
    expect(back).toBe(md);
  });
});
