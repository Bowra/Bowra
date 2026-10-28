/**
 * canvas-update-turn batch B1-2 — split superseded sections into two paths:
 * old charts with `_meta.folded_by === "revise"` stay out of History (otherwise every revision marks the current report as superseded).
 */

import { describe, expect, it } from "vitest";

import { isRevisionFoldedSection, replacedFigureLabel, splitSupersededSections } from "./report-history-split";
import type { ReportSection } from "@/lib/types";

const sec = (id: string, kind: string, extra: Record<string, unknown> = {}): ReportSection =>
  ({ id, kind, superseded: true, turn_id: "t1", ...extra }) as unknown as ReportSection;

describe("splitSupersededSections (B1-2)", () => {
  it("routes revise-folded figures to `replaced`, everything else to `prior`", () => {
    const oldH1 = sec("h", "h1", { text: "旧报告" });
    const oldN = sec("n", "narrative", { markdown: "x" });
    const figA = sec("a", "chart", { spec: { title: "A 图" }, _meta: { folded_by: "revise" } });
    const codeA = sec("c", "code_block", { code: "select", _meta: { folded_by: "revise" } });
    const { prior, replaced } = splitSupersededSections([oldH1, figA, oldN, codeA]);
    expect(prior.map((s) => s.id)).toEqual(["h", "n"]);
    expect(replaced.map((s) => s.id)).toEqual(["a", "c"]);
  });

  it("ignores folded_by on non-superseded sections and other fold reasons", () => {
    const live = { ...sec("l", "chart", { _meta: { folded_by: "revise" } }), superseded: false } as ReportSection;
    const alpha = sec("x", "chart", { _meta: { folded_by: "alpha" } });
    expect(isRevisionFoldedSection(live)).toBe(false);
    expect(splitSupersededSections([alpha]).prior.map((s) => s.id)).toEqual(["x"]);
  });

  it("labels a replaced figure by caption, then spec title", () => {
    expect(replacedFigureLabel(sec("a", "chart", { caption: " 月度趋势 ", spec: { title: "x" } }))).toBe("月度趋势");
    expect(replacedFigureLabel(sec("b", "chart", { spec: { title: "季度对比" } }))).toBe("季度对比");
    expect(replacedFigureLabel(sec("c", "code_block", { code: "select" }))).toBe("");
  });
});
