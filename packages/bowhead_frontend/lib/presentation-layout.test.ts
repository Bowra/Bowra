import { describe, expect, it } from "vitest";

import type { InventoryElement } from "./presentation-edit";
import { alignment, cellRect, fontLook, locateTable, locateText, pageGlyphs, styledRuns, type PageGlyph } from "./presentation-layout";

const glyph = (text: string, left: number, bottom: number, size = 0.04, font = "PingFangSC-Regular", width = text.length * 0.02): PageGlyph => ({
  text, left, right: left + width, top: bottom - size, bottom, size, font,
});

const textElement = (paragraphs: string[], box: [number, number, number, number]): InventoryElement => ({
  key: "256:2", shape_id: 2, name: "标题", kind: "text", box, editable: true, reason: null,
  paragraphs: paragraphs.map((text) => ({ text, mixed_style: false, fields: 0 })),
});

describe("in-place layout", () => {
  it("reads pdf.js items with their PDF font names", () => {
    const [item] = pageGlyphs([{ str: "季度", transform: [20, 0, 0, 20, 50, 400], width: 40, fontName: "g_d0_f2" }], [0, 0, 960, 540], { g_d0_f2: "BAAAAA+PingFangSC-Semibold" });
    expect(item.font).toBe("BAAAAA+PingFangSC-Semibold");
    expect(item.left).toBeCloseTo(50 / 960);
    expect(item.bottom).toBeCloseTo(1 - 400 / 540);
    expect(item.size).toBeCloseTo(20 / 540);
    expect(fontLook(item.font)).toMatchObject({ weight: 600, italic: false });
    expect(fontLook("Carlito-Bold").weight).toBe(700);
    expect(fontLook("Verdana").family.startsWith('"Verdana"')).toBe(true);
  });

  it("takes only the element's own text where boxes overlap, and splits it into paragraphs", () => {
    const title = textElement(["区域表现：5 个省份", "按年度"], [0.08, 0.02, 0.8, 0.1]);
    const glyphs = [
      glyph("区域表现：", 0.1, 0.07, 0.05), glyph("5 个省份", 0.22, 0.07, 0.05),
      glyph("按年度", 0.1, 0.11, 0.03),
      // A subtitle of another element inside the title's box.
      glyph("按省份分组", 0.1, 0.11, 0.02),
    ];
    const layout = locateText(title, glyphs);
    expect(layout?.paragraphs.map((paragraph) => paragraph.index)).toEqual([0, 1]);
    expect(layout?.paragraphs[0].size).toBeCloseTo(0.05);
    expect(layout?.paragraphs[0].glyphs.map((item) => item.text)).toEqual(["区域表现：", "5 个省份"]);
    expect(layout?.paragraphs[1].glyphs.map((item) => item.text)).toEqual(["按年度"]);
    expect(layout?.paragraphs[0].charGlyph.slice(0, 6)).toEqual([0, 0, 0, 0, 0, 1]);
    expect(locateText(textElement(["别处的字"], [0.5, 0.5, 0.1, 0.1]), glyphs)).toBeNull();
  });

  it("keeps the look of each character, replaced ones included", () => {
    // 「季度销售额 2,905 万元」: the number is bold (glyph 1).
    const charGlyph = [0, 0, 0, 0, 0, null, 1, 1, 1, 1, 1, null, 2, 2];
    // A space has no glyph of its own and goes with the text before it.
    expect(styledRuns("季度销售额 2,905 万元", "季度销售额 3,050 万元", charGlyph)).toEqual([
      { text: "季度销售额 ", glyph: 0 }, { text: "3,050 ", glyph: 1 }, { text: "万元", glyph: 2 },
    ]);
    expect(styledRuns("季度销售额 2,905 万元", "季度销售额 2,905 万元（3 月）", charGlyph)).toEqual([
      { text: "季度销售额 ", glyph: 0 }, { text: "2,905 ", glyph: 1 }, { text: "万元（3 月）", glyph: 2 },
    ]);
  });

  it("places table rows from the rendered lines, which grow when text wraps", () => {
    const table: InventoryElement = {
      key: "256:5", shape_id: 5, name: "表格 1", kind: "table", box: [0.1, 0.4, 0.6, 0.2], editable: true, reason: null,
      rows: [
        [{ text: "产品线", editable: true, reason: null }, { text: "销售额（万元）", editable: true, reason: null }],
        [{ text: "产品线 A", editable: true, reason: null }, { text: "1,200", editable: true, reason: null }],
      ],
      col_widths: [0.5, 0.5],
      row_heights: [0.5, 0.5],
    };
    const glyphs = [
      // The header wrapped onto two lines, so the render is taller than the file's 0.2.
      glyph("产品线", 0.11, 0.44, 0.03), glyph("销售额（万", 0.41, 0.44, 0.03), glyph("元）", 0.41, 0.48, 0.03),
      glyph("产品线 A", 0.11, 0.56, 0.03), glyph("1,200", 0.41, 0.56, 0.03),
    ];
    const layout = locateTable(table, glyphs);
    expect(layout?.located).toBe(1);
    const header = cellRect(layout!, 0, 1);
    expect(header[1] + header[3]).toBeGreaterThan(0.48);
    const body = cellRect(layout!, 1, 0);
    expect(body[1]).toBeLessThan(0.53);
    expect(body[1] + body[3]).toBeGreaterThan(0.56);
    expect(layout?.cells[1][1].map((item) => item.text)).toEqual(["1,200"]);
  });

  it("tells left, centered and right-aligned text apart", () => {
    expect(alignment([glyph("1,200", 0.1, 0.5, 0.03, "", 0.05)], 0.1, 0.3)).toBe("left");
    expect(alignment([glyph("1,200", 0.175, 0.5, 0.03, "", 0.05)], 0.1, 0.3)).toBe("center");
    expect(alignment([glyph("1,200", 0.24, 0.5, 0.03, "", 0.05)], 0.1, 0.3)).toBe("right");
  });
});
