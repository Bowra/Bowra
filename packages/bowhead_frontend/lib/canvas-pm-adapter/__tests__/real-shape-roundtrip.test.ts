/**
 * A1-2 (report-edit-audit P0-1 root fix) — lossless roundtrip regression for real DB shapes.
 *
 * Production sections carry turn_id / style / rev / superseded and other keys PM nodes cannot represent
 * (fixtures omitted them, so old round-trip tests passed while missing the culprit). Text-based/recursive
 * node reconstruction lost those keys → shallowDiff's key union classified them as deleted → every real
 * narrative stayed changed:true → read-only flush validation always save_failed → exports refused;
 * the first edit-mode flush sent phantom PATCHes {turn_id: undefined, ...}, unnecessarily increasing rev.
 * Fix = canvas-to-pm stores all extra keys in meta.extras; pm-to-canvas spreads them back unchanged.
 */

import { describe, expect, test } from "vitest";
import { canvasToPMDoc } from "../canvas-to-pm";
import { pmDocToSections } from "../pm-to-canvas";
import type { ReportSection } from "@/lib/types";
import { ALL_SINGLE_FIXTURES, FIXTURE_section } from "./fixtures";

/** Add mandatory production extra keys to fixtures (narrative shape observed during walkthrough). */
function withRealShape(section: ReportSection): ReportSection {
  return {
    ...section,
    rev: 4,
    turn_id: "t_roundtrip_example",
    style: { tone: "compact" },
  } as unknown as ReportSection;
}

describe("real-shape roundtrip — 附加键不产生幽灵 diff", () => {
  test.each(ALL_SINGLE_FIXTURES.map((s) => [s.kind, s] as const))(
    "kind=%s: turn_id/style/rev 附加键 roundtrip 后 changed=false",
    (_kind, fixture) => {
      const section = withRealShape(fixture);
      const doc = canvasToPMDoc([section]);
      const patches = pmDocToSections(doc, [section]);
      expect(patches).toHaveLength(1);
      expect(patches[0].patch).toEqual({});
      expect(patches[0].changed).toBe(false);
    },
  );

  test("recursive section wrapper 附加键同样无损", () => {
    const section = withRealShape(FIXTURE_section);
    const doc = canvasToPMDoc([section]);
    const patches = pmDocToSections(doc, [section]);
    expect(patches[0].changed).toBe(false);
    expect(patches[0].patch).toEqual({});
  });

  test("narrative 空数组 unverified_numbers 不产生幽灵 diff", () => {
    const section = {
      id: "sec_n_empty",
      kind: "narrative",
      markdown: "正文一段。",
      unverified_numbers: [],
      turn_id: "t_x",
      rev: 2,
    } as unknown as ReportSection;
    const patches = pmDocToSections(canvasToPMDoc([section]), [section]);
    expect(patches[0].changed).toBe(false);
  });

  test("真实编辑仍被识别 (extras 不吞真 diff)", () => {
    const section = withRealShape({
      id: "sec_n_edit",
      kind: "narrative",
      markdown: "原文。",
    } as unknown as ReportSection);
    const doc = canvasToPMDoc([{ ...(section as object), markdown: "改过的文。" } as ReportSection]);
    const patches = pmDocToSections(doc, [section]);
    expect(patches[0].changed).toBe(true);
    expect(Object.keys(patches[0].patch)).toEqual(["markdown"]);
  });
});
