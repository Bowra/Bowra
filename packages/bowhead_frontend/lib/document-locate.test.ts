import { describe, expect, it } from "vitest";

import { fitsOnLastLine, lastLineRoom, locateParagraphs, type LocatePage, type PdfTextItemLike } from "./document-locate";

const HEIGHT = 842;

/** A text item drawn with its baseline `baseline` points from the top of the page. */
function item(str: string, x: number, baseline: number, width: number, size = 10): PdfTextItemLike {
  return { str, transform: [size, 0, 0, size, x, HEIGHT - baseline], width, height: size };
}

function page(...items: PdfTextItemLike[]): LocatePage {
  return { width: 595, height: HEIGHT, items };
}

describe("locateParagraphs (docx inline edit design §8.2)", () => {
  it("puts quotes drawn after a line's CJK text back where they stand", () => {
    // LibreOffice draws the CJK text of the line as one item, then the Latin quotes into its gaps.
    const pages = [page(item("增长的含金量正在透支", 100, 100, 200), item("“", 160, 100, 5), item("”", 220, 100, 5))];
    const [located] = locateParagraphs(pages, [{ key: "document:b0", text: "增长的“含金量”正在透支" }]);
    expect(located.status).toBe("exact");
    expect(located.lines).toHaveLength(1);
    expect(located.lines[0]).toMatchObject({ page: 0, x0: 100, x1: 300 });
  });

  it("anchors on unique text so a repeated short cell does not jump ahead", () => {
    const pages = [page(
      item("合计", 100, 100, 20), item("第一季度销售额为两千九百万元", 100, 120, 140),
      item("合计", 100, 140, 20), item("第二季度销售额为三千一百万元", 100, 160, 140),
    )];
    const located = locateParagraphs(pages, [
      { key: "a", text: "第一季度销售额为两千九百万元" },
      { key: "b", text: "合计" },
      { key: "c", text: "第二季度销售额为三千一百万元" },
    ]);
    expect(located.map((result) => result.status)).toEqual(["exact", "exact", "exact"]);
    expect(located[1].lines[0].top).toBeCloseTo(132);
  });

  it("finds a floating table that the render draws after the paragraph that follows it", () => {
    const pages = [page(
      item("表格前面的一段正文内容", 100, 100, 110),
      item("表格后面的一段正文内容", 100, 300, 110),
      item("项目", 100, 150, 20), item("金额", 200, 150, 20),
    )];
    const located = locateParagraphs(pages, [
      { key: "before", text: "表格前面的一段正文内容" },
      { key: "cell-1", text: "项目" },
      { key: "cell-2", text: "金额" },
      { key: "after", text: "表格后面的一段正文内容" },
    ]);
    expect(located.map((result) => result.status)).toEqual(["exact", "exact", "exact", "exact"]);
    expect(located[2].lines[0].x0).toBe(200);
  });

  it("matches a paragraph that a page break and a header cut in two", () => {
    const pages = [
      page(item("这一段从第一页的末尾开始", 100, 780, 120)),
      page(item("季度复盘报告", 250, 40, 60), item("一直写到第二页的开头", 100, 100, 100)),
    ];
    const [located] = locateParagraphs(pages, [{ key: "split", text: "这一段从第一页的末尾开始一直写到第二页的开头" }]);
    expect(located.status).toBe("split");
    expect(located.lines.map((line) => line.page)).toEqual([0, 1]);
  });

  it("reports text the render does not show, and paragraphs with nothing to match", () => {
    const pages = [page(item("页面上的文字", 100, 100, 60))];
    const located = locateParagraphs(pages, [{ key: "gone", text: "不在页面上的段落" }, { key: "image", text: "￼ " }]);
    expect(located.map((result) => result.status)).toEqual(["missing", "empty"]);
  });
});

describe("lastLineRoom and fitsOnLastLine (docx inline edit design §8.3)", () => {
  const body = (key: string, text: string) => ({ key, kind: "body" as const, text });

  it("measures a paragraph's room from its own full lines, else from the page's", () => {
    const pages = [page(
      item("第一行写满了整个版心的宽度", 100, 100, 400), item("第二行较短", 100, 115, 100),
      item("单行段落", 100, 140, 40),
    )];
    const paragraphs = [body("long", "第一行写满了整个版心的宽度第二行较短"), body("short", "单行段落")];
    const room = lastLineRoom(locateParagraphs(pages, paragraphs), paragraphs);
    expect(room.get("long")).toBeCloseTo(300);
    expect(room.get("short")).toBeCloseTo(360);
  });

  it("gives no room after trailing spaces or a line break, or without a known edge", () => {
    const pages = [page(item("结尾有空格", 100, 100, 50), item("单元格", 100, 200, 30))];
    const paragraphs = [body("spaces", "结尾有空格  "), { key: "cell", kind: "cell" as const, text: "单元格", table: { key: "document:b1", col: 0, col_span: 1 } }];
    const room = lastLineRoom(locateParagraphs(pages, paragraphs), paragraphs);
    expect(room.get("spaces")).toBe(0);
    expect(room.get("cell")).toBe(0);
  });

  it("lets a single-line cell reach where its column's full lines end", () => {
    const table = { key: "document:b2", col: 1, col_span: 1 };
    const pages = [page(item("这一格有两行文字第一行写满", 300, 100, 130), item("第二行", 300, 112, 30), item("短格", 300, 140, 20))];
    const paragraphs = [
      { key: "wide", kind: "cell" as const, text: "这一格有两行文字第一行写满第二行", table },
      { key: "narrow", kind: "cell" as const, text: "短格", table },
    ];
    expect(lastLineRoom(locateParagraphs(pages, paragraphs), paragraphs).get("narrow")).toBeCloseTo(110);
  });

  it("paints only what fits with half an em to spare", () => {
    expect(fitsOnLastLine(30, 20, 10)).toBe(true);
    expect(fitsOnLastLine(30, 26, 10)).toBe(false);
    expect(fitsOnLastLine(0, -5, 10)).toBe(true);
    expect(fitsOnLastLine(null, 0, 10)).toBe(false);
  });
});
