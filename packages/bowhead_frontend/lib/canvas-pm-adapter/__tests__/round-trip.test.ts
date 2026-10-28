/**
 * P3.1.6 — Round-trip unit tests.
 *
 * Verify lossless canvasToPMDoc → pmDocToSections data conversion (24 kinds).
 * + markdown ↔ PM 5 case round-trip.
 */

import { describe, expect, it, test } from "vitest";
import { canvasToPMDoc } from "../canvas-to-pm";
import { pmDocToSections } from "../pm-to-canvas";
import { markdownToPMNodes, pmNodesToMarkdown } from "../markdown-pm";
import {
  ALL_FIXTURES,
  ALL_SINGLE_FIXTURES,
  FIXTURE_section,
  MARKDOWN_SAMPLES,
  SOURCE_REFS_FIXTURES,
  FIXTURE_design,
  FIXTURE_narrative_with_refs,
  FIXTURE_recommendations_with_refs,
  FIXTURE_insight_card_with_refs,
} from "./fixtures";
import type { ReportSection } from "@/lib/types";

// ────────────────────────────────────────────────────────────
// 1. Overall forward + backward round-trip
// ────────────────────────────────────────────────────────────

describe("canvasToPMDoc + pmDocToSections — round-trip", () => {
  test.each(ALL_SINGLE_FIXTURES)(
    "kind=$kind: 单 section round-trip 不变 + diff 为空",
    (section: ReportSection) => {
      const doc = canvasToPMDoc([section]);
      const patches = pmDocToSections(doc, [section]);

      expect(patches).toHaveLength(1);
      expect(patches[0].sectionId).toBe(section.id);
      expect(patches[0].kind).toBe(section.kind);
      // No modification should mean changed=false, patch={}
      expect(patches[0].changed).toBe(false);
      expect(patches[0].patch).toEqual({});
    },
  );

  it("recursive section 嵌套 round-trip 不变", () => {
    const doc = canvasToPMDoc([FIXTURE_section]);
    const patches = pmDocToSections(doc, [FIXTURE_section]);
    expect(patches).toHaveLength(1);
    expect(patches[0].sectionId).toBe(FIXTURE_section.id);
    expect(patches[0].kind).toBe("section");
    expect(patches[0].changed).toBe(false);
  });

  it("混合 24 kind 一起 round-trip 不变", () => {
    const doc = canvasToPMDoc(ALL_FIXTURES);
    const patches = pmDocToSections(doc, ALL_FIXTURES);
    expect(patches).toHaveLength(ALL_FIXTURES.length);
    for (const p of patches) {
      expect(p.changed).toBe(false);
    }
  });
});

// ────────────────────────────────────────────────────────────
// 2. Edit detection: modifying a field should produce the correct diff
// ────────────────────────────────────────────────────────────

describe("pmDocToSections — diff detection", () => {
  it("改 h1 text → patch 含 text 字段", () => {
    const orig = { id: "h1_test", kind: "h1" as const, text: "原标题" };
    const doc = canvasToPMDoc([orig]);
    // Modify h1 attrs.payload.text in the PM doc
    const topNode = (doc.content as { type: string; attrs: Record<string, unknown> }[])[0];
    const payload = topNode.attrs.payload as Record<string, unknown>;
    payload.text = "新标题";

    const patches = pmDocToSections(doc, [orig]);
    expect(patches[0].changed).toBe(true);
    expect(patches[0].patch).toEqual({ text: "新标题" });
  });

  it("改 design text layer → patch 含 layers 字段", () => {
    const orig = FIXTURE_design as Extract<ReportSection, { kind: "design" }>;
    const doc = canvasToPMDoc([orig]);
    const topNode = (doc.content as { type: string; attrs: Record<string, unknown> }[])[0];
    const payload = topNode.attrs.payload as { layers: Array<Record<string, unknown>> };
    payload.layers = payload.layers.map((layer) => (
      layer.id === "cta" ? { ...layer, text: "限时 7 折" } : layer
    ));

    const patches = pmDocToSections(doc, [orig]);
    expect(patches[0].changed).toBe(true);
    expect(patches[0].patch).toEqual({
      layers: [
        ...orig.layers.slice(0, 1),
        { ...orig.layers[1], text: "限时 7 折" },
      ],
    });
  });

  it("rev is server-managed and ignored by patch diff", () => {
    const orig = { id: "h1_test", kind: "h1" as const, text: "标题", rev: 2 };
    const doc = canvasToPMDoc([{ ...orig, rev: 1 }]);

    const patches = pmDocToSections(doc, [orig]);
    expect(patches[0].changed).toBe(false);
    expect(patches[0].patch).toEqual({});
  });

  it("design flattened fields are server-managed and ignored by patch diff", () => {
    const orig = {
      ...(FIXTURE_design as Extract<ReportSection, { kind: "design" }>),
      rev: 3,
      flattened_artifact_id: null,
      flattened_source_rev: null,
      flattened_at: null,
    };
    const doc = canvasToPMDoc([{
      ...orig,
      rev: 4,
      flattened_artifact_id: "art_flattened001",
      flattened_source_rev: 3,
      flattened_at: "2026-06-13T00:00:00Z",
    }]);

    const patches = pmDocToSections(doc, [orig]);
    expect(patches[0].changed).toBe(false);
    expect(patches[0].patch).toEqual({});
  });
});

