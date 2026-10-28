import { describe, expect, it } from "vitest";

import { RUN_CODE_STDOUT_ALIGNED, RUN_CODE_STDOUT_WITH_CSV } from "./__fixtures__/source-content";
import { buildSourceTable, codeRunForSource, looksLikeMarkdown, sourceTextBlocks, type SourceBlock } from "./source-content";
import type { ReportSection } from "./types";

describe("codeRunForSource", () => {
  const block = (id: string, extra: Record<string, unknown>) =>
    ({ id, kind: "code_block", call_id: id, lang: "python", code: "print(1)", ...extra }) as unknown as ReportSection;

  it("links a source alias to the code block that produced it", () => {
    const sections = [block("sec_a", { output_aliases: ["alias-a"] }), block("sec_b", { output_aliases: ["alias-b"] })];
    expect(codeRunForSource(sections, "alias-b")?.id).toBe("sec_b");
    expect(codeRunForSource(sections, "alias-c")).toBeNull();
    expect(codeRunForSource(sections, undefined)).toBeNull();
  });

  it("skips superseded runs and old code blocks without aliases", () => {
    const sections = [
      block("sec_old", { output_aliases: ["alias-a"], superseded: true }),
      block("sec_legacy", { output_artifact_ids: ["art_a"] }),
      block("sec_new", { output_aliases: ["alias-a"] }),
    ];
    expect(codeRunForSource(sections, "alias-a")?.id).toBe("sec_new");
    expect(codeRunForSource([sections[1]!], "alias-a")).toBeNull();
  });
});

function kinds(blocks: SourceBlock[]): string[] {
  return blocks.map((block) => block.kind);
}

describe("sourceTextBlocks: Run Code stdout", () => {
  it("turns a printed DataFrame into a table and keeps the summary lines as preformatted text", () => {
    const blocks = sourceTextBlocks(RUN_CODE_STDOUT_ALIGNED);
    expect(kinds(blocks)).toEqual(["table", "text"]);
    const table = blocks[0]!;
    if (table.kind !== "table") throw new Error("expected a table");
    expect(table.table.columns).toEqual(["月份", "利润率", "退款率", "利润率_环比pp", "退款率_环比pct"]);
    expect(table.table.rows).toHaveLength(4);
    expect(table.table.rows[1]).toMatchObject({ 月份: "2", 利润率: "0.1777", 退款率_环比pct: "20.3704" });
    // NaN is a missing cell, not a reason to demote the column to text.
    expect(table.table.numericColumns).toEqual(table.table.columns);
    const text = blocks[1]!;
    if (text.kind !== "text") throw new Error("expected text");
    // Line breaks survive; list reprs such as `[4.0, 0.25]` never become table rows.
    expect(text.text.split("\n")).toContain("利润率 最大环比升: [4.0, 0.25]");
    expect(text.text).toContain("均值=0.1766\n退款率 1-8月");
  });

  it("drops the default RangeIndex, keeps prose as text and reads the labelled CSV dump", () => {
    const blocks = sourceTextBlocks(RUN_CODE_STDOUT_WITH_CSV);
    expect(kinds(blocks)).toEqual(["table", "text", "table"]);
    const [printed, prose, csv] = blocks;
    if (printed?.kind !== "table" || prose?.kind !== "text" || csv?.kind !== "table") throw new Error("shape");
    expect(printed.table.columns).toEqual(["月份", "营业额_亿", "客流量_万"]);
    expect(printed.table.rows[0]).toEqual({ 月份: "2026年1月", 营业额_亿: "18.250000", 客流量_万: "220.5000" });
    expect(printed.table.numericColumns).toEqual(["营业额_亿", "客流量_万"]);
    expect(prose.text).toBe("营业额 1月 18.25亿 3月 13.90亿 累计 -23.84%\n客流量 1月 220.50万 3月 190.75万 累计 -13.49%");
    expect(csv.label).toBe("csv");
    expect(csv.table.rows).toHaveLength(3);
    expect(csv.table.rows[2]).toEqual({ 月份: "2026年3月", 营业额_亿: "13.9", 客流量_万: "190.75" });
  });

  it("drops the unnamed index column written by DataFrame.to_csv()", () => {
    const blocks = sourceTextBlocks(",region,revenue\n0,East,42\n1,West,41\n");
    expect(blocks).toHaveLength(1);
    const block = blocks[0]!;
    if (block.kind !== "table") throw new Error("expected a table");
    expect(block.table.columns).toEqual(["region", "revenue"]);
  });

  it("keeps a labelled non-RangeIndex as a first column", () => {
    const blocks = sourceTextBlocks("      revenue  orders\nEast       42      7\nWest       41      9");
    const block = blocks[0]!;
    if (block.kind !== "table") throw new Error("expected a table");
    expect(block.table.columns).toEqual(["#1", "revenue", "orders"]);
    expect(block.table.rows[0]).toEqual({ "#1": "East", revenue: "42", orders: "7" });
  });

  it("reads TSV", () => {
    const blocks = sourceTextBlocks("name\tvalue\na\t1\nb\t2");
    expect(kinds(blocks)).toEqual(["table"]);
  });
});

