/**
 * Write-back fidelity: silent losses exposed after removing the blanket degradation gate.
 *
 * Background: 26261bb9a added table schema+serializer to the write path, fixing readable-but-unwritable tables. The
 * blanket gate previously **also blocked** Link / strike / codeBlock: these already lacked fidelity, simply being
 * kept out of persistence. Narrowing the gate turned the same fidelity loss from unsavable into **silent corrupting writes**.
 * Enforce each here, plus an **inventory gate** (last describe) to prevent another extension slipping through.
 */

import { Editor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import Link from "@tiptap/extension-link";
import { Table } from "@tiptap/extension-table";
import TableRow from "@tiptap/extension-table-row";
import TableCell from "@tiptap/extension-table-cell";
import TableHeader from "@tiptap/extension-table-header";
import { TextStyle } from "@tiptap/extension-text-style";
import FontFamily from "@tiptap/extension-font-family";
import type { JSONContent } from "@tiptap/react";
import { describe, expect, it } from "vitest";

import { CanvasDocument, getAllCanvasExtensions } from ".";
import { FontSize } from "./extensions/font-size";
import { FontWeight } from "./extensions/font-weight";
import { markdownToPMNodes, pmNodesToMarkdown } from "./markdown-pm";

function write(nodes: JSONContent[]): { md: string; degraded: string[] } {
  const degraded: string[] = [];
  const md = pmNodesToMarkdown(nodes, { onDegrade: (r) => degraded.push(r) });
  return { md, degraded };
}

function roundTrip(md: string) {
  return write(markdownToPMNodes(md));
}

const TABLE = ["| a | b |", "| --- | --- |", "| 1 | 2 |"].join("\n");

describe("[cite:N] —— 显示期的清理不许发生在往返路径上", () => {
  it("★ 往返必须保住 [cite:N]，否则用户敲一个字就把引用锚写没了", () => {
    // Observed on 116 dogfood, 2026-08-15: read-only load of cv_51ac5cfb5a5a marked only one of 11 sections
    // changed: sec_f828f0c2af72, missing its trailing ` [cite:1]`, and
    // **not marked degraded**, hence savable in edit mode: typing one character anywhere in the report
    // makes flush PATCH this section without [cite:1], permanently erasing its citation anchor.
    //
    // Root cause: display-time cleanup (canvas lacks a cite resolver, so raw [cite:1] would leak)
    // lived in markdownToPMNodes, the **round-trip read side**, rather than the rendering layer.
    // Correct approach: retain original text in PM doc; citation-plugin decorations hide it.
    const src = "全年规模稳定。 [cite:1]";
    const { md, degraded } = roundTrip(src);

    expect(degraded).toEqual([]);
    expect(md).toContain("[cite:1]");
  });

  it("正向对照：不含 marker 的同一句本来就保真（证明上面那条不是恒真）", () => {
    const { md } = roundTrip("全年规模稳定。");
    expect(md).toContain("全年规模稳定。");
  });
});

describe("超链接（段里有表格时曾被静默删除）", () => {
  it("同段有表格时，显式 markdown 链接仍然活着", () => {
    // Production shape: detecting a table switches the read path to generateJSON, whose extensions
    // **never included Link** ⇒ <a> becomes plain text ⇒ a user edit permanently erases links.
    const { md, degraded } = roundTrip(`见 [官网](https://example.com/a)。\n\n${TABLE}`);

    expect(degraded).toEqual([]);
    expect(md).toContain("[官网](https://example.com/a)");
  });

  it("正向对照：同一段去掉表格时链接本来就是好的", () => {
    // This comparison was the original diagnostic experiment; the sole difference is presence of a table.
    const { md } = roundTrip("见 [官网](https://example.com/a)。");
    expect(md).toContain("[官网](https://example.com/a)");
  });

  it("裸 URL 不被 linkify 成自动链接 —— 两条读路径必须给出同一份 doc", () => {
    // markdown-it linkify turns bare URLs into <a>, unlike the commonmark preset path.
    // Unfixed, first editing of every table-containing section adds a spurious <https://…> diff.
    const withTable = roundTrip(`见 https://bare.example.com 结束。\n\n${TABLE}`);
    const noTable = roundTrip("见 https://bare.example.com 结束。");

    expect(withTable.md).toContain("见 https://bare.example.com 结束。");
    expect(withTable.md).not.toContain("<https://bare.example.com>");
    expect(noTable.md).toContain("见 https://bare.example.com 结束。");
  });
});

describe("删除线（曾让整段永久存不上）", () => {
  it("编辑器直接产出的 strike mark 能写回 GFM，不再降级", () => {
    // Cmd+Shift+X uses this path: StarterKit Strike is enabled, but write schema lacks strike
    // ⇒ fromJSON throws `There is no mark type strike in this schema` ⇒ fail-closed permanently rejects.
    const { md, degraded } = write([
      {
        type: "paragraph",
        content: [
          { type: "text", text: "保留" },
          { type: "text", marks: [{ type: "strike" }], text: "删掉" },
        ],
      },
    ]);

    expect(degraded).toEqual([]);
    expect(md).toBe("保留~~删掉~~");
  });

  it("`~~文字~~` 读得回 strike mark —— 有没有表格都一样", () => {
    // Fixing write without read saves strikethrough, but refresh returns two literal tilde sequences; saving again produces  `\~\~`。
    for (const src of ["这是 ~~删除线~~ 文字。", `这是 ~~删除线~~ 文字。\n\n${TABLE}`]) {
      const nodes = markdownToPMNodes(src);
      const marks = JSON.stringify(nodes);
      expect(marks).toContain('"strike"');
      expect(write(nodes).md).toContain("~~删除线~~");
    }
  });

  it("往返是不动点，不会每存一次多一层转义", () => {
    let cur = "这是 ~~删除线~~ 文字。";
    for (let i = 0; i < 3; i++) cur = roundTrip(cur).md;
    expect(cur).toBe("这是 ~~删除线~~ 文字。");
  });
});

describe("代码块（同段有表格时丢语言 + 换行无界增长）", () => {
  const SRC = ["```python", "print(1)", "```", "", TABLE].join("\n");

  it("语言标注保住", () => {
    // Tiptap CodeBlock gets language through element.firstElementChild, but @tiptap/html zeed-dom
    // lacks this property ⇒ language always null. Read from markdown-it tokens instead (the language source).
    const { md, degraded } = roundTrip(SRC);
    expect(degraded).toEqual([]);
    expect(md).toContain("```python");
  });

  it("反复往返长度不变 —— 换行不再每代 +1", () => {
    // Before fix: 38 → 39 → 40 → 41, one extra blank line per save, growing without bound.
    const lengths: number[] = [];
    let cur = SRC;
    for (let i = 0; i < 4; i++) {
      cur = roundTrip(cur).md;
      lengths.push(cur.length);
    }
    expect(new Set(lengths).size).toBe(1);
  });

  it("裸 <pre> 与 fence 混排时语言不串位 —— 自查逮到的", () => {
    // Language was taken by position from markdown-it **tokens**, but with html:true (#305 typography),
    // raw body <pre> uses html_block tokens while generateJSON still turns it into a codeBlock
    // ⇒ counts differ and languages misalign. Observed: raw <pre> took the following ```python language,
    // while ```python became null, worse than loss: assigning language **to the wrong block**.
    // Now count <pre> in generateJSON **sole input** (HTML string), guaranteeing alignment.
    const md = [
      '<span style="font-size: 14px">大字</span>',
      "",
      "<pre><code>raw block</code></pre>",
      "",
      "```python",
      "print(1)",
      "```",
    ].join("\n");

    const blocks = markdownToPMNodes(md).filter((n) => n.type === "codeBlock");

    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toMatchObject({ attrs: { language: null } });
    expect(blocks[1]).toMatchObject({ attrs: { language: "python" } });
  });

  it("code_block.params ↔ codeBlock.language 双向都通", () => {
    const nodes = markdownToPMNodes("```sql\nSELECT 1\n```");
    expect(nodes[0]).toMatchObject({ type: "codeBlock", attrs: { language: "sql" } });
    expect(write(nodes).md).toContain("```sql");
  });
});

describe("紧凑列表不得因为改走 HTML 路就变松散", () => {
  it("同一段加个删除线，列表写回来必须一模一样 —— 自查逮到的", () => {
    // Default PM schema bullet_list has tight attr (read by serializer renderList);
    // Tiptap bulletList **does not** ⇒ generateJSON output lacks that key ⇒
    // fromJSON fills schema default false ⇒ tight lists are written back as loose.
    // Table/typography paths already suffered; adding strikethrough routing expanded my blast radius.
    const body = ["## 标题", "", "PLACEHOLDER", "", "- 列表一", "- 列表二", "", "> 引用"].join("\n");
    const withStrike = body.replace("PLACEHOLDER", "这是 ~~删除线~~ 与 **加粗**。");
    const without = body.replace("PLACEHOLDER", "这是 删除线 与 **加粗**。");

    const a = write(markdownToPMNodes(withStrike)).md;
    const b = write(markdownToPMNodes(without)).md;

    expect(a).toContain("- 列表一\n- 列表二");
    // Both read paths must produce identical output for the same structure (except the tilde pair)
    expect(a.replace("~~删除线~~", "删除线")).toBe(b);
  });

  it("反向保险：markdown 里真的松散列表仍写回松散", () => {
    // Fill tight only when the **key is absent**. Markdown-parsed lists carry explicit true/false,
    // real information; overfilling would compress loose lists into tight ones.
    const loose = "- 第一\n\n- 第二";
    expect(write(markdownToPMNodes(loose)).md).toBe(loose);
  });
});

describe("表格单元格的块级转义", () => {
  it("行首的 `-` / `N.` / `>` 不再被写成字面反斜杠", () => {
    // GFM parses table cells inline, so block constructs do not apply; these escapes are redundant,
    // yet persist unchanged and render as literal backslashes in Word / PDF / Feishu exports.
    const src = [
      "| 指标 | 值 |",
      "| --- | --- |",
      "| 毛利 | -37.5% |",
      "| 步骤 | 1. 第一 |",
      "| 备注 | > 注意 |",
    ].join("\n");

    const { md, degraded } = roundTrip(src);

    expect(degraded).toEqual([]);
    expect(md).toContain("| -37.5% |");
    expect(md).toContain("| 1. 第一 |");
    expect(md).toContain("| > 注意 |");
    expect(md).not.toContain("\\-");
    expect(md).not.toContain("\\.");
    expect(md).not.toContain("\\>");
  });

  it("inline 级转义**不能**跟着一起撤 —— 竖线和星号仍要转义", () => {
    // Reverse safeguard: remove only leading block-level escapes; over-removal lets a|b split one cell into two and * become emphasis.
    const src = ["| 项目 | 值 |", "| --- | --- |", "| **重点** | a\\|b |"].join("\n");
    const { md } = roundTrip(src);

    expect(md).toContain("**重点**");
    expect(md).toContain("a\\|b");
  });
});

/**
 * Inventory gate: the most valuable test in this batch.
 *
 * Link / strike / codeBlock all were editor-producible but unrecognized by writes, and **no tests inventoried
 * this**. Guard tests used taskList, a synthetic node absent from the app, to represent that category,
 * leaving genuinely reachable strike untested beside it. Enumerate from the **actual editor schema** instead:
 * every editor-producible mark/node must serialize faithfully through writes, with no omissions.
 */
describe("清点：编辑器能产出的每一样，写路径都必须写得回", () => {
  /** Same source as report-view.tsx extensions (omit purely decorative/interactive extensions, absent from schema). */
  function editorSchema() {
    return new Editor({
      extensions: [
        CanvasDocument,
        StarterKit.configure({ document: false }),
        TextStyle,
        FontFamily,
        FontSize,
        FontWeight,
        Link.configure({ openOnClick: false }),
        Table.configure({ resizable: false }),
        TableRow,
        TableCell,
        TableHeader,
        ...getAllCanvasExtensions(),
      ],
      content: { type: "doc", content: [] } as never,
    }).schema;
  }

  /** One minimal sample per mark (with required attrs). */
  const MARK_SAMPLES: Record<string, JSONContent> = {
    bold: { type: "text", marks: [{ type: "bold" }], text: "x" },
    italic: { type: "text", marks: [{ type: "italic" }], text: "x" },
    strike: { type: "text", marks: [{ type: "strike" }], text: "x" },
    code: { type: "text", marks: [{ type: "code" }], text: "x" },
    link: {
      type: "text",
      marks: [{ type: "link", attrs: { href: "https://example.com", title: null } }],
      text: "x",
    },
    textStyle: {
      type: "text",
      marks: [{ type: "textStyle", attrs: { fontSize: "14px", fontFamily: null, fontWeight: null } }],
      text: "x",
    },
  };

  /** One minimal sample per node; listItem only exists within lists, covered by bulletList/orderedList. */
  const NODE_SAMPLES: Record<string, JSONContent> = {
    paragraph: { type: "paragraph", content: [{ type: "text", text: "x" }] },
    heading: { type: "heading", attrs: { level: 2 }, content: [{ type: "text", text: "x" }] },
    blockquote: {
      type: "blockquote",
      content: [{ type: "paragraph", content: [{ type: "text", text: "x" }] }],
    },
    bulletList: {
      type: "bulletList",
      content: [
        {
          type: "listItem",
          content: [{ type: "paragraph", content: [{ type: "text", text: "x" }] }],
        },
      ],
    },
    orderedList: {
      type: "orderedList",
      attrs: { start: 1 },
      content: [
        {
          type: "listItem",
          content: [{ type: "paragraph", content: [{ type: "text", text: "x" }] }],
        },
      ],
    },
    codeBlock: {
      type: "codeBlock",
      attrs: { language: "python" },
      content: [{ type: "text", text: "print(1)" }],
    },
    hardBreak: {
      type: "paragraph",
      content: [{ type: "text", text: "a" }, { type: "hardBreak" }, { type: "text", text: "b" }],
    },
    horizontalRule: { type: "horizontalRule" },
    table: {
      type: "table",
      content: [
        {
          type: "tableRow",
          content: [
            {
              type: "tableHeader",
              content: [{ type: "paragraph", content: [{ type: "text", text: "h" }] }],
            },
          ],
        },
        {
          type: "tableRow",
          content: [
            {
              type: "tableCell",
              content: [{ type: "paragraph", content: [{ type: "text", text: "c" }] }],
            },
          ],
        },
      ],
    },
    figure_ref: { type: "figure_ref", attrs: { targetSectionId: "sec_x" } },
  };

  /**
   * Structural nodes outside Markdown serialization: doc is root; section_* are canvas section wrappers,
   * directly unpacked to ReportSection by pmNodeToSection and never passed to pmNodesToMarkdown.
   * text / listItem / tableRow / tableCell / tableHeader only live inside other nodes,
   * covered transitively by the samples above.
   */
  const NOT_SERIALIZED = new Set(["doc", "text", "listItem", "tableRow", "tableCell", "tableHeader"]);
  const isSectionWrapper = (name: string) => name.startsWith("section_");

  it("每个 mark 都能保真写回", () => {
    for (const [name, sample] of Object.entries(MARK_SAMPLES)) {
      const { degraded } = write([{ type: "paragraph", content: [sample] }]);
      expect(degraded, `mark "${name}" 降级了`).toEqual([]);
    }
  });

  it("每个节点都能保真写回", () => {
    for (const [name, sample] of Object.entries(NODE_SAMPLES)) {
      const { degraded } = write([sample]);
      expect(degraded, `节点 "${name}" 降级了`).toEqual([]);
    }
  });

  it("样本表没有漏项 —— 编辑器 schema 里的每一样都在上面被测到", () => {
    // Maintenance gate: adding a report-view extension immediately fails here,
    // requiring both markdown-pm schema + serializer support (the omitted step for Link/strike).
    const schema = editorSchema();

    const marks = Object.keys(schema.marks);
    expect(marks.filter((m) => !(m in MARK_SAMPLES))).toEqual([]);

    const nodes = Object.keys(schema.nodes).filter(
      (n) => !NOT_SERIALIZED.has(n) && !isSectionWrapper(n),
    );
    expect(nodes.filter((n) => !(n in NODE_SAMPLES))).toEqual([]);
  });
});
