import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { ALL_KINDS } from "./canvas-pm-adapter/schema";
import { computeCanvasVisualInputSha256 } from "./canvas-visual-identity";
import type { ReportSection } from "./types";
import { preprocessSections } from "@/components/canvas/report-view";
import { chartStyleExportThemeFixture } from "@/__tests__/chart-style-export-fixture";

type SurfaceFixture = {
  sections: Array<{
    id: string;
    kind: string;
    payload: Record<string, unknown>;
    hidden_for_chat_mode?: boolean;
    superseded?: boolean;
    quarantined?: boolean;
  }>;
  expected_entries: Array<{
    section_id: string;
    effective_kind: string;
    placement: string;
    parent_section_id: string | null;
    parity_requirement: string;
  }>;
  expected_exclusions: Array<{ section_id: string; reason: string }>;
  expected_visual_input_sha256: Record<string, string>;
};

const fixturePath = resolve(
  process.cwd(),
  "../../eval/fixtures/canvas_export/surface_projection_v1.json",
);
const fixture = JSON.parse(
  readFileSync(fixturePath, "utf-8"),
) as SurfaceFixture;

describe("CanvasSurfaceProjection shared contract", () => {
  it("keeps the frontend section registry and server fixture in lockstep", () => {
    // unknown is a **frontend-only** fallback render target (canvas-to-pm.ts writes it for unknown kinds);
    // backend neither produces nor projects it, instead taking
    // the default SURFACE_KIND_POLICIES.get(kind, _UNKNOWN_KIND_POLICY) branch.
    // Exclude it from this fixture, shared by seven backend export tests
    // （xlsx / pptx / pdf / ir / validation / materializer / projection），
    // and carrying expected_visual_input_sha256; changing it for a kind absent from backend
    // creates only collateral damage. Lockstep covers only kinds **actually persisted**.
    const frontendKinds = [...ALL_KINDS].filter((k) => k !== "unknown").sort();
    const fixtureKinds = [...new Set(fixture.sections.map((section) => section.kind))].sort();
    const plannedKinds = [...new Set(fixture.expected_entries.map((entry) => entry.effective_kind))].sort();

    expect(fixtureKinds).toEqual(frontendKinds);
    expect(plannedKinds).toEqual(frontendKinds);
    expect(fixture.expected_entries).toHaveLength(22);
    expect(new Set(fixture.expected_entries.map((entry) => entry.section_id)).size).toBe(22);
    expect(fixture.expected_exclusions.map((entry) => entry.reason)).toEqual([
      "hidden_for_chat_mode",
      "superseded_history",
      "quarantined_rejected",
    ]);
  });

  it("mirrors the actual ReportView top-level preprocessing order", () => {
    const reportKinds = new Set([
      "eyebrow",
      "h1",
      "meta",
      "kpi_row",
      "deck",
      "section",
      "narrative",
      "callout",
      "next_list",
      "datarc_iframe",
      "html_artifact",
      "external_iframe",
      "image",
      "design",
      "chart",
      "spec_block",
      "sources",
      "insight_card",
      "recommendations",
    ]);
    const pageInput = fixture.sections
      .filter(
        (section) =>
          reportKinds.has(section.kind)
          && !section.hidden_for_chat_mode
          && !section.superseded
          && !section.quarantined,
      )
      .map(
        (section) =>
          ({
            id: section.id,
            kind: section.kind,
            ...section.payload,
          }) as ReportSection,
      );
    const actualTopLevelIds = preprocessSections(pageInput).map(
      (section) => section.id,
    );
    const expectedTopLevelIds = fixture.expected_entries
      .filter(
        (entry) =>
          entry.parent_section_id === null
          && ["report_body", "sources"].includes(entry.placement),
      )
      .map((entry) => entry.section_id);

    expect(actualTopLevelIds).toEqual(expectedTopLevelIds);
  });

  it("computes the same six visual identities as the export normalizer", async () => {
    const actual = Object.fromEntries(
      await Promise.all(
        Object.keys(fixture.expected_visual_input_sha256).map(async (sectionId) => {
          const frozen = fixture.sections.find((section) => section.id === sectionId);
          expect(frozen).toBeDefined();
          const section = {
            id: frozen!.id,
            kind: frozen!.kind,
            ...frozen!.payload,
          } as ReportSection;
          return [
            sectionId,
            await computeCanvasVisualInputSha256({
              section,
              locale: "zh-CN",
              theme: chartStyleExportThemeFixture(),
            }),
          ];
        }),
      ),
    );
    expect(actual).toEqual(fixture.expected_visual_input_sha256);
  });
});
