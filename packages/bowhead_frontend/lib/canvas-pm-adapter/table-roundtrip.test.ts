/**
 * GFM table read/write roundtrip — fix content that can be read but not written back.
 *
 * Background: on detecting tables, the read path `markdownToPMNodes` explicitly loads Tiptap
 * Table/TableRow/TableCell/TableHeader to parse them; the write schema originally lacked these four nodes,
 * so `fromJSON` threw `Unknown node type: tableHeader` → the whole section degraded to plain text.
 * Production consequence: narratives containing tables were degraded **without any user edits**;
 * editing one character erased the section's headings/bold/figure anchors/entire table (116 evidence, irreversible).
 *
 * Lock the **paired implementation** here: schema recognizes nodes + serializer writes them back; either missing half fails.
 */

import { describe, expect, it } from "vitest";

import { markdownToPMNodes, pmNodesToMarkdown } from "./markdown-pm";

function roundTrip(md: string): { out: string; degraded: string[] } {
  const nodes = markdownToPMNodes(md);
  const degraded: string[] = [];
  const out = pmNodesToMarkdown(nodes, { onDegrade: (r) => degraded.push(r) });
  return { out, degraded };
}

describe("GFM 表格往返", () => {
  it("含表格的段不再降级，且表格结构保住", () => {
    const md = [
      "结论如下。",
      "",
      "| 品牌 | 营业额（万元） |",
      "| --- | --- |",
      "| 金辉福 | 43,964 |",
      "| 六福珠宝 | 15,955 |",
    ].join("\n");

    const { out, degraded } = roundTrip(md);

    // Critical assertion: no longer use the degradation branch
    expect(degraded).toEqual([]);
    // Tables remain tables, rather than flattened plain text
    expect(out).toMatch(/\|\s*品牌\s*\|/);
    expect(out).toMatch(/\|\s*-{3,}\s*\|/);
    expect(out).toContain("金辉福");
    expect(out).toContain("43,964");
    expect(out).toContain("15,955");
  });

  it("表格与图锚点、加粗、小标题共存时全部保真", () => {
    // These elements were erased together in production
    const md = [
      "## 头部格局",
      "",
      "[figure:sec_abc123]",
      "**图1**：说明文字。",
      "",
      "| 排名 | 品牌 |",
      "| --- | --- |",
      "| 第1名 | 金辉福 |",
    ].join("\n");

    const { out, degraded } = roundTrip(md);

    expect(degraded).toEqual([]);
    expect(out).toContain("## 头部格局");
    expect(out).toContain("[figure:sec_abc123]");
    expect(out).toContain("**图1**");
    expect(out).toMatch(/\|\s*排名\s*\|/);
  });

  it("单元格内的加粗被保留，竖线被转义（否则会把一格劈成两格）", () => {
    const md = ["| 项目 | 值 |", "| --- | --- |", "| **重点** | a\\|b |"].join("\n");

    const { out, degraded } = roundTrip(md);

    expect(degraded).toEqual([]);
    expect(out).toContain("**重点**");
    expect(out).toContain("\\|");
  });

  it("列数不齐的表补空列，不产生错位的行", () => {
    const md = ["| a | b | c |", "| --- | --- | --- |", "| 1 | 2 |"].join("\n");

    const { out, degraded } = roundTrip(md);

    expect(degraded).toEqual([]);
    // Each data row must have the same pipe count as the header, or GFM rendering misaligns
    const lines = out.split("\n").filter((l) => l.trim().startsWith("|"));
    const bars = lines.map((l) => (l.match(/(?<!\\)\|/g) ?? []).length);
    expect(new Set(bars).size).toBe(1);
  });

  it("正向对照：不含表格的普通段落照旧不降级", () => {
    const { out, degraded } = roundTrip("普通段落，**加粗**，结束。");

    expect(degraded).toEqual([]);
    expect(out).toContain("**加粗**");
  });
});
