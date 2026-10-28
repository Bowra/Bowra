/**
 * Visibility-model invariants (canvas-comprehensive WP-3 + review-fix P0).
 *
 * P0 confirmed by three perspectives: canvas.done precedes turn done (backend canvas.done → D6
 * sections_hidden → done). Revealing buffered content in onCanvasDone at canvas.done would:
 *   ① Flash chat-mode sections before sections_hidden arrives;
 *   ② Drain buffered early, making turn done's reveal-vs-clear decision see no content and clear report sections.
 * Fix: **onCanvasDone does not reveal**; reveal only at turn termination (use-turn-runner done handler,
 * after sections_hidden). This test enforces that, preventing reveal from being added back to onCanvasDone.
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  getCanvasSnapshot,
  onCanvasDone,
  onCanvasOpened,
  onSectionStreaming,
  revealBufferedSectionsForTurn,
  resetCanvas,
  selectVisibleSections,
} from "./canvas-store";

const visibleCount = () => selectVisibleSections(getCanvasSnapshot()).length;
const bufferedSize = () => getCanvasSnapshot().bufferedLeadSectionIds.size;

afterEach(() => resetCanvas());

describe("canvas-store · visibility model (P0 lock)", () => {
  it("onCanvasDone does NOT reveal buffered (canvas.done precedes sections_hidden)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative", partial_payload: { markdown: "报告" } });
    expect(visibleCount()).toBe(0);
    expect(bufferedSize()).toBe(1);
    // canvas.done arrived: never reveal (chat-mode flash + drained buffer would make done clear content incorrectly).
    onCanvasDone({ errors: [], turn_id: "t1" });
    expect(bufferedSize()).toBe(1); // buffered unchanged
    expect(visibleCount()).toBe(0); // Still invisible
  });

  it("reveal happens at turn done (revealBufferedSectionsForTurn) — the only reveal path", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative", partial_payload: { markdown: "报告" } });
    onCanvasDone({ errors: [], turn_id: "t1" }); // status only, no reveal
    expect(visibleCount()).toBe(0);
    // Only turn done handler reveals here (after sections_hidden).
    revealBufferedSectionsForTurn("t1");
    expect(visibleCount()).toBeGreaterThan(0);
  });

  it("canvas.done still handles fallback-empty idle (no reveal side-effect)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onCanvasDone({ errors: [], source: "turn_done_chokepoint_fallback", section_count: 0, turn_id: "t1" });
    expect(getCanvasSnapshot().status).toBe("idle");
  });
});
