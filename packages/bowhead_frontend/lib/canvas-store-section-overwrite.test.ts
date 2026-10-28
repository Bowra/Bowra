/**
 * 0.39.7 #2 — canvas stream→F5 inconsistency repro + regression guard.
 *
 * Bug: `_hasContent` didn't recognize insight_card/chart/code_block/etc
 * kind-specific fields. `onSectionAdded`'s skeleton-replace loop treated
 * just-filled sections as still-empty, so the next section_added with
 * the same kind overwrote the previous. Repro: 3 consecutive insight_cards
 * → only the LAST rendered live. F5 reload (hydrateCanvas batch path)
 * showed all 3 because hydrate doesn't go through the skeleton loop.
 *
 * Regression sequence for a report whose final narrative differs from intermediate sections:
 *   - Backend canvas has 3 insight_cards (sec_b00e..., sec_5a6b..., sec_47e7...)
 *   - Pre-F5 DOM: 1 insight_card visible (sec_47e7..., the LAST added)
 *   - Post-F5 DOM: 3 insight_cards visible
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  getCanvasSnapshot,
  onCanvasOpened,
  onSectionAdded,
  resetCanvas,
} from "./canvas-store";

describe("canvas-store · section overwrite bug (0.39.7 #2)", () => {
  afterEach(() => {
    resetCanvas();
  });

  it("3 consecutive insight_card section_added all persist (not just last)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({
      section: {
        id: "sec_a", kind: "insight_card",
        title: "Insight A", thesis: "T-A", narrative: "N-A",
      } as never,
    });
    onSectionAdded({
      section: {
        id: "sec_b", kind: "insight_card",
        title: "Insight B", thesis: "T-B", narrative: "N-B",
      } as never,
    });
    onSectionAdded({
      section: {
        id: "sec_c", kind: "insight_card",
        title: "Insight C", thesis: "T-C", narrative: "N-C",
      } as never,
    });
    const ids = getCanvasSnapshot().sections.map((s) => (s as { id: string }).id);
    expect(ids).toContain("sec_a");
    expect(ids).toContain("sec_b");
    expect(ids).toContain("sec_c");
    expect(ids).toHaveLength(3);
  });

  it("4 consecutive chart section_added all persist (not just last)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    const mkChart = (id: string, title: string) => ({
      section: {
        id, kind: "chart",
        spec: { kind: "line", title, source_meta: { id: "art_x" } },
      } as never,
    });
    onSectionAdded(mkChart("sec_chart_1", "Q1 GMV"));
    onSectionAdded(mkChart("sec_chart_2", "Q2 GMV"));
    onSectionAdded(mkChart("sec_chart_3", "Q3 GMV"));
    onSectionAdded(mkChart("sec_chart_4", "Q4 GMV"));
    const ids = getCanvasSnapshot().sections.map((s) => (s as { id: string }).id);
    expect(ids).toEqual(["sec_chart_1", "sec_chart_2", "sec_chart_3", "sec_chart_4"]);
  });

  it("2 code_block section_added persist (regression for sandbox-output blocks)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({
      section: {
        id: "sec_code_1", kind: "code_block",
        call_id: "c1", lang: "python", code: "print('one')",
      } as never,
    });
    onSectionAdded({
      section: {
        id: "sec_code_2", kind: "code_block",
        call_id: "c2", lang: "python", code: "print('two')",
      } as never,
    });
    const ids = getCanvasSnapshot().sections.map((s) => (s as { id: string }).id);
    expect(ids).toEqual(["sec_code_1", "sec_code_2"]);
  });

  it("sources section persists alongside other sources (entries field)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({
      section: {
        id: "sec_src_1", kind: "sources",
        label: "Data Sources",
        entries: [{ label: "bi_query: A", tool: "bi_query" }],
      } as never,
    });
    onSectionAdded({
      section: {
        id: "sec_src_2", kind: "sources",
        label: "Doc Sources",
        entries: [{ label: "kb_search: B" }],
      } as never,
    });
    const ids = getCanvasSnapshot().sections.map((s) => (s as { id: string }).id);
    expect(ids).toContain("sec_src_1");
    expect(ids).toContain("sec_src_2");
  });

  it("insight_card with only narrative (no thesis) still treated as content", () => {
    // Some payloads carry narrative without thesis — must NOT be "empty".
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({
      section: {
        id: "sec_n1", kind: "insight_card",
        title: "X",  // title alone is enough
      } as never,
    });
    onSectionAdded({
      section: {
        id: "sec_n2", kind: "insight_card",
        title: "Y",
      } as never,
    });
    const ids = getCanvasSnapshot().sections.map((s) => (s as { id: string }).id);
    expect(ids).toEqual(["sec_n1", "sec_n2"]);
  });

  it("legacy text/markdown fields still gate correctly (regression)", () => {
    // Don't break the existing narrative-with-markdown path.
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({
      section: { id: "sec_n_a", kind: "narrative", markdown: "First." } as never,
    });
    onSectionAdded({
      section: { id: "sec_n_b", kind: "narrative", markdown: "Second." } as never,
    });
    const ids = getCanvasSnapshot().sections.map((s) => (s as { id: string }).id);
    expect(ids).toEqual(["sec_n_a", "sec_n_b"]);
  });
});
