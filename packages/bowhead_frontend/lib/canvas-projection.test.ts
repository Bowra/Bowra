/**
 * canvas-projection — normalized projections & diff counts (canvas-comprehensive Batch 1 WP-0).
 * Projections determine terminal-reconciliation drift counts and "live==F5 diff==0" assertions; require:
 *   - Same content, different exempt fields (rev/turn_id/superseded_by_turn) → equal projections, diff==0
 *   - Changed content → diff>0
 *   - Changed superseded visual state → diff>0 (compared field, not exempt)
 */
import { describe, expect, it } from "vitest";

import { projectCanvasForDiff, diffProjections } from "./canvas-projection";
import type { ReportSection } from "./types";

const narr = (id: string, markdown: string, extra: Record<string, unknown> = {}): ReportSection =>
  ({ id, kind: "narrative", markdown, ...extra }) as unknown as ReportSection;

describe("canvas-projection", () => {
  it("同内容不同豁免字段 (rev/turn_id/superseded_by_turn) → diff==0", () => {
    const live = [narr("s1", "利润率 12%", { rev: 3, turn_id: "t_live" })];
    const server = [narr("s1", "利润率 12%", { rev: 7, turn_id: "t_server", superseded_by_turn: "t_x" })];
    expect(diffProjections(projectCanvasForDiff(live), projectCanvasForDiff(server))).toBe(0);
  });

  it("内容变 → diff>0", () => {
    const a = [narr("s1", "利润率 12%")];
    const b = [narr("s1", "利润率 15%")];
    expect(diffProjections(projectCanvasForDiff(a), projectCanvasForDiff(b))).toBe(1);
  });

  it("superseded 视觉状态变 → diff>0 (不豁免)", () => {
    const a = [narr("s1", "同内容", { superseded: false })];
    const b = [narr("s1", "同内容", { superseded: true })];
    expect(diffProjections(projectCanvasForDiff(a), projectCanvasForDiff(b))).toBe(1);
  });

  it("server 有 live 无 (丢帧场景) → diff>0", () => {
    const live = [narr("s1", "a")];
    const server = [narr("s1", "a"), narr("s2", "b")];
    expect(diffProjections(projectCanvasForDiff(live), projectCanvasForDiff(server))).toBe(1);
  });

  it("顺序变 → diff>0", () => {
    const a = [narr("s1", "a"), narr("s2", "b")];
    const b = [narr("s2", "b"), narr("s1", "a")];
    expect(diffProjections(projectCanvasForDiff(a), projectCanvasForDiff(b))).toBe(2);
  });

  it("key 序无关 (稳定序列化) → 同内容不同字段书写序 diff==0", () => {
    const a = [{ id: "s1", kind: "kpi_row", cells: [{ label: "GMV", value: "1亿" }] }] as unknown as ReportSection[];
    const b = [{ kind: "kpi_row", cells: [{ value: "1亿", label: "GMV" }], id: "s1" }] as unknown as ReportSection[];
    expect(diffProjections(projectCanvasForDiff(a), projectCanvasForDiff(b))).toBe(0);
  });

  it("空 vs 空 → diff==0", () => {
    expect(diffProjections(projectCanvasForDiff([]), projectCanvasForDiff([]))).toBe(0);
  });
});
