/**
 * feedback-0713 #3 — preprocessSections image folding + buildImageLookup.
 *
 * chart_section_id is a visual reference (same field for chart/image): images referenced by insight_card
 * fold into the card without duplicate top-level display; unreferenced images (AI posters) are unaffected by orphan-chart
 * rules. Existing chart folding is unchanged.
 */

import { describe, expect, it } from "vitest";

import { preprocessSections } from "./report-view";
import { buildImageLookup } from "@/lib/canvas-pm-adapter/canvas-context";
import { pmDocHasContent, shouldRefuseSetContent } from "@/lib/canvas-pm-adapter";
import type { ReportSection } from "@/lib/types";

const img = (id: string): ReportSection =>
  ({ id, kind: "image", artifact_id: `art_${id}`, caption: `图 ${id}` }) as ReportSection;
const chart = (id: string): ReportSection =>
  ({ id, kind: "chart", spec: { kind: "bar", title: id, rows: [] } }) as unknown as ReportSection;
const insightCard = (id: string, refIds: string[]): ReportSection =>
  ({
    id,
    kind: "insight_card",
    title: "insight",
    evidence: refIds.map((r) => ({ kind: "chart", caption: "c", chart_section_id: r })),
  }) as unknown as ReportSection;
const narrative = (id: string): ReportSection =>
  ({ id, kind: "narrative", markdown: "正文" }) as ReportSection;

describe("preprocessSections image 折叠 (feedback-0713 #3)", () => {
  it("被 insight_card 引用的 image → 折叠不顶层重复显", () => {
    const out = preprocessSections([
      insightCard("sec_card", ["sec_img1"]),
      img("sec_img1"),
      narrative("sec_n"),
    ]);
    expect(out.map((s) => s.id)).not.toContain("sec_img1");
    expect(out.map((s) => s.id)).toContain("sec_card");
  });

  it("未被引用的 image（AI 海报）不受 orphan 规则牵连, 照常顶层渲染", () => {
    const out = preprocessSections([
      insightCard("sec_card", ["sec_chartX"]),
      chart("sec_chartX"),
      img("sec_poster"),
    ]);
    expect(out.map((s) => s.id)).toContain("sec_poster");
    // Existing-behavior comparison: referenced chart is folded
    expect(out.map((s) => s.id)).not.toContain("sec_chartX");
  });

  // Batch 2-d (plan-report-assembly decision #4, 2026-08-24) contract change:
  // Criterion changes from any insight_card on this canvas to this chart being referenced by evidence.
  // Calling add_chart explicitly asks to display a chart. Fold it only when an insight card
  // actually embeds it; charts from other analysis branches must remain independently visible.
  it("被 evidence 引用的 chart 折叠（内嵌进卡片，不顶层重复显）", () => {
    const out = preprocessSections([
      insightCard("sec_card", ["sec_c1"]),
      chart("sec_c1"),
      chart("sec_orphan"),
    ]);
    expect(out.map((s) => s.id)).not.toContain("sec_c1");
  });

  it("未被引用的 chart 顶层照常显示（听大模型的：它调了 add_chart）", () => {
    const out = preprocessSections([
      insightCard("sec_card", ["sec_c1"]),
      chart("sec_c1"),
      chart("sec_orphan"),
    ]);
    expect(out.map((s) => s.id)).toContain("sec_orphan");
  });

  it("无 insight_card 的老 turn: image/chart 全保留", () => {
    const out = preprocessSections([chart("sec_c1"), img("sec_img1"), narrative("sec_n")]);
    expect(out.map((s) => s.id)).toEqual(expect.arrayContaining(["sec_c1", "sec_img1", "sec_n"]));
  });
});

describe("buildImageLookup", () => {
  it("只收 kind=image, 按 id 映射整个 section", () => {
    const sections = [chart("sec_c1"), img("sec_img1"), narrative("sec_n")];
    const map = buildImageLookup(sections);
    expect(map.size).toBe(1);
    expect(map.get("sec_img1")?.artifact_id).toBe("art_sec_img1");
  });
});

describe("pmDocHasContent (feedback-0713 #7 防吞稿不变量)", () => {
  it.each([
    [null, false],
    [{ type: "doc", content: [] }, false],
    [{ type: "doc", content: [{ type: "section_narrative", attrs: {}, content: [{ type: "paragraph" }] }] }, false],
    [
      {
        type: "doc",
        content: [
          {
            type: "section_narrative",
            attrs: {},
            content: [{ type: "paragraph", content: [{ type: "text", text: "真内容" }] }],
          },
        ],
      },
      true,
    ],
    // Atomic sections (chart/image, no children but with payload) count as content
    [{ type: "doc", content: [{ type: "section_chart", attrs: { payload: {} } }] }, true],
    // Whitespace-only text does not
    [{ type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text: "   " }] }] }, false],
  ])("case %#", (doc, expected) => {
    expect(pmDocHasContent(doc as never)).toBe(expected);
  });
});

