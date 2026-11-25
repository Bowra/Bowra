// @vitest-environment node
/**
 * remove_report_section deletes a writer's own section on the server and emits
 * `canvas.section_removed`; the live Canvas drops it as reload and replay do.
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  getCanvasSnapshot,
  onCanvasOpened,
  onSectionAdded,
  onSectionRemoved,
  resetCanvas,
} from "./canvas-store";

describe("canvas-store · section removed", () => {
  afterEach(() => {
    resetCanvas();
  });

  it("drops only the removed section", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({ section: { id: "sec_new", kind: "narrative", markdown: "## Margins\n\nNew." } as never });
    onSectionAdded({ section: { id: "sec_old", kind: "narrative", markdown: "## Margins\n\nOld." } as never });

    onSectionRemoved({ section_id: "sec_old" });

    const ids = getCanvasSnapshot().sections.map((section) => (section as unknown as { id: string }).id);
    expect(ids).toEqual(["sec_new"]);
  });

  it("ignores an id the Canvas does not hold", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({ section: { id: "sec_a", kind: "narrative", markdown: "A" } as never });

    onSectionRemoved({ section_id: "sec_missing" });

    expect(getCanvasSnapshot().sections).toHaveLength(1);
  });
});
