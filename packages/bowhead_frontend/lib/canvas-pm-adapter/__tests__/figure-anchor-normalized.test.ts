/**
 * Normalized body text must render figures through the **real frontend parser**, validating the backend normalization gate.
 *
 * ⚠ The `NORMALIZED_CASES` below are the **single source of truth across languages**: Python
 * `packages/bowhead_core/tests/test_figure_anchor_frontend_parity.py` extracts this array
 * and asserts `figure_anchor_layout.normalize_figure_anchors(input) == normalized` for each case.
 * Each side checks one half: the backend produces this shape, and the frontend renders figures from it.
 * Changing only one side invalidates the gate, so both use the same literals (as with CAPTION_RE in figure-caption.ts).
 */
import { describe, expect, it } from "vitest";
import { canvasToPMDoc } from "../canvas-to-pm";
import { markdownToPMNodes, pmNodesToMarkdown } from "../markdown-pm";

interface NormalizedCase {
  label: string;
  figures: Record<string, string>;
  /** Before normalization (actual writer output that would appear as literal text on the canvas). */
  input: string;
  /** After normalization (this file asserts that it renders figures). */
  normalized: string;
}

export const NORMALIZED_CASES: NormalizedCase[] = [
  {
    "label": "F4 产物：独占行补空行",
    "figures": { "sec_a": "各门店营业额排名" },
    "input": "前文说明见图。\n[figure:sec_a]",
    "normalized": "前文说明见图。\n\n[figure:sec_a]"
  },
  {
    "label": "行尾锚切段",
    "figures": { "sec_a": "各门店营业额排名" },
    "input": "前文说明见图。[figure:sec_a]",
    "normalized": "前文说明见图。\n\n[figure:sec_a]"
  },
  {
    "label": "句中锚换标题 + 图挪到段后",
    "figures": { "sec_a": "各门店营业额排名" },
    "input": "对照全年排名[figure:sec_a]可见，后文。",
    "normalized": "对照全年排名「各门店营业额排名」可见，后文。\n\n[figure:sec_a]"
  },
  {
    "label": "叠放两锚分成两段",
    "figures": { "sec_a": "各门店营业额排名", "sec_b": "月度趋势" },
    "input": "前文。\n\n[figure:sec_b]\n[figure:sec_a]\n\n后文。",
    "normalized": "前文。\n\n[figure:sec_b]\n\n[figure:sec_a]\n\n后文。"
  },
  {
    "label": "列表不拆断，图落在整块之后",
    "figures": { "sec_a": "各门店营业额排名" },
    "input": "- 条目一 [figure:sec_a]\n- 条目二",
    "normalized": "- 条目一\n- 条目二\n\n[figure:sec_a]"
  },
  {
    "label": "标题行尾锚落到标题之后",
    "figures": { "sec_a": "各门店营业额排名" },
    "input": "## 标题 [figure:sec_a]\n\n正文。",
    "normalized": "## 标题\n\n[figure:sec_a]\n\n正文。"
  }
];

function figureRefIds(nodes: unknown): string[] {
  const out: string[] = [];
  const walk = (n: { type?: string; attrs?: { targetSectionId?: string }; content?: unknown[] }): void => {
    if (n?.type === "figure_ref" && n.attrs?.targetSectionId) out.push(n.attrs.targetSectionId);
    if (Array.isArray(n?.content)) n.content.forEach((child) => walk(child as typeof n));
  };
  (nodes as Array<Parameters<typeof walk>[0]>).forEach(walk);
  return out;
}

describe("后端归一后的正文在前端渲得出图", () => {
  for (const c of NORMALIZED_CASES) {
    it(c.label, () => {
      const nodes = markdownToPMNodes(c.normalized);
      const serialized = JSON.stringify(nodes);

      // ① Each figure id becomes a block-level figure_ref
      const refs = figureRefIds(nodes);
      for (const id of Object.keys(c.figures)) {
        if (c.normalized.includes(`[figure:${id}]`)) expect(refs).toContain(id);
      }
      // ② No literal anchors may remain on screen
      expect(serialized).not.toContain("[figure:");
      // ③ Round-trip bytes stay identical: the normalized shape is an editor fixed point; read-only loading produces no diff
      expect(pmNodesToMarkdown(nodes)).toBe(c.normalized);
      // ④ Inlined figures are no longer rendered a second time at the top level
      const sections = [
        { id: "sec_n", kind: "narrative", seq: 1, markdown: c.normalized },
        ...Object.keys(c.figures).map((id, i) => ({ id, kind: "chart", seq: i + 2, spec: {} })),
      ];
      const doc = canvasToPMDoc(sections as never);
      const topIds = (doc.content ?? []).map((n) => (n.attrs as { sectionId?: string })?.sectionId);
      for (const id of refs) expect(topIds).not.toContain(id);
    });
  }

  it("归一前的原始形状确实渲不出图（否则这道闸没必要存在）", () => {
    let checked = 0;
    for (const c of NORMALIZED_CASES) {
      if (c.input === c.normalized) continue;
      const nodes = markdownToPMNodes(c.input);
      const refs = figureRefIds(nodes);
      const ids = Object.keys(c.figures).filter((id) => c.input.includes(`[figure:${id}]`));
      const leaked = ids.some((id) => !refs.includes(id));
      expect(leaked, `${c.label}：原始形状居然全渲出来了`).toBe(true);
      checked += 1;
    }
    // Positive control: this assertion must actually run, rather than silently pass if the case table is emptied
    expect(checked).toBeGreaterThanOrEqual(5);
  });
});
