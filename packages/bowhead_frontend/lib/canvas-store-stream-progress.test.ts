/**
 * WP3 (design D6 + review): canvas-store reducer correctness; selectStreamProgress
 * progress selector + onCanvasReordered preserves sections.
 */

import { afterEach, describe, expect, it } from "vitest";

import type { ReportSection } from "./types";
import {
  getCanvasSnapshot,
  onCanvasDone,
  onCanvasOpened,
  onCanvasReordered,
  onOutlineReady,
  onSectionAdded,
  resetCanvas,
  selectStreamProgress,
} from "./canvas-store";

const narrative = (id: string, markdown?: string): ReportSection =>
  ({ id, kind: "narrative", ...(markdown ? { markdown } : {}) }) as unknown as ReportSection;

describe("canvas-store · selectStreamProgress (WP3 D6)", () => {
  afterEach(() => resetCanvas());

  it("idle → null, outline_ready seeds total, section_added advances done, done → null", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    expect(selectStreamProgress(getCanvasSnapshot())).toBeNull(); // No outline yet

    onOutlineReady({
      sections: [narrative("sec_a"), narrative("sec_b"), narrative("sec_c")].map(
        (s) => ({ id: s.id, kind: s.kind }),
      ),
    });
    // Outline state: three skeletons, zero filled
    expect(selectStreamProgress(getCanvasSnapshot())).toEqual({ done: 0, total: 3 });

    onSectionAdded({ section: narrative("sec_a", "第一节正文") });
    onSectionAdded({ section: narrative("sec_b", "第二节正文") });
    // Streaming state: 2/3
    expect(selectStreamProgress(getCanvasSnapshot())).toEqual({ done: 2, total: 3 });

    onSectionAdded({ section: narrative("sec_c", "第三节正文") });
    expect(selectStreamProgress(getCanvasSnapshot())).toEqual({ done: 3, total: 3 });

    onCanvasDone({ section_count: 3 });
    // Done state: hide progress
    expect(selectStreamProgress(getCanvasSnapshot())).toBeNull();
  });
});

describe("canvas-store · onCanvasReordered (WP3 review #6)", () => {
  afterEach(() => resetCanvas());

  it("order 没覆盖到的 section 追加保留, 不丢失", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({ section: narrative("a", "A") });
    onSectionAdded({ section: narrative("b", "B") });
    onSectionAdded({ section: narrative("c", "C") });

    // Backend supplied partial order (omitted c); c must survive
    onCanvasReordered({ order: ["b", "a"] });
    const ids = getCanvasSnapshot().sections.map((s) => (s as unknown as { id: string }).id);
    expect(ids).toEqual(["b", "a", "c"]); // Order applied + c appended and preserved
  });
});
