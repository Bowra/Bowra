/**
 * canvas-update-turn batch B1-6 — the extra `canvas.opened{revised:true}` emitted at revision-turn completion only updates the title.
 *
 * It signals which report was revised (IM cards have no h1 section and need it for the first line), not lifecycle:
 * this turn has no `canvas.done` yet; reverting status from streaming to opened prematurely enables editing
 * and disables chat's missing-done fallback.
 */

import { beforeEach, describe, expect, it } from "vitest";

import {
  getCanvasSnapshot,
  onCanvasOpened,
  onSectionAdded,
  onSectionsSuperseded,
  resetCanvas,
} from "./canvas-store";
import type { ReportSection } from "./types";

const sec = (id: string, kind: string, extra: Record<string, unknown> = {}): ReportSection =>
  ({ id, kind, ...extra }) as unknown as ReportSection;

describe("canvas.opened(revised) (B1-6)", () => {
  beforeEach(() => resetCanvas());

  it("updates only the title, leaving streaming state and sections untouched", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1", title: "旧标题" });
    onSectionAdded({ section: sec("n1", "narrative", { markdown: "a" }), seq: 1 });
    const before = getCanvasSnapshot();
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1", title: "新口径标题", revised: true });
    const after = getCanvasSnapshot();
    expect(after.title).toBe("新口径标题");
    expect(after.status).toBe(before.status);
    expect(after.sections).toHaveLength(1);
    expect(after.skeletonStartIdx).toBe(before.skeletonStartIdx);
    expect(after.outlineReady).toBe(before.outlineReady);
  });

  it("ignores a revised frame for a different canvas", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1", title: "本画布" });
    onCanvasOpened({ canvas_id: "cv2", session_id: "s1", title: "别的画布", revised: true });
    const s = getCanvasSnapshot();
    expect(s.canvasId).toBe("cv1");
    expect(s.title).toBe("本画布");
  });
});

describe("canvas.sections_superseded folded_by (B1-2)", () => {
  beforeEach(() => resetCanvas());

  it("stamps _meta.folded_by so the current turn can tell replaced figures from prior reports", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1", title: "T" });
    onSectionAdded({ section: sec("figA", "chart", { spec: {} }), seq: 1 });
    onSectionAdded({ section: sec("oldH1", "h1", { text: "旧报告" }), seq: 2 });
    onSectionsSuperseded({ superseded_section_ids: ["figA"], superseded_by_turn: "t2", folded_by: "revise" });
    onSectionsSuperseded({ superseded_section_ids: ["oldH1"], superseded_by_turn: "t2" });
    const byId = new Map(getCanvasSnapshot().sections.map((x) => [(x as unknown as { id: string }).id, x]));
    const fig = byId.get("figA") as unknown as { superseded?: boolean; _meta?: { folded_by?: string } };
    const h1 = byId.get("oldH1") as unknown as { superseded?: boolean; _meta?: { folded_by?: string } };
    expect(fig.superseded).toBe(true);
    expect(fig._meta?.folded_by).toBe("revise");
    expect(h1.superseded).toBe(true);
    expect(h1._meta?.folded_by).toBeUndefined();
  });
});
