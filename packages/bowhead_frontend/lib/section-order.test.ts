/**
 * A3 (report-edit-audit) — display-order → full database-order mapping.
 *
 * The editor has only 6 top-level nodes (charts inlined into narrative, code_block folded into the appendix); sending only those 6
 * to /reorder makes other sections' seq values collide (P1-1 root cause). Preserve replacement-in-place semantics here:
 * slots of inline/folded blocks stay fixed; user reordering applies only among visible blocks.
 */

import { describe, expect, it } from "vitest";
import { mapDisplayOrderToFullOrder } from "./section-order";

const FULL = ["code1", "chart1", "eyebrow", "h1", "meta", "kpi", "narrative", "sources"];
const DISPLAY = ["eyebrow", "h1", "meta", "kpi", "narrative", "sources"];

describe("mapDisplayOrderToFullOrder", () => {
  it("展示序未变 → 全量序原样", () => {
    expect(mapDisplayOrderToFullOrder(FULL, DISPLAY)).toEqual(FULL);
  });

  it("用户把 kpi 拖到 h1 前 → 只在可见槽位间换, 内联/折叠块槽位不动", () => {
    const dragged = ["eyebrow", "kpi", "h1", "meta", "narrative", "sources"];
    expect(mapDisplayOrderToFullOrder(FULL, dragged)).toEqual([
      "code1",
      "chart1",
      "eyebrow",
      "kpi",
      "h1",
      "meta",
      "narrative",
      "sources",
    ]);
  });

  it("内联图与折叠代码块不会被挤到尾部", () => {
    const out = mapDisplayOrderToFullOrder(FULL, ["sources", "narrative", "kpi", "meta", "h1", "eyebrow"]);
    expect(out[0]).toBe("code1");
    expect(out[1]).toBe("chart1");
    expect(out).toHaveLength(FULL.length);
    expect(new Set(out)).toEqual(new Set(FULL));
  });

  it("展示集与库序数量对不齐(流式/刚增删) → 退回库序交由服务端补全", () => {
    expect(mapDisplayOrderToFullOrder(FULL, ["eyebrow", "h1"])).toEqual(FULL);
  });

  it("展示序里的陌生 id 被忽略而不是塞进库序", () => {
    const withGhost = [...DISPLAY.slice(0, 5), "ghost", "sources"];
    expect(mapDisplayOrderToFullOrder(FULL, withGhost)).toEqual(FULL);
  });

  it("空展示序 → 库序不动", () => {
    expect(mapDisplayOrderToFullOrder(FULL, [])).toEqual(FULL);
  });
});
