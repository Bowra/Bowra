/**
 * reconcileCanvasWithServer: final-state reconciliation (canvas-comprehensive Batch 1 WP-2 / TD-5 / FR-1.1).
 * Contract: server-wins convergence of sections + canvas metadata; **preserve pyCells**; count drift;
 * self-heal banners; clear this turn's buffered residue. Fast regression (actual evidence in canvas-cc/ mock-server e2e).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  reconcileCanvasWithServer,
  onSectionAdded,
  onCanvasOpened,
  onCanvasOpFailed,
  upsertPyCell,
  getCanvasSnapshot,
  resetCanvas,
  __reconcileStats,
} from "./canvas-store";
import type { ReportSection, PyCellSpec } from "./types";

const narr = (id: string, markdown: string): ReportSection =>
  ({ id, kind: "narrative", markdown }) as unknown as ReportSection;

afterEach(() => {
  resetCanvas();
  __reconcileStats.count = 0;
  __reconcileStats.lastDiff = 0;
  vi.restoreAllMocks();
});

const detail = (sections: ReportSection[]) => ({
  canvas: sections.length
    ? { id: "cv1", sessionId: "s1", title: "报告" }
    : null,
  sections,
});

describe("reconcileCanvasWithServer", () => {
  it("server 有段、本地缺段 → 收敛到 server (F5 自愈), drift 记 1", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({ section: narr("s_a", "A") }); // Only one local section (lost-frame scenario)
    reconcileCanvasWithServer(detail([narr("s_a", "A"), narr("s_b", "B")]), { turnId: "t1" });
    const snap = getCanvasSnapshot();
    expect(snap.sections.map((s) => (s as unknown as { id: string }).id)).toEqual(["s_a", "s_b"]);
    expect(__reconcileStats.count).toBe(1);
    expect(__reconcileStats.lastDiff).toBe(1); // Local s_b is missing
  });

  it("本地已一致 → drift 0 (健康, 无 warn)", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({ section: narr("s_a", "A") });
    onSectionAdded({ section: narr("s_b", "B") });
    reconcileCanvasWithServer(detail([narr("s_a", "A"), narr("s_b", "B")]), { turnId: "t1" });
    expect(__reconcileStats.lastDiff).toBe(0);
    expect(warn).not.toHaveBeenCalled();
  });

  it("保留 pyCells (Data/Code tab 不误伤)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    upsertPyCell({ n: 1, name: "c", status: "success", code: "x=1", output: "1", durMs: 5, turnId: "t1", subagentId: null } as PyCellSpec);
    reconcileCanvasWithServer(detail([narr("s_a", "A")]), { turnId: "t1" });
    expect(getCanvasSnapshot().pyCells.length).toBe(1); // pyCell survives
  });

  it("server 空 → server-wins 清空 sections (chat-mode / B7)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onSectionAdded({ section: narr("s_a", "A") });
    reconcileCanvasWithServer(detail([]), { turnId: "t1" });
    expect(getCanvasSnapshot().sections.length).toBe(0);
    expect(getCanvasSnapshot().status).toBe("idle");
  });

  it("banner 自愈: server 有段 → 清 lastCanvasOpFailed", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onCanvasOpFailed({ op: "compose", reasonCode: "lock_timeout", sectionId: null, at: 1 });
    expect(getCanvasSnapshot().lastCanvasOpFailed).not.toBeNull();
    reconcileCanvasWithServer(detail([narr("s_a", "A")]), { turnId: "t1" });
    expect(getCanvasSnapshot().lastCanvasOpFailed).toBeNull();
  });

  it("banner 不误清: server 空 → lastCanvasOpFailed 保留 (真失败, 无报告可解释)", () => {
    onCanvasOpened({ canvas_id: "cv1", session_id: "s1" });
    onCanvasOpFailed({ op: "compose", reasonCode: "lock_timeout", sectionId: null, at: 1 });
    reconcileCanvasWithServer(detail([]), { turnId: "t1" });
    expect(getCanvasSnapshot().lastCanvasOpFailed).not.toBeNull();
  });
});
