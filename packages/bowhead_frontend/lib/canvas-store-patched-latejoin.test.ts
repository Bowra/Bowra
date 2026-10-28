/**
 * onSectionPatched late-join upsert (canvas-comprehensive Batch 1 WP-4 / B2/B3).
 * write_report update + sources-repatch emit section_patched carrying the FULL
 * section; a FE that never saw the original (dropped section_added / reorder /
 * reconnect) must INSERT it, not silently drop (F3).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  onSectionPatched,
  onSectionAdded,
  onCanvasOpened,
  getCanvasSnapshot,
  resetCanvas,
} from "./canvas-store";
import type { ReportSection } from "./types";

const narr = (id: string, markdown: string): ReportSection =>
  ({ id, kind: "narrative", markdown }) as unknown as ReportSection;

afterEach(() => {
  resetCanvas();
  vi.restoreAllMocks();
});

describe("onSectionPatched · late-join upsert (WP-4)", () => {
  it("existing section → merge patch", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({ section: narr("s1", "v1") });
    onSectionPatched({ section_id: "s1", patch: { markdown: "v2" }, rev: 2 });
    const sec = getCanvasSnapshot().sections[0] as unknown as { markdown: string; rev: number };
    expect(sec.markdown).toBe("v2");
    expect(sec.rev).toBe(2);
  });

  it("unseen section + patch carries full section (has kind) → insert", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    // FE never saw section_added for s9; patched arrives with the full section.
    onSectionPatched({
      section_id: "s9",
      patch: { id: "s9", kind: "sources", entries: [{ n: 1 }] },
      rev: 1,
    });
    const ids = getCanvasSnapshot().sections.map((s) => (s as unknown as { id: string }).id);
    expect(ids).toContain("s9");
  });

  it("unseen section + bare field patch (no kind) → warn, not silent drop", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionPatched({ section_id: "ghost", patch: { markdown: "x" } });
    expect(getCanvasSnapshot().sections.length).toBe(0); // not inserted (no kind)
    expect(warn).toHaveBeenCalled(); // but observable (F3), not silent
  });
});
