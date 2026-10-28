/**
 * #3 canvas-pane FLASH regression (has been re-fixed many times — keep it locked).
 *
 * Root invariant: the right pane is gated on `selectVisibleSections` (workspace.tsx
 * `hasCanvas`), NOT raw `canvas.sections`. Lead's buffered `sec_lead_*` sections must
 * never count as visible until response_kind is decided at `done`. If a future change
 * regresses the pane gate back to raw `sections.length`, the chat-mode turns below
 * would surface (visible > 0 mid-turn) → pane opens then vanishes = the flash.
 *
 * These tests assert the visible-sections signal (what the pane reads) across the full
 * turn lifecycle: stream → discard (chat) stays empty; outline / reveal (canvas) shows.
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  clearSectionsForChatTurn,
  getCanvasSnapshot,
  onCanvasOpened,
  onOutlineReady,
  onSectionStreaming,
  resetCanvas,
  revealBufferedSectionsForTurn,
  selectReportSurfaceSections,
  selectVisibleSections,
} from "./canvas-store";

const visibleCount = () => selectVisibleSections(getCanvasSnapshot()).length;
const reportSurfaceCount = () => selectReportSurfaceSections(getCanvasSnapshot()).length;

describe("canvas-store · #3 pane-flash regression", () => {
  afterEach(() => resetCanvas());

  it("chat turn: buffered sec_lead_* never count as visible, then discarded (no flash)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative", partial_payload: { markdown: "hi" } });
    onSectionStreaming({ section_id: "sec_lead_t1_002", kind: "narrative", partial_payload: { markdown: "more" } });
    // raw sections accumulate, but the PANE signal stays empty → pane never opens
    expect(getCanvasSnapshot().sections.length).toBe(2);
    expect(visibleCount()).toBe(0);
    // turn classified chat → discard → still nothing visible (no appear-then-vanish)
    clearSectionsForChatTurn("t1");
    expect(visibleCount()).toBe(0);
  });

  it("canvas turn (outline): non-buffered skeletons are visible immediately (pane opens once)", () => {
    onCanvasOpened({ canvas_id: "cv2", session_id: "s2" });
    onOutlineReady({ sections: [{ id: "sk_1", kind: "narrative" }, { id: "sk_2", kind: "kpi_row" }] });
    expect(visibleCount()).toBeGreaterThan(0);
  });

  it("canvas turn (lead narrative, no outline): hidden during turn, revealed at done", () => {
    onCanvasOpened({ canvas_id: "cv3", session_id: "s3" });
    onSectionStreaming({ section_id: "sec_lead_t3_001", kind: "narrative", partial_payload: { markdown: "report" } });
    expect(visibleCount()).toBe(0); // no premature pane open mid-turn
    revealBufferedSectionsForTurn("t3"); // response_kind=canvas at done
    expect(visibleCount()).toBeGreaterThan(0); // opens once, no flash
  });

  it("python/data-only turn stays in Chat while auxiliary content remains available", () => {
    onCanvasOpened({ canvas_id: "cv4", session_id: "s4" });
    onOutlineReady({
      sections: [
        { id: "sec_python", kind: "code_block" },
        { id: "sec_sql", kind: "code_block" },
        { id: "sec_rows", kind: "data_table" },
      ],
    });

    // Code/Data may stay in the workspace store; this must not automatically open the Report right pane.
    expect(visibleCount()).toBe(3);
    expect(reportSurfaceCount()).toBe(0);
  });
});
