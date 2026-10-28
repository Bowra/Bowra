import { describe, expect, it, vi } from "vitest";
import { applyChartEdit } from "./composition";
import { chartDatasetAvailable, evidencePlanForViewer, type EvidencePlanV1 } from "./presentation-plan";
import { redactViewerSourceIdentitiesInValue } from "../canvas-pm-adapter/citation-types";
import type { ChartSpec } from "./types";
import { ensureFullRows } from "./view-transforms";
import { needsRowCompletion } from "./complete-chart-rows";
import { fetchFilePreview } from "../api";

vi.mock("../api", () => ({ fetchFilePreview: vi.fn() }));
const alias = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const datasetAlias = "source-ref-v1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";
const plan: EvidencePlanV1 = {
  version: 1,
  dataset: { artifact_id: datasetAlias, availability: "available", content_digest: "a".repeat(64), schema_digest: "b".repeat(64) },
  evidence_notes: [{ id: "definition", measure_keys: ["value"], excerpt: "Revenue includes returns.", locator: "Definition", verified_content_digest: "c".repeat(64), availability: "available",
    source_ref: { id: alias, source_alias: alias, access_state: "linked_accessible", safe_label: "Definition" } }],
  text_dependencies: { "/title": { note_ids: ["definition"], dataset: true }, "/views/0/spec/title": { note_ids: ["definition"], dataset: true }, "/views/1/spec/title": { note_ids: ["definition"], dataset: true } },
};
const child: ChartSpec = { kind: "table", title: "Values", data_artifact_id: "art_data", row_count: 3, rows: [{ region: "A", value: 12 }], columns: [{ key: "region", header: "Region", type: "categorical" }, { key: "value", header: "Value", type: "numeric", unit: "USD" }] };
const root: ChartSpec = { ...child, title: "Regional results", presentation_plan: plan, views: [{ id: "primary", kind: "chart", spec: child }, { id: "detail", kind: "chart", spec: child }], layout: { type: "split", children: [{ view: "primary" }, { view: "detail" }] } };

describe("native evidence plans", () => {
  it("uses Unicode code points and rejects server-owned/unknown shapes", () => {
    const accepted = structuredClone(plan);
    accepted.evidence_notes[0]!.excerpt = "😀".repeat(300);
    expect(evidencePlanForViewer(accepted)).toEqual(accepted);
    accepted.evidence_notes[0]!.excerpt += "😀";
    expect(evidencePlanForViewer(accepted)).toBeNull();
    expect(evidencePlanForViewer({ ...plan, interactions: [] })).toBeNull();
    expect(evidencePlanForViewer({ ...plan, dataset: { ...plan.dataset, injected: true } })).toBeNull();
    expect(evidencePlanForViewer({ ...plan, version: 2 })).not.toBeNull();
    expect(evidencePlanForViewer({ ...plan, version: 4 })).toBeNull();
  });
  it("edits the existing child while retaining the complete sealed root", () => {
    const edited = applyChartEdit(root, { scope: "view", viewId: "detail" }, { kind: "kpi" });
    expect(edited.presentation_plan).toBe(plan);
    expect(edited.views![0]).toBe(root.views![0]);
    expect(edited.views![1]!.spec.kind).toBe("kpi");
    expect(root.views![1]!.spec.kind).toBe("table");
    expect(() => applyChartEdit({ ...root, presentation_plan: { version: 2, availability: "unavailable" } }, { scope: "root" }, { title: "New" })).toThrow();
  });
  it("revokes literal text and captions without modifying data, layout or stable child IDs", () => {
    const redacted = redactViewerSourceIdentitiesInValue(root, new Set([alias]));
    expect(redacted.title).toBe("");
    expect(redacted.views!.map((view) => [view.id, view.spec.title])).toEqual([["primary", ""], ["detail", ""]]);
    expect(JSON.stringify(redacted)).not.toContain("Revenue includes returns.");
    expect(JSON.stringify(redacted)).not.toContain('"locator"');
    expect(redacted.rows).toBe(root.rows);
    expect(redacted.layout).toBe(root.layout);
    expect(evidencePlanForViewer(redacted.presentation_plan)).not.toBeNull();
    expect(redactViewerSourceIdentitiesInValue(redacted, new Set([alias]))).toBe(redacted);
    expect(root.title).toBe("Regional results");
  });
  it("keeps saved rows and performs no completion request after dataset revocation", async () => {
    const redacted = redactViewerSourceIdentitiesInValue(root, new Set([datasetAlias]));
    expect(chartDatasetAvailable(redacted)).toBe(false);
    expect(needsRowCompletion(redacted)).toBe(false);
    expect(await ensureFullRows(redacted)).toEqual({ spec: redacted, truncated: true, ok: false });
    expect(fetchFilePreview).not.toHaveBeenCalled();
    expect(evidencePlanForViewer(redacted.presentation_plan)?.dataset.content_digest).toBeUndefined();
  });
});