describe("shouldRefuseSetContent (防吞稿门决策, review P1 锁)", () => {
  const emptyDoc = { type: "doc", content: [{ type: "paragraph" }] };
  const fullDoc = {
    type: "doc",
    content: [{ type: "paragraph", content: [{ type: "text", text: "稿件内容" }] }],
  };
  const figureOnlyDoc = { type: "doc", content: [{ type: "figure_ref", attrs: { targetSectionId: "sec_x" } }] };

  it("空 doc 盖非空编辑器 → 拒（防吞稿本体）", () => {
    expect(shouldRefuseSetContent(emptyDoc as never, fullDoc as never, false)).toBe(true);
  });
  it("显式丢本地（forced）→ 永不拒——取消编辑/冲突重载/回滚必须能清空", () => {
    expect(shouldRefuseSetContent(emptyDoc as never, fullDoc as never, true)).toBe(false);
  });
  it("非空 doc 正常套用", () => {
    expect(shouldRefuseSetContent(fullDoc as never, fullDoc as never, false)).toBe(false);
  });
  it("空编辑器套任何 doc 不拒（对称性）", () => {
    expect(shouldRefuseSetContent(emptyDoc as never, emptyDoc as never, false)).toBe(false);
  });
  it("figure_ref-only 是合法非空 doc, 不算空（review P2: 跨端更新不被误拦）", () => {
    expect(shouldRefuseSetContent(figureOnlyDoc as never, fullDoc as never, false)).toBe(false);
    expect(pmDocHasContent(figureOnlyDoc as never)).toBe(true);
  });
});

describe("preprocessSections seqAuthoritative (A3 report-edit-audit)", () => {
  const eyebrow = (id: string) => ({ id, kind: "eyebrow", text: "e" }) as never;
  const code = (id: string) => ({ id, kind: "code_block", code: "print(1)" }) as never;

  it("默认(seq 非权威) → rank 强排把 header 提前 / code 沉底", () => {
    const out = preprocessSections([code("c1"), eyebrow("e1")]);
    expect(out.map((s) => s.id)).toEqual(["e1", "c1"]);
  });

  it("seqAuthoritative=true → 按库序渲染, 不强排(用户拖拽序得以保留)", () => {
    const out = preprocessSections([code("c1"), eyebrow("e1")], true);
    expect(out.map((s) => s.id)).toEqual(["c1", "e1"]);
  });

  it("seqAuthoritative=true 仍保留 chart 去重(折叠内联图)", () => {
    const out = preprocessSections(
      [
        { id: "ic", kind: "insight_card", title: "t", evidence: [{ chart_section_id: "ch1" }] } as never,
        { id: "ch1", kind: "chart", spec: {} } as never,
      ],
      true,
    );
    expect(out.map((s) => s.id)).toEqual(["ic"]);
  });
});

describe("preprocessSections 多报告分段 (116 dogfood 1.0.93 P2)", () => {
  const sec = (id: string, kind: string): ReportSection => ({ id, kind }) as unknown as ReportSection;

  it("plan 多腿轮: 第二个 h1 起新段, 两腿不互插 (段内 rank 仍生效)", () => {
    const out = preprocessSections([
      sec("a_h1", "h1"),
      sec("a_meta", "meta"),
      sec("a_nar", "narrative"),
      sec("a_kpi", "kpi_row"),
      sec("b_eyebrow", "eyebrow"),
      sec("b_h1", "h1"),
      sec("b_kpi", "kpi_row"),
      sec("b_nar", "narrative"),
    ]);
    expect(out.map((s) => s.id)).toEqual([
      "a_h1", "a_meta", "a_kpi", "a_nar",
      "b_eyebrow", "b_h1", "b_kpi", "b_nar",
    ]);
  });

  it("单 h1 画布=单段: 晚 append 的 header 仍拉到最前 (原语义不变)", () => {
    const out = preprocessSections([
      sec("s_chart", "chart"),
      sec("s_code", "code_block"),
      sec("s_h1", "h1"),
      sec("s_kpi", "kpi_row"),
      sec("s_nar", "narrative"),
    ]);
    expect(out.map((s) => s.id)).toEqual(["s_h1", "s_kpi", "s_chart", "s_nar", "s_code"]);
  });
});