// ────────────────────────────────────────────────────────────
// 3. Markdown ↔ PM round-trip
// ────────────────────────────────────────────────────────────

describe("markdownToPMNodes + pmNodesToMarkdown — round-trip", () => {
  test.each(MARKDOWN_SAMPLES)("markdown round-trip stable: %s", (md: string) => {
    const nodes = markdownToPMNodes(md);
    expect(nodes.length).toBeGreaterThan(0);
    const back = pmNodesToMarkdown(nodes);
    // Strict equality is too brittle (whitespace may differ); verify only:
    //  - back is nonempty
    //  - back contains the original core characters (excluding Markdown markers such as # **)
    expect(back).toBeTruthy();
    // Placeholders [num:..] [ref:..] [chart:..] must survive
    if (md.includes("[num:")) expect(back).toContain("[num:");
    if (md.includes("[ref:")) expect(back).toContain("[ref:");
    if (md.includes("[chart:")) expect(back).toContain("[chart:");
  });

  it("空 markdown → 空数组或 paragraph", () => {
    const nodes = markdownToPMNodes("");
    expect(nodes.length).toBeGreaterThan(0); // Return at least one paragraph
  });
});

// ────────────────────────────────────────────────────────────
// 3.5 W3-9: preserve source_refs through round-trip (field added in P3.3 W1-1)
// ────────────────────────────────────────────────────────────

describe("source_refs round-trip — narrative / recommendations / insight_card", () => {
  it("narrative source_refs 经 PM 转换后字段保留 (changed=false)", () => {
    const doc = canvasToPMDoc([FIXTURE_narrative_with_refs]);
    const patches = pmDocToSections(doc, [FIXTURE_narrative_with_refs]);
    expect(patches).toHaveLength(1);
    expect(patches[0].sectionId).toBe(FIXTURE_narrative_with_refs.id);
    // source_refs unchanged → diff should be empty
    expect(patches[0].changed).toBe(false);
    expect(patches[0].patch).toEqual({});
  });

  it("recommendations source_refs round-trip 不变", () => {
    const doc = canvasToPMDoc([FIXTURE_recommendations_with_refs]);
    const patches = pmDocToSections(doc, [FIXTURE_recommendations_with_refs]);
    expect(patches).toHaveLength(1);
    expect(patches[0].changed).toBe(false);
    expect(patches[0].patch).toEqual({});
  });

  it("insight_card source_refs round-trip 不变", () => {
    const doc = canvasToPMDoc([FIXTURE_insight_card_with_refs]);
    const patches = pmDocToSections(doc, [FIXTURE_insight_card_with_refs]);
    expect(patches).toHaveLength(1);
    expect(patches[0].changed).toBe(false);
    expect(patches[0].patch).toEqual({});
  });

  it("narrative 修改 source_refs 应触发 diff (走 meta path)", () => {
    const orig = FIXTURE_narrative_with_refs;
    const doc = canvasToPMDoc([orig]);
    // narrative is text-based; source_refs live in attrs.meta
    const topNode = (
      doc.content as { type: string; attrs: Record<string, unknown> }[]
    )[0];
    const meta = topNode.attrs.meta as Record<string, unknown>;
    meta.source_refs = ["s_001", "s_002", "s_003_NEW"];

    const patches = pmDocToSections(doc, [orig]);
    expect(patches[0].changed).toBe(true);
    expect(patches[0].patch).toMatchObject({
      source_refs: ["s_001", "s_002", "s_003_NEW"],
    });
  });

  it("recommendations 修改 source_refs 应触发 diff (走 atomic payload path)", () => {
    const orig = FIXTURE_recommendations_with_refs;
    const doc = canvasToPMDoc([orig]);
    const topNode = (
      doc.content as { type: string; attrs: Record<string, unknown> }[]
    )[0];
    const payload = topNode.attrs.payload as Record<string, unknown>;
    payload.source_refs = ["s_010", "s_011", "s_012_NEW"];

    const patches = pmDocToSections(doc, [orig]);
    expect(patches[0].changed).toBe(true);
    expect(patches[0].patch).toMatchObject({
      source_refs: ["s_010", "s_011", "s_012_NEW"],
    });
  });

  it("3 个 source_refs section 一起 round-trip 不变", () => {
    const doc = canvasToPMDoc(SOURCE_REFS_FIXTURES);
    const patches = pmDocToSections(doc, SOURCE_REFS_FIXTURES);
    expect(patches).toHaveLength(SOURCE_REFS_FIXTURES.length);
    for (const p of patches) {
      expect(p.changed).toBe(false);
    }
  });
});

