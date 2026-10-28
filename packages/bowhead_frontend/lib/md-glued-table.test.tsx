/**
 * GFM headers glued to prose-line endings.
 *
 * Synthetic examples keep separators and data rows on separate lines while attaching the header
 * to the preceding paragraph. GFM needs a standalone header to recognize the table.
 * Only repair the boundary when the next line is a valid GFM separator.
 * Leave normal tables, pipes inside code blocks and occasional prose `|` characters untouched.
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Markdown, repairGluedTableHeader } from "./md";

const GLUED = [
  "**Brand ranking and share** Example A led the sample period. | Rank | 品牌 | 金额 | 占比 |",
  "|---:|---|---:|---:|",
  "| 1 | 示例甲 | 12,345.67 | 25.00% |",
  "| 2 | 示例乙 | 9,876.54 | 20.00% |",
].join("\n");

describe("repairGluedTableHeader", () => {
  it("★ 把粘在散文后的表头拆到自己那一行", () => {
    const repaired = repairGluedTableHeader(GLUED);
    const lines = repaired.split("\n");
    expect(lines[0]).toBe("**Brand ranking and share** Example A led the sample period.");
    expect(lines[2]).toBe("| Rank | 品牌 | 金额 | 占比 |");
  });

  it("★ 修完真能被 GFM 认成表格 —— 只断言字符串等于没验到底", () => {
    const { container } = render(<Markdown markdown={GLUED} />);
    const table = container.querySelector("table");
    expect(table).not.toBeNull();
    expect(table!.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(table!.querySelectorAll("thead th")).toHaveLength(4);
  });

  it("★ positive control：同一个串绕开修复直接喂 remark-gfm，渲不出表格", () => {
    // Without this, the case above could merely test remark-gfm's existing recognition, making the fix ineffective.
    const { container } = render(
      <ReactMarkdown remarkPlugins={[[remarkGfm, { singleTilde: false }]]}>{GLUED}</ReactMarkdown>,
    );
    expect(container.querySelector("table")).toBeNull();
  });
});

describe("误伤对照", () => {
  it("本来就合法的表格一个字都不许动", () => {
    const ok = ["| A | B |", "|---|---|", "| 1 | 2 |"].join("\n");
    expect(repairGluedTableHeader(ok)).toBe(ok);
  });

  it("★ 散文里的竖线不许被拆 —— 下一行不是分隔行就不动手", () => {
    const prose = "口径是 A | B | C 三选一。\n这一行不是分隔行。";
    expect(repairGluedTableHeader(prose)).toBe(prose);
  });

  it("★ 代码块里的竖线不许被拆", () => {
    const code = ["```", "cat x | grep y | wc -l", "还有一行", "```"].join("\n");
    expect(repairGluedTableHeader(code)).toBe(code);
  });

  it("没有竖线的文本原样返回（快路径）", () => {
    const plain = "一段普通散文，没有任何管道符。";
    expect(repairGluedTableHeader(plain)).toBe(plain);
  });

  it("空值不炸", () => {
    expect(repairGluedTableHeader("")).toBe("");
  });
});
