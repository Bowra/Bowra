/**
 * feedback-0713 F1 — lock the setContent admission-gate semantics (gateReportSetContent).
 *
 * Key invariant: an open overlay means "defer" (do not mark initialDoc as seen; rerun after closing
 * and apply normally), not "skip-mark-seen" (mark as seen). Changing defer to mark-seen
 * permanently loses updates arriving during the overlay; moving the guard after mark-seen lets document rebuilding invalidate the captured
 * selection midway through the three formatting actions (device reproduction: font size affects 1 character / serif is a no-op).
 */
import { describe, expect, it } from "vitest";

import { gateReportSetContent } from "./report-view";

describe("gateReportSetContent", () => {
  it("选区 overlay 开着 → defer (格式三连期间不重建 doc)", () => {
    expect(
      gateReportSetContent({ overlayKind: "selection-toolbar", forced: false, dirtyCount: 0 }),
    ).toBe("defer");
    expect(
      gateReportSetContent({ overlayKind: "block-ai", forced: false, dirtyCount: 0 }),
    ).toBe("defer");
  });

  it("overlay 开着且有脏账 → 仍是 defer (defer 优先, 不消耗 mark-seen)", () => {
    expect(
      gateReportSetContent({ overlayKind: "selection-toolbar", forced: false, dirtyCount: 3 }),
    ).toBe("defer");
  });

  it("forced (冲突重载/回滚, 本就要丢本地) → 两道门都不拦", () => {
    expect(
      gateReportSetContent({ overlayKind: "selection-toolbar", forced: true, dirtyCount: 0 }),
    ).toBe("apply");
    expect(
      gateReportSetContent({ overlayKind: null, forced: true, dirtyCount: 5 }),
    ).toBe("apply");
  });

  it("无 overlay 有脏账 → skip-mark-seen (#3 既有语义不变)", () => {
    expect(
      gateReportSetContent({ overlayKind: null, forced: false, dirtyCount: 1 }),
    ).toBe("skip-mark-seen");
  });

  it("无 overlay 无脏账 → apply", () => {
    expect(
      gateReportSetContent({ overlayKind: null, forced: false, dirtyCount: 0 }),
    ).toBe("apply");
  });
});