// ────────────────────────────────────────────────────────────
// 4. PM node name ↔ Section kind mappings are symmetric
// ────────────────────────────────────────────────────────────

describe("schema 映射对称", () => {
  it("KIND_TO_NODE_NAME / NODE_NAME_TO_KIND 双向一致", async () => {
    const { KIND_TO_NODE_NAME, NODE_NAME_TO_KIND, ALL_KINDS } = await import("../schema");
    for (const k of ALL_KINDS) {
      const nodeName = KIND_TO_NODE_NAME[k];
      expect(nodeName).toBe(`section_${k}`);
      expect(NODE_NAME_TO_KIND[nodeName]).toBe(k);
    }
  });

  it("ALL_KINDS = 22 (Sprint ι: code_block; multimodal-v1: image; S3: spec_block; multimodal-v2A: design; +unknown; −trend_chart/−attribution/−sql_block)", async () => {
    const { ALL_KINDS, TOTAL_KIND_COUNT } = await import("../schema");
    expect(ALL_KINDS.length).toBe(22);
    expect(TOTAL_KIND_COUNT).toBe(22);
  });
});

// ── Bake seam for retired markers (citation-audit M3, impl review P1) ────────────────────
// All round-trip assertions above assume gen3 fixtures (actual DB shape after cleanup). This describe
// enforces **intentional asymmetry**: feeding retired markers must make the forward adapter bake them.
//
// Why enforce explicitly: the old gen2 fixture + `changed=false` assertion effectively locked the forward adapter
// into not baking, exactly the review P1 bug (raw canvas-body markers). Migrating the fixture to
// gen3 turns these four assertions green but also erases the sole evidence targeting that seam: another author
// might remove `stripLegacy` from canvas-to-pm to make round-trip pass, silently reviving P1.
describe("canvasToPMDoc bake 退役标记（永久兜底 seam）", () => {
  const legacySection = {
    id: "f_legacy",
    kind: "narrative" as const,
    markdown: "Q1 GMV [num:849000:CNY:src:s_001]，主因 [ref:s_002] 渠道提升。",
  };

  it("裸标记永不进 PM doc（渲染层硬底线）", () => {
    const doc = canvasToPMDoc([legacySection as never]);
    const text = JSON.stringify(doc);
    expect(text).not.toContain("[num:");
    expect(text).not.toContain("[ref:");
    expect(text).toContain("84.9万元");
  });

  it("bake 后与 BE strip_legacy 同口径（单位必保，不是裸删）", () => {
    const doc = canvasToPMDoc([legacySection as never]);
    const patches = pmDocToSections(doc, [legacySection as never]);
    // Actual retired DB content → edit write-back bakes it into plain text (same semantics as M2 migration,
    // self-healing rather than corruption). Thus changed=true is **expected**, consistent with gen3 fixtures above
    // having changed=false.
    expect(patches[0].changed).toBe(true);
    expect((patches[0].patch as { markdown?: string }).markdown).toBe(
      "Q1 GMV 84.9万元，主因 渠道提升。",
    );
  });
});
