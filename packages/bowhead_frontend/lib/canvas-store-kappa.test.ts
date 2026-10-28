/**
 * Sprint κ Phase C — canvas-store unit tests for streaming path.
 *
 * Per tech-design-r3 + PRD r2 §4.4.1 / §4.4.4:
 * - currentlyStreamingSectionId tracks most-recent streaming section
 * - pruneStreamingSections clears streaming-state sections (cancel UX)
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  clearSectionsForChatTurn,
  getCanvasSnapshot,
  hasBufferedCanvasContentForTurn,
  onCanvasDone,
  onCanvasOpened,
  onSectionAdded,
  onSectionChunk,
  onSectionDone,
  onSectionStreaming,
  pruneStreamingSections,
  resetCanvas,
  revealBufferedSectionsForTurn,
  selectVisibleSections,
} from "./canvas-store";

describe("canvas-store · Sprint κ streaming", () => {
  afterEach(() => {
    resetCanvas();
  });

  it("onSectionStreaming sets currentlyStreamingSectionId", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({
      section_id: "sec_lead_t1_001",
      kind: "narrative",
      partial_payload: { markdown: "" },
    });
    expect(getCanvasSnapshot().currentlyStreamingSectionId).toBe("sec_lead_t1_001");
    expect(getCanvasSnapshot().streamingSectionIds.has("sec_lead_t1_001")).toBe(true);
  });

  it("subsequent onSectionStreaming updates currentlyStreaming to latest", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "a", kind: "narrative" });
    onSectionStreaming({ section_id: "b", kind: "narrative" });
    expect(getCanvasSnapshot().currentlyStreamingSectionId).toBe("b");
    // Both still in streaming set
    expect(getCanvasSnapshot().streamingSectionIds.size).toBe(2);
  });

  it("onSectionDone clears currentlyStreaming if it matches", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "a", kind: "narrative" });
    onSectionDone({ section_id: "a", payload: { markdown: "Done." } });
    expect(getCanvasSnapshot().currentlyStreamingSectionId).toBeNull();
    expect(getCanvasSnapshot().streamingSectionIds.has("a")).toBe(false);
  });

  it("onSectionDone keeps currentlyStreaming if other section closes", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "a", kind: "narrative" });
    onSectionStreaming({ section_id: "b", kind: "narrative" });
    onSectionDone({ section_id: "a", payload: { markdown: "A done" } });
    expect(getCanvasSnapshot().currentlyStreamingSectionId).toBe("b");
  });

  it("duplicate section_added after section_done does not reopen streaming", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    const donePayload = {
      spec: { kind: "line", title: "月度趋势", rows: [], columns: [], row_count: 0 },
      id: "chart_1",
      kind: "chart",
      rev: 1,
    };
    const sectionAddedPayload = {
      spec: { kind: "line", title: "月度趋势", rows: [], columns: [], row_count: 0 },
      id: "chart_1",
      kind: "chart",
      rev: 1,
    };
    onSectionStreaming({ section_id: "chart_1", kind: "chart" });
    onSectionDone({ section_id: "chart_1", payload: donePayload });
    onCanvasDone({});
    const before = getCanvasSnapshot();
    onSectionAdded({ section: sectionAddedPayload as never });
    const after = getCanvasSnapshot();
    expect(after.status).toBe("done");
    expect(after.sections).toBe(before.sections);
  });

  it("onSectionChunk appends delta to section markdown", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({
      section_id: "a", kind: "narrative",
      partial_payload: { markdown: "" },
    });
    onSectionChunk({ section_id: "a", delta: "Hello " });
    onSectionChunk({ section_id: "a", delta: "world." });
    const sec = getCanvasSnapshot().sections.find((s) => s.id === "a");
    // narrative section accumulates markdown
    expect((sec as { markdown: string }).markdown).toBe("Hello world.");
  });

  it("pruneStreamingSections removes streaming-state sections only", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    // One streaming + one done section
    onSectionStreaming({ section_id: "streaming_one", kind: "narrative" });
    onSectionAdded({
      section: {
        id: "done_one", kind: "narrative", markdown: "static content",
      } as never,
    });
    pruneStreamingSections();
    const ids = getCanvasSnapshot().sections.map((s) => s.id);
    expect(ids).toContain("done_one");
    expect(ids).not.toContain("streaming_one");
    expect(getCanvasSnapshot().streamingSectionIds.size).toBe(0);
    expect(getCanvasSnapshot().currentlyStreamingSectionId).toBeNull();
  });

  it("pruneStreamingSections is no-op when nothing streaming", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({
      section: { id: "x", kind: "narrative", markdown: "stable" } as never,
    });
    const before = getCanvasSnapshot().sections.length;
    pruneStreamingSections();
    expect(getCanvasSnapshot().sections.length).toBe(before);
  });
});

describe("canvas-store · canvas-pipeline-fix B1 buffer mode", () => {
  afterEach(() => {
    resetCanvas();
  });

  it("sec_lead_* section_streaming → added to bufferedLeadSectionIds", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({
      section_id: "sec_lead_t1_001", kind: "narrative",
      partial_payload: { markdown: "" },
    });
    expect(getCanvasSnapshot().bufferedLeadSectionIds.has("sec_lead_t1_001"))
      .toBe(true);
  });

  it("non-Lead section_streaming → NOT added to bufferedLeadSectionIds", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({
      section_id: "sec_compute_xyz", kind: "insight_card",
      partial_payload: {},
    });
    expect(getCanvasSnapshot().bufferedLeadSectionIds.has("sec_compute_xyz"))
      .toBe(false);
  });

  it("selectVisibleSections hides Lead-buffered sections, shows others", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative" });
    onSectionAdded({
      section: { id: "sec_payload_x", kind: "narrative",
                 markdown: "payload prose" } as never,
    });
    const visible = selectVisibleSections(getCanvasSnapshot());
    const ids = visible.map((s) => s.id);
    expect(ids).not.toContain("sec_lead_t1_001");
    expect(ids).toContain("sec_payload_x");
  });

  it("revealBufferedSectionsForTurn moves Lead sections out of buffer", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative" });
    onSectionStreaming({ section_id: "sec_lead_t1_002", kind: "narrative" });
    onSectionStreaming({ section_id: "sec_lead_t9_999", kind: "narrative" });
    revealBufferedSectionsForTurn("t1");
    const buffered = getCanvasSnapshot().bufferedLeadSectionIds;
    expect(buffered.has("sec_lead_t1_001")).toBe(false);
    expect(buffered.has("sec_lead_t1_002")).toBe(false);
    // Leave other turns' buffered content unchanged
    expect(buffered.has("sec_lead_t9_999")).toBe(true);
    const visible = selectVisibleSections(getCanvasSnapshot()).map((s) => s.id);
    expect(visible).toContain("sec_lead_t1_001");
    expect(visible).toContain("sec_lead_t1_002");
    expect(visible).not.toContain("sec_lead_t9_999");
  });

  it("clearSectionsForChatTurn drops sections + buffered ids for same turn", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative" });
    onSectionChunk({ section_id: "sec_lead_t1_001", delta: "chat-mode prose" });
    clearSectionsForChatTurn("t1");
    const s = getCanvasSnapshot();
    expect(s.sections.find((x) => x.id === "sec_lead_t1_001")).toBeUndefined();
    expect(s.bufferedLeadSectionIds.has("sec_lead_t1_001")).toBe(false);
  });

  it("revealBufferedSectionsForTurn empty turnId is no-op", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative" });
    revealBufferedSectionsForTurn("");
    expect(getCanvasSnapshot().bufferedLeadSectionIds.size).toBe(1);
  });
});

// ─── chat-truth Phase Q — terminal-path coverage on canvas buffer ─────
describe("canvas-store · chat-truth Phase Q terminal reveal", () => {
  afterEach(() => {
    resetCanvas();
  });

  it("Q-T1 reveal canvas-mode done drains buffered set", () => {
    // Simulate canvas-mode done: Lead streamed one sec_lead_*; reveal should drain buffered set,
    // retaining sections array (content visible).
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative" });
    onSectionChunk({ section_id: "sec_lead_t1_001", delta: "lead prose" });
    revealBufferedSectionsForTurn("t1");
    const s = getCanvasSnapshot();
    expect(s.bufferedLeadSectionIds.has("sec_lead_t1_001")).toBe(false);
    expect(s.sections.find((x) => x.id === "sec_lead_t1_001")).toBeTruthy();
    const visible = selectVisibleSections(s).map((x) => x.id);
    expect(visible).toContain("sec_lead_t1_001");
  });

  it("Q-T2 turn_failed retains canvas via reveal when buffered has content", () => {
    // Simulate turn_failed: buffered has sec_lead_t1_*, predicate returns true,
    // use reveal (not clear) → sections remain and are visible.
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative" });
    onSectionChunk({ section_id: "sec_lead_t1_001", delta: "partial" });
    const snap = getCanvasSnapshot();
    expect(hasBufferedCanvasContentForTurn(snap, "t1")).toBe(true);
    revealBufferedSectionsForTurn("t1");
    const visible = selectVisibleSections(getCanvasSnapshot()).map((x) => x.id);
    expect(visible).toContain("sec_lead_t1_001");
  });

  it("Q-T3 turn_aborted retains partial canvas", () => {
    // Simulate turn_aborted: pruneStreamingSections first removes streaming-state, but
    // done-state buffered remains; reveal makes it visible.
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative" });
    onSectionChunk({ section_id: "sec_lead_t1_001", delta: "first half" });
    // section_done before abort → leaves streamingSectionIds, remains in bufferedLead
    onSectionDone({
      section_id: "sec_lead_t1_001",
      payload: { markdown: "first half" },
    });
    // turn_aborted path: prune then reveal
    pruneStreamingSections();
    expect(hasBufferedCanvasContentForTurn(getCanvasSnapshot(), "t1")).toBe(true);
    revealBufferedSectionsForTurn("t1");
    expect(selectVisibleSections(getCanvasSnapshot()).length).toBeGreaterThan(0);
    const ids = selectVisibleSections(getCanvasSnapshot()).map((x) => x.id);
    expect(ids).toContain("sec_lead_t1_001");
  });

  it("Q-T4 chat-mode done discards (regression guard)", () => {
    // chat-mode done still uses clearSectionsForChatTurn → clears all sections + buffered.
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionStreaming({ section_id: "sec_lead_t1_001", kind: "narrative" });
    onSectionChunk({ section_id: "sec_lead_t1_001", delta: "chat prose" });
    clearSectionsForChatTurn("t1");
    const s = getCanvasSnapshot();
    expect(s.sections.find((x) => x.id === "sec_lead_t1_001")).toBeUndefined();
    expect(s.bufferedLeadSectionIds.has("sec_lead_t1_001")).toBe(false);
  });

  it("Q-T5 failed with no canvas content → clears (no orphan reveal)", () => {
    // Turn produced no sec_lead_* (e.g. abort before failed tool call); predicate returns false,
    // take clear path — avoid wasting a no-op reveal of an empty set.
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    // No onSectionStreaming call, only onCanvasOpened
    const snap = getCanvasSnapshot();
    expect(hasBufferedCanvasContentForTurn(snap, "t1")).toBe(false);
    // Verify hasBufferedCanvasContentForTurn does not misclassify other turns' buffered IDs
    onSectionStreaming({ section_id: "sec_lead_tOTHER_001", kind: "narrative" });
    expect(hasBufferedCanvasContentForTurn(getCanvasSnapshot(), "t1")).toBe(false);
    expect(hasBufferedCanvasContentForTurn(getCanvasSnapshot(), "tOTHER")).toBe(true);
  });

  it("Q-T-extra empty turnId → false", () => {
    expect(hasBufferedCanvasContentForTurn(getCanvasSnapshot(), "")).toBe(false);
  });
});
