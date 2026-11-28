import { describe, expect, it } from "vitest";

import { layoutDocument, locateReadOnly, paintDecision, paragraphPage, readLocatePages, type ParagraphLayout, type PdfDocumentForLayout } from "./document-layout";

const HEIGHT = 842;
const item = (str: string, x: number, baseline: number, width: number, size = 10) => ({ str, transform: [size, 0, 0, size, x, HEIGHT - baseline], width, height: size });

describe("document layout (docx inline edit design §8.2, §8.3)", () => {
  it("reads every page's text relative to its own origin and finds paragraphs by page", async () => {
    const pages = [
      [item("第一页的第一段文字写在这里", 72, 100, 130)],
      [item("第二页开头的一段话放在这里", 72, 100, 130)],
    ];
    // A page whose box does not start at 0: its items are moved back to the page's origin.
    const pdf: PdfDocumentForLayout = {
      numPages: 2,
      getPage: async (number) => ({
        view: number === 2 ? [10, 20, 605, 862] : [0, 0, 595, 842],
        getTextContent: async () => ({ items: number === 2 ? pages[1].map((entry) => ({ ...entry, transform: [10, 0, 0, 10, entry.transform[4] + 10, entry.transform[5] + 20] })) : pages[0] }),
      }),
    };
    const read = await readLocatePages(pdf);
    expect(read.map((page) => [page.width, page.height])).toEqual([[595, 842], [595, 842]]);
    const layout = layoutDocument(read, { paragraphs: [
      { key: "a", kind: "body", table: null, text: "第一页的第一段文字写在这里", heading_level: null, editable: true, reason: null },
      { key: "b", kind: "body", table: null, text: "第二页开头的一段话放在这里", heading_level: null, editable: true, reason: null },
      { key: "c", kind: "body", table: null, text: "渲染里找不到的段落", heading_level: null, editable: true, reason: null },
      { key: "d", kind: "body", table: null, text: "不能改的页眉文字", heading_level: null, editable: false, reason: "header_footer" },
    ] });
    expect(paragraphPage(layout, "a")).toBe(1);
    expect(paragraphPage(layout, "b")).toBe(2);
    expect(layout.paragraphs.get("b")?.lines[0].x0).toBeCloseTo(72);
    expect(layout.missing).toEqual(["c"]);
  });

  it("finds a header on every page and a note only on its page", () => {
    const pages = [
      { width: 595, height: HEIGHT, items: [item("季度复盘报告", 250, 40, 60), item("正文第一页", 72, 100, 50), item("1 资料来源：年报", 72, 800, 80)] },
      { width: 595, height: HEIGHT, items: [item("季度复盘报告", 250, 40, 60), item("正文第二页", 72, 100, 50), item("2", 290, 820, 5)] },
    ];
    const found = locateReadOnly(pages, [
      { key: "header1:b0", part: "header", text: "季度复盘报告", reason: "header_footer" },
      { key: "footnotes:n2.b0", part: "footnote", text: " 资料来源：年报", reason: "note" },
      { key: "footer1:b0", part: "footer", text: "\ufffc", reason: "header_footer" },
    ]);
    expect(found.get("header1:b0")?.map((line) => line.page)).toEqual([0, 1]);
    expect(found.get("footnotes:n2.b0")?.map((line) => line.page)).toEqual([0]);
    expect(found.has("footer1:b0")).toBe(false);
  });

  describe("paintDecision", () => {
    const measure = (text: string, size: number) => [...text].length * size;
    const located = (room: number | null, status: ParagraphLayout["status"] = "exact"): ParagraphLayout => ({
      status, room, lines: [{ page: 0, x0: 72, x1: 200, top: 90, bottom: 102, fontSize: 10 }],
    });

    it("paints an edit that fits the last line with half an em to spare", () => {
      expect(paintDecision({ key: "k", expect: "2,905", text: "3,105" }, located(0), measure, "￼")).toBe("paint");
      expect(paintDecision({ key: "k", expect: "四月", text: "四月三日" }, located(25), measure, "￼")).toBe("paint");
      expect(paintDecision({ key: "k", expect: "四月", text: "四月三日" }, located(24), measure, "￼")).toBe("pending");
    });

    it("keeps as pending what may move text: a new line, a tab, a split or missing paragraph", () => {
      expect(paintDecision({ key: "k", expect: "a", text: "a\nb" }, located(500), measure, "￼")).toBe("pending");
      expect(paintDecision({ key: "k", expect: "a", text: "a\tb" }, located(500), measure, "￼")).toBe("pending");
      expect(paintDecision({ key: "k", expect: "a", text: "b" }, located(500, "split"), measure, "￼")).toBe("pending");
      expect(paintDecision({ key: "k", expect: "a", text: "b" }, undefined, measure, "￼")).toBe("pending");
      expect(paintDecision({ key: "k", expect: "a", text: "b" }, located(null), measure, "￼")).toBe("pending");
    });
  });
});
