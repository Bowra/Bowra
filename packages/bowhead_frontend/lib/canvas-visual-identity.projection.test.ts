import { describe, expect, it } from "vitest";

import { computeCanvasVisualInputSha256 } from "./canvas-visual-identity";
import type { ReportSection } from "./types";
import { chartStyleExportThemeFixture } from "@/__tests__/chart-style-export-fixture";

// R8 (2026-08-14, real device on 116): visual identity must be **insensitive to viewer projection**.
//
// Server project_sources_for_viewer rewrites chart spec.source_meta into
// viewer-safe source-ref-v1: alias objects, while canvas detail exposes
// visualInputSha256 computed from the **pre-projection** payload. Recalculation receives the projected
// section ⇒ equality never holds ⇒ all exact_visual receipts mismatch ⇒
// **V2 exports always downgrade to legacy** (116 production: 24/24 mismatches, no PDF charts, all raw floats).
//
// source_meta is not visual: export visual pages render only ChartBlock; source row ChartSourceLine
// is outside the screenshot. Matching server-side guard is in
// `packages/bowhead_core/tests/test_export_visual_identity_projection_invariance.py`。

const RAW_SOURCE_META = { id: "art_1", query: "secret", ordinal: 3 };
/** Actual server-projected shape (source-ref-v1: is a HMAC-signed alias within scope). */
const PROJECTED_SOURCE_META = {
  id: "source-ref-v1:usDBY1obolRpjAUoZZ-dA54H37VgU-Yt83FMuE3sqEY",
  source_alias: "source-ref-v1:usDBY1obolRpjAUoZZ-dA54H37VgU-Yt83FMuE3sqEY",
  ordinal: 3,
};

function chartSection(sourceMeta: unknown): ReportSection {
  return {
    id: "sec_chart",
    kind: "chart",
    rev: 1,
    spec: {
      kind: "bar",
      title: "头部品牌退款率对比",
      columns: [
        { key: "brand", header: "品牌", type: "categorical" },
        { key: "rate", header: "退款率", type: "numeric" },
      ],
      rows: [{ brand: "老庙", rate: 0.292 }],
      row_count: 1,
      source_meta: sourceMeta,
    },
  } as unknown as ReportSection;
}

const idOf = (section: ReportSection) =>
  computeCanvasVisualInputSha256({
    section,
    locale: "zh-CN",
    theme: chartStyleExportThemeFixture(),
  });

describe("R8 视觉身份对 viewer 投影不敏感", () => {
  it("includes saved shared domains while excluding permission-dependent note fields", async () => {
    const section = chartSection(RAW_SOURCE_META);
    const plan = { scale_groups: [{ view_ids: ["first", "second"], axis: "y", measure_keys: ["rate"], encoding: "bar", domain: [0, 1], scope_digest: "a".repeat(64) }], dataset: { availability: "available" } };
    (section as any).spec.presentation_plan = plan;
    const original = await idOf(section);
    plan.dataset.availability = "unavailable";
    expect(await idOf(section)).toBe(original);
    plan.scale_groups[0].domain[1] = 2;
    expect(await idOf(section)).not.toBe(original);
  });
  it("★source_meta 被投影改写后，身份不变（这条红 = V2 导出全线降级）", async () => {
    const raw = await idOf(chartSection(RAW_SOURCE_META));
    const projected = await idOf(chartSection(PROJECTED_SOURCE_META));
    expect(raw).not.toBeNull();
    expect(projected).toBe(raw);
  });

  it("★source_meta 整个缺失也算同一张图（老 section 没有这个字段）", async () => {
    const withMeta = await idOf(chartSection(RAW_SOURCE_META));
    const section = chartSection(RAW_SOURCE_META);
    delete (section as any).spec.source_meta;
    expect(await idOf(section)).toBe(withMeta);
  });

  it("★反向：真进画面的东西改了，身份必须变（别把身份做成常量）", async () => {
    const base = await idOf(chartSection(RAW_SOURCE_META));

    const rows = chartSection(RAW_SOURCE_META);
    (rows as any).spec.rows = [{ brand: "老庙", rate: 0.4 }];
    expect(await idOf(rows)).not.toBe(base);

    const kind = chartSection(RAW_SOURCE_META);
    (kind as any).spec.kind = "line";
    expect(await idOf(kind)).not.toBe(base);

    const title = chartSection(RAW_SOURCE_META);
    (title as any).spec.title = "别的标题";
    expect(await idOf(title)).not.toBe(base);

    const header = chartSection(RAW_SOURCE_META);
    (header as any).spec.columns[1].header = "退款率（口径二）";
    expect(await idOf(header)).not.toBe(base);
  });
});
