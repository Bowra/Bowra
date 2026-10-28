/**
 * **Reference stability** of derived canvas sections: the broken link behind production React #185.
 *
 * Observed on 116 prod v1.0.83: one execution turn emitted **32**
 * Maximum update depth exceeded errors (verified react-dom production #185 follows
 * nestedUpdateCount). Cause chain (spike §2.4):
 *
 * ```
 * chat-workbench subscribes to whole canvas state → rerenders on every commit
 *   → reportSections computed inline without memo → **new array on every render**
 *   → ReportView canonicalSections is root memo dependency → invalidates entire memo chain
 *   → report-view useEffect(…setState…, [initialDoc, …]) runs each render → loop
 * ```
 *
 * Exactly the second cause named in React dev error copy:
 * useEffect … **or one of the dependencies changes on every render**.
 *
 * ⚠ Enforce **reference**, not content. toEqual (value equality) **enforces nothing**:
 * an unmemoized implementation returns equally valued new arrays, retaining the loop. Use toBe.
 *
 * Mutation guard: remove hook useMemo (return selector directly) → both ★ tests must fail.
 */
import { describe, it, expect } from "vitest";
import { renderHook } from "@testing-library/react";

import { useReportSurfaceSections, useVisibleSections } from "./use-canvas-derived-sections";
import type { CanvasState } from "./canvas-store";

// ⚠ Hoist sections to module scope: rebuilding arrays in state() makes the test changing only
// bufferedLeadSectionIds **falsely pass** (actual invalidation comes from sections reference changes).
const SECTIONS = [
  { id: "s1", kind: "h1", text: "标题" },
  { id: "s2", kind: "narrative", markdown: "正文" },
  { id: "s3", kind: "code_block", call_id: "tl_1", lang: "sql", code: "select 1" },
];

// ⚠ Default to **nonempty** bufferedLeadSectionIds: when empty, selectVisibleSections
// returns s.sections early, accidentally stable without memo; with an empty fixture,
// removing memo never fails (initial implementation missed this until mutation checks).
function state(over: Partial<CanvasState> = {}): CanvasState {
  return {
    sections: SECTIONS,
    bufferedLeadSectionIds: new Set<string>(["s_buffered"]),
    ...over,
  } as unknown as CanvasState;
}

describe("canvas 派生 section — 引用稳定（断 #185 的那一环）", () => {
  it("★ canvas 未变时**重渲返回同一引用**（用 toBe，不是 toEqual）", () => {
    const s = state();
    const { result, rerender } = renderHook(() => useReportSurfaceSections(s));
    const first = result.current;
    rerender();
    rerender();
    expect(result.current).toBe(first);
  });

  it("★ exportSections 同样稳定（此前在 JSX 里内联调用，每渲染新数组）", () => {
    const s = state();
    const { result, rerender } = renderHook(() => useVisibleSections(s));
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });

  it("sections 真变了要失效（回归面：不能为了稳定把内容更新挡掉）", () => {
    let s = state();
    const { result, rerender } = renderHook(() => useReportSurfaceSections(s));
    const first = result.current;
    s = state({ sections: [...SECTIONS, { id: "s4", kind: "narrative", markdown: "新段" }] as never });
    rerender();
    expect(result.current).not.toBe(first);
    expect(result.current.length).toBe(first.length + 1);
  });

  it("bufferedLeadSectionIds 变了也要失效（它是 selectVisibleSections 的另一个输入）", () => {
    let s = state();
    const { result, rerender } = renderHook(() => useVisibleSections(s));
    const first = result.current;
    // **Preserve** sections reference, changing only bufferedLeadSectionIds; otherwise test measures
    // sections reference changes and passes even with missing dependency (caught by initial mutation checks).
    s = state({ bufferedLeadSectionIds: new Set(["s2"]) as never });
    rerender();
    expect(result.current).not.toBe(first);
    expect(result.current.some((x) => (x as { id: string }).id === "s2")).toBe(false);
  });

  it("报告面滤掉 SQL/代码/裸表（原 selector 语义不能被包装改变）", () => {
    const { result } = renderHook(() => useReportSurfaceSections(state()));
    expect(result.current.map((x) => (x as { kind: string }).kind)).toEqual(["h1", "narrative"]);
  });
});