describe("sourceTextBlocks: other content kinds", () => {
  it("routes unambiguous Markdown to the Markdown renderer as one block", () => {
    const md = "## Summary\n\n- stores **42**\n- revenue 3.86";
    expect(sourceTextBlocks(md)).toEqual([{ kind: "markdown", text: md }]);
  });

  it("keeps plain text as one preformatted block with its line breaks", () => {
    const text = "Loaded 2 files\nrows=8\ncolumns=['a', 'b']";
    expect(sourceTextBlocks(text)).toEqual([{ kind: "text", text }]);
  });

  it("never mistakes aligned prose without numeric columns for a table", () => {
    const text = "营业额 1月 19.89亿 8月 9.96亿\n客流量 1月 231.95万 8月 167.70万\n单客 1月 857元 8月 594元";
    expect(kinds(sourceTextBlocks(text))).toEqual(["text"]);
  });

  it("reads a whole JSON array of flat records as a table and leaves JSON objects verbatim", () => {
    const array = sourceTextBlocks('[{"region": "East", "revenue": 42}, {"region": "West", "revenue": 41}]');
    expect(kinds(array)).toEqual(["table"]);
    const object = '{"region": "East", "revenue": 42}';
    expect(sourceTextBlocks(object)).toEqual([{ kind: "text", text: object }]);
  });

  it("never splits a printed list repr across table cells", () => {
    const text = "name pair score\nA [1, 2] 3\nB [4, 5] 6";
    expect(kinds(sourceTextBlocks(text))).toEqual(["text"]);
  });

  it("returns nothing for blank payloads", () => {
    expect(sourceTextBlocks("  \n ")).toEqual([]);
  });
});

describe("looksLikeMarkdown", () => {
  it.each([
    ["# Title", true],
    ["```sql\nselect 1\n```", true],
    ["| a | b |\n| --- | --- |\n| 1 | 2 |", true],
    ["see [docs](https://example.com)", true],
    ["- one\n- two", true],
    ["- only one dash line", false],
    ["月份 营业额\n2026年1月 19.89", false],
  ])("%j → %s", (text, expected) => {
    expect(looksLikeMarkdown(text)).toBe(expected);
  });
});

describe("buildSourceTable", () => {
  it("keeps the real total when the payload carries a subset", () => {
    const table = buildSourceTable(["region", "revenue"], [{ region: "East", revenue: 42 }], 8);
    expect(table.totalRows).toBe(8);
    expect(table.rows).toEqual([{ region: "East", revenue: "42" }]);
    expect(table.numericColumns).toEqual(["revenue"]);
  });

  it("treats a column of four-digit integers as labels (years), not quantities", () => {
    const table = buildSourceTable(["year", "revenue"], [{ year: "2025", revenue: "12" }, { year: "2026", revenue: "13" }]);
    expect(table.numericColumns).toEqual(["revenue"]);
  });

  it("omits the total when every row is shown", () => {
    expect(buildSourceTable(["a"], [{ a: "x" }], 1).totalRows).toBeUndefined();
  });
});
