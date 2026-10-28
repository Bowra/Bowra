/**
 * report-engine Phase-1 (WP1) — figure_ref anchor round-trip + serializer pair-fix.
 *
 * Guards the confirmed P0 (review-c / framework review2): a `figure_ref` node
 * inside a narrative must serialize back to `[figure:<id>]` and NOT be silently
 * dropped by pmNodesToMarkdown's extractPlainText fallback.
 */

import { describe, expect, it } from "vitest";
import { markdownToPMNodes, pmNodesToMarkdown } from "../markdown-pm";
import { canvasToPMDoc } from "../canvas-to-pm";
import type { ReportSection } from "@/lib/types";

// minimal section shapes — canvasToPMDoc only needs id/kind (+ markdown) for the
// weave logic; full payloads are exercised by the 24-kind round-trip suite.
const narrative = (id: string, markdown: string): ReportSection =>
  ({ id, kind: "narrative", markdown } as unknown as ReportSection);
const chart = (id: string): ReportSection =>
  ({ id, kind: "chart", spec: { kind: "bar", title: "t", columns: [], rows: [] } } as unknown as ReportSection);

const topIds = (doc: { content?: { attrs?: { sectionId?: string } }[] }): string[] =>
  (doc.content ?? []).map((n) => n.attrs?.sectionId ?? "");

describe("figure_ref anchor — forward parse (markdownToPMNodes)", () => {
  it("standalone [figure:id] paragraph → figure_ref node", () => {
    const md = "前面一段文字。\n\n[figure:sec_ab12_7]\n\n**图1**：完成率 70.45%。";
    const nodes = markdownToPMNodes(md);
    const fig = nodes.find((n) => n.type === "figure_ref");
    expect(fig).toBeTruthy();
    expect(fig?.attrs?.targetSectionId).toBe("sec_ab12_7");
    // caption line stays a normal paragraph (so its [N] citations decorate).
    expect(nodes.some((n) => n.type === "paragraph")).toBe(true);
  });

  it("inline [figure:id] mid-sentence is NOT lifted (stays text → pill path)", () => {
    const md = "见 [figure:sec_x] 那张图就懂了。";
    const nodes = markdownToPMNodes(md);
    expect(nodes.find((n) => n.type === "figure_ref")).toBeFalsy();
  });

  it("multiple standalone anchors each lift", () => {
    const md = "[figure:sec_1]\n\n中间文字\n\n[figure:sec_2]";
    const nodes = markdownToPMNodes(md);
    const figs = nodes.filter((n) => n.type === "figure_ref");
    expect(figs.map((f) => f.attrs?.targetSectionId)).toEqual(["sec_1", "sec_2"]);
  });

  // dogfood F7: real Author output puts the anchor and its `**图N**:` caption on
  // CONSECUTIVE lines (no blank line) → CommonMark merges them into ONE paragraph.
  // The lift must still fire and split the caption into its own paragraph; before
  // the fix the anchor leaked to the canvas as literal `[figure:id]` text.
  it("anchor + caption on consecutive lines (no blank) → lift + caption paragraph", () => {
    const md = "女装是利润担当。\n\n[figure:sec_45184b332df6]\n**图1**：品类营业额Top15。\n\n然而下滑。";
    const nodes = markdownToPMNodes(md);
    const fig = nodes.find((n) => n.type === "figure_ref");
    expect(fig?.attrs?.targetSectionId).toBe("sec_45184b332df6");
    // the caption survives as a paragraph (NOT swallowed, NOT left as raw text)
    const captionPara = nodes.find(
      (n) => n.type === "paragraph" && JSON.stringify(n).includes("品类营业额Top15"),
    );
    expect(captionPara).toBeTruthy();
    // the literal anchor text must NOT survive anywhere
    expect(JSON.stringify(nodes)).not.toContain("[figure:");
  });
});

describe("figure_ref anchor — backward serialize (pmNodesToMarkdown) [P0]", () => {
  it("figure_ref node → [figure:id] (NOT dropped)", () => {
    const md = "[figure:sec_ab12_7]";
    const nodes = markdownToPMNodes(md);
    const back = pmNodesToMarkdown(nodes);
    expect(back).toContain("[figure:sec_ab12_7]");
  });

  it("full round-trip: prose + anchor + caption survives both directions", () => {
    const md = "前面。\n\n[figure:sec_ab12_7]\n\n图1：完成率上升。\n\n后面。";
    const nodes = markdownToPMNodes(md);
    const back = pmNodesToMarkdown(nodes);
    // anchor preserved (the regression we are guarding against)
    expect(back).toContain("[figure:sec_ab12_7]");
    // surrounding prose not destroyed by the extractPlainText fallback
    expect(back).toContain("前面");
    expect(back).toContain("后面");
    expect(back).toContain("完成率");
    // re-parse is stable: anchor still lifts
    const nodes2 = markdownToPMNodes(back);
    expect(nodes2.find((n) => n.type === "figure_ref")?.attrs?.targetSectionId).toBe("sec_ab12_7");
  });

  it("existing [num:]/[ref:] placeholders still survive alongside figure_ref", () => {
    const md = "[figure:sec_9]\n\n营收 [num:123:万元:src:tl_abc] 同比 [ref:chunk_5]。";
    const back = pmNodesToMarkdown(markdownToPMNodes(md));
    expect(back).toContain("[figure:sec_9]");
    expect(back).toContain("[num:");
    expect(back).toContain("[ref:");
  });
});

describe("canvasToPMDoc weave — referenced figures skip top-level (INV-1)", () => {
  it("a block-anchored figure is skipped from top-level (renders inline via figure_ref)", () => {
    const sections = [
      narrative("nar_1", "论点。\n\n[figure:chart_1]\n\n图1：完成率。"),
      chart("chart_1"),
    ];
    const doc = canvasToPMDoc(sections) as { content: { type: string; attrs?: { sectionId?: string }; content?: { type: string; attrs?: { targetSectionId?: string } }[] }[] };
    // chart_1 NOT at top-level
    expect(topIds(doc)).not.toContain("chart_1");
    expect(topIds(doc)).toContain("nar_1");
    // narrative carries the figure_ref pointing at chart_1
    const nar = doc.content.find((n) => n.attrs?.sectionId === "nar_1");
    const fig = nar?.content?.find((c) => c.type === "figure_ref");
    expect(fig?.attrs?.targetSectionId).toBe("chart_1");
  });

  it("an unreferenced figure keeps its top-level slot", () => {
    const sections = [
      narrative("nar_1", "论点。\n\n[figure:chart_1]\n\n图1。"),
      chart("chart_1"),
      chart("chart_2"), // not referenced anywhere
    ];
    const ids = topIds(canvasToPMDoc(sections));
    expect(ids).not.toContain("chart_1"); // referenced → skipped
    expect(ids).toContain("chart_2"); // orphan → stays
  });

  it("no anchors → all sections present (no regression)", () => {
    const sections = [narrative("nar_1", "纯文字，无图。"), chart("chart_1")];
    expect(topIds(canvasToPMDoc(sections))).toEqual(["nar_1", "chart_1"]);
  });

  it("inline [figure:id] pill does NOT skip the figure (scroll target stays)", () => {
    const sections = [
      narrative("nar_1", "见 [figure:chart_1] 这张图。"), // inline → pill, not figure_ref
      chart("chart_1"),
    ];
    expect(topIds(canvasToPMDoc(sections))).toContain("chart_1");
  });
});
