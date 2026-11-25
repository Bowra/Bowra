import { afterEach, describe, expect, it, vi } from "vitest";

import {
  fetchPresentationInventory,
  findRepeatedValue,
  formatChartValue,
  matchPendingEdits,
  pageTextItems,
  parseChartValue,
  PresentationEditError,
  savePresentationEdits,
  measureTextOverflow,
  spillsFurther,
  toWireEdits,
  upsertPendingEdit,
  type InventoryChart,
  type PendingEdit,
  type PresentationInventory,
} from "./presentation-edit";

afterEach(() => {
  vi.unstubAllGlobals();
});

const chart: InventoryChart = {
  type: "COLUMN_CLUSTERED",
  categories: ["1 月", "2 月"],
  series: [{ name: "销售额", values: [1180, 905], format: "#,##0" }],
};

function inventory(overrides: Partial<PresentationInventory> = {}): PresentationInventory {
  return {
    artifact_id: "art_v4",
    version_no: 4,
    edit_mode: "version",
    slide_width: 12192000,
    slide_height: 6858000,
    field_token: "￼",
    file: { filename: "季度复盘.pptx", mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation", size: 4096 },
    new_filename: null,
    edited_copies: [],
    slides: [{
      slide_id: 256,
      index: 1,
      inherited_text: false,
      elements: [
        { key: "256:2", shape_id: 2, name: "Title", kind: "text", box: [0.1, 0.1, 0.5, 0.1], editable: true, reason: null,
          paragraphs: [{ text: "季度复盘", mixed_style: false, fields: 0 }] },
        { key: "256:3", shape_id: 3, name: "Table", kind: "table", box: [0.1, 0.3, 0.5, 0.3], editable: true, reason: null,
          rows: [[{ text: "产品线 B（含新品）", editable: true, reason: null }]] },
        { key: "256:4", shape_id: 4, name: "Chart", kind: "chart", box: [0.6, 0.3, 0.3, 0.3], editable: true, reason: null, chart },
      ],
    }],
    ...overrides,
  };
}

describe("chart numbers follow the series format", () => {
  it("shows grouping, decimals and percent as the deck does", () => {
    expect(formatChartValue(1180, "#,##0", "zh-CN")).toBe("1,180");
    expect(formatChartValue(0.91, "0%", "zh-CN")).toBe("91%");
    expect(formatChartValue(0.915, "0.0%", "en-US")).toBe("91.5%");
    expect(formatChartValue(1234.5, "General", "en-US")).toBe("1234.5");
    expect(formatChartValue(null, "#,##0", "en-US")).toBe("");
  });

  it("reads what a person typed, including 91% in a percent series and the locale's separators", () => {
    expect(parseChartValue("1,250", "#,##0", "zh-CN")).toBe(1250);
    expect(parseChartValue("91%", "0%", "zh-CN")).toBeCloseTo(0.91);
    expect(parseChartValue("93", "0%", "zh-CN")).toBeCloseTo(0.93);
    expect(parseChartValue("1.250,5", "#,##0.0", "de-DE")).toBe(1250.5);
    expect(parseChartValue("12,5", "0.0", "fr-FR")).toBe(12.5);
    expect(parseChartValue("", "#,##0", "en-US")).toBeNull();
    expect(parseChartValue("一千", "#,##0", "zh-CN")).toBeUndefined();
    expect(parseChartValue("12%", "#,##0", "en-US")).toBeUndefined();
  });

  it("finds the same number written as text elsewhere on the slide, but not inside a longer number", () => {
    const texts = [
      { key: "a", text: "季度内最高月 1,180 万元" },
      { key: "b", text: "全年 11,180 万元" },
      { key: "c", text: "1180" },
    ];
    expect(findRepeatedValue(1180, "#,##0", texts)).toEqual(["a", "c"]);
  });
});

describe("pending edits", () => {
  it("drops an edit that types the original back and sends the expectation the person saw", () => {
    const edit: PendingEdit = { kind: "text", key: "256:2", slideId: 256, paragraph: 0, expect: "季度复盘", text: "年度复盘" };
    let edits = upsertPendingEdit([], edit);
    expect(toWireEdits(edits)).toEqual([{ kind: "text", key: "256:2", paragraph: 0, text: "年度复盘", expect: "季度复盘" }]);
    edits = upsertPendingEdit(edits, { ...edit, text: "季度复盘" });
    expect(edits).toEqual([]);
  });

  it("sends chart values as numbers with the series names", () => {
    const edits = upsertPendingEdit([], { kind: "chart", key: "256:4", slideId: 256, expect: chart, values: [[1250, 905]] });
    expect(toWireEdits(edits)).toEqual([{
      kind: "chart",
      key: "256:4",
      expect: { categories: ["1 月", "2 月"], series: [{ name: "销售额", values: [1180, 905] }] },
      series: [{ name: "销售额", values: [1250, 905] }],
    }]);
  });

  it("re-applies only edits whose target is unchanged in the newer version", () => {
    const rows = matchPendingEdits([
      { kind: "text", key: "256:2", slideId: 256, paragraph: 0, expect: "季度复盘", text: "年度复盘" },
      { kind: "cell", key: "256:3", slideId: 256, row: 0, col: 0, expect: "产品线 B", text: "产品线 B（华东）" },
      { kind: "chart", key: "256:4", slideId: 256, expect: chart, values: [[1250, 905]] },
      { kind: "text", key: "256:9", slideId: 256, paragraph: 0, expect: "已删除", text: "新的" },
    ], inventory());
    expect(rows.map((row) => [row.applicable, row.latestText])).toEqual([
      [true, null],
      [false, "产品线 B（含新品）"],
      [true, null],
      [false, null],
    ]);
  });
});

describe("text outside its box after a save", () => {
  const box: [number, number, number, number] = [0.1, 0.1, 0.5, 0.1];

  const none = { above: 0, below: 0, right: 0 };

  it("reports text of the element that runs below its box", () => {
    const items = [
      { text: "第一行很长的标题", left: 0.11, right: 0.55, top: 0.11, bottom: 0.15 },
      { text: "第二行", left: 0.11, right: 0.3, top: 0.19, bottom: 0.24 },
    ];
    const after = measureTextOverflow(items, box, "第一行很长的标题第二行");
    expect(after.below).toBeCloseTo(0.04);
    expect(spillsFurther(none, after)).toBe(true);
  });

  it("ignores text that fits, and other elements' text below the box", () => {
    const fits = [{ text: "季度复盘", left: 0.11, right: 0.3, top: 0.12, bottom: 0.17 }];
    expect(measureTextOverflow(fits, box, "季度复盘")).toEqual(none);
    const neighbour = [...fits, { text: "下一段正文", left: 0.11, right: 0.4, top: 0.25, bottom: 0.29 }];
    expect(spillsFurther(none, measureTextOverflow(neighbour, box, "季度复盘"))).toBe(false);
  });

  it("reports a new line below the box even when a large first line already reached above it", () => {
    const title: [number, number, number, number] = [0.05, 0.08, 0.6, 0.12];
    const before = measureTextOverflow([{ text: "Quarterly review", left: 0.0625, right: 0.34, top: 0.063, bottom: 0.13 }], title, "Quarterly review");
    const after = measureTextOverflow([
      { text: "Quarterly review for", left: 0.0625, right: 0.39, top: 0.063, bottom: 0.13 },
      { text: "the whole year", left: 0.0625, right: 0.31, top: 0.156, bottom: 0.222 },
    ], title, "Quarterly review for the whole year");
    expect(spillsFurther(before, before)).toBe(false);
    expect(spillsFurther(before, after)).toBe(true);
  });

  it("converts pdf.js runs of an unrotated page to top-left fractions", () => {
    const [item] = pageTextItems([{ str: "Hi", transform: [12, 0, 0, 12, 100, 500], width: 20, height: 12 }], [0, 0, 1000, 600]);
    expect(item.left).toBeCloseTo(0.1);
    expect(item.right).toBeCloseTo(0.12);
    expect(item.bottom).toBeCloseTo(1 - 500 / 600);
    expect(item.top).toBeCloseTo(1 - 512 / 600);
  });
});

describe("requests", () => {
  it("posts the inventory request to the owner Results route and reads uploads' extra fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ...inventory({ edit_mode: "new_file" }),
      new_filename: "客户模板（已修改）.pptx",
      edited_copies: [{ artifact_id: "art_copy", filename: "客户模板（已修改）.pptx", version_no: 2 }],
    }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const value = await fetchPresentationInventory({ sessionId: "s 1", artifactId: "art_up" });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/sessions/s%201/results/presentation-edit/inventory");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ artifact_id: "art_up" });
    expect(value.new_filename).toBe("客户模板（已修改）.pptx");
    expect(value.edited_copies).toEqual([{ artifact_id: "art_copy", filename: "客户模板（已修改）.pptx", version_no: 2 }]);
  });

  it("turns a refused save into the facts the editor shows", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: { error: { code: "version_target_not_latest", latest: {
        artifact_id: "art_v4", version_no: 4, created_at: "2026-11-24T06:32:00Z", produced_by: "agent", request_excerpt: "把总结页改成三条要点",
      } } },
    }), { status: 409 })));
    const error = await savePresentationEdits({ sessionId: "s_1", artifactId: "art_v3", edits: [] }).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(PresentationEditError);
    expect((error as PresentationEditError).latest).toMatchObject({ version_no: 4, produced_by: "agent", request_excerpt: "把总结页改成三条要点" });

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: { error: { code: "parse_unavailable", reason: "timeout", diagnostic_id: "pe_0a1b2c3d4e" } },
    }), { status: 503 })));
    const failed = await savePresentationEdits({ sessionId: "s_1", artifactId: "art_v3", edits: [] }).catch((cause: unknown) => cause) as PresentationEditError;
    expect(failed.diagnostics).toMatchObject({ status: 503, code: "parse_unavailable", diagnosticCode: "pe_0a1b2c3d4e" });
  });
});
