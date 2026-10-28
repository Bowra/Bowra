import { describe, expect, it } from "vitest";
import { timeWindowFixture } from "../../e2e/_chart-time-window-fixture";
import { preparedFramesFixture } from "../../e2e/_chart-prepared-frames-fixture";
import { prepareFrameViews, type FrameBinding } from "./prepared-frames";
import { evidencePlanForViewer } from "./presentation-plan";
import type { FocusBinding } from "./linked-focus";
import { canonicalizeChartSelectedContext, selectedContextForView, type ChartFollowupScope } from "./selected-context";
import { canonicalizeComposerAttachmentRef } from "../composer-attachment-refs";

const scope: ChartFollowupScope = { version: 1, sectionId: "sec", sectionRev: 3, viewId: "detail", bindingId: "time.fixture", datasetDigest: "a".repeat(64) };
const selector = { ...scope, tuples: [["2026-02"]], measureKeys: ["revenue", "cost"] };

describe("existing chart reference selection", () => {
  it("retains the closed selector through ordinary reference normalization without accepting browser values", () => {
    const ref = { fileId: "art", filename: "months.csv", size: 12, mime: "text/csv", referenceKind: "result", selectedContext: selector };
    expect(canonicalizeComposerAttachmentRef(ref)?.selectedContext).toEqual(selector);
    expect(canonicalizeComposerAttachmentRef({ ...ref, referenceKind: "upload" })).toBeNull();
    expect(canonicalizeComposerAttachmentRef({ ...ref, selectedContext: { ...selector, observations: [{ revenue: 100 }] } })).toBeNull();
    const copy = canonicalizeComposerAttachmentRef(ref)!;
    copy.selectedContext!.tuples![0][0] = "changed";
    expect(ref.selectedContext.tuples[0][0]).toBe("2026-02");
  });
  it.each([{ version: true }, { sectionRev: 0 }, { tuples: [[true]] }, { tuples: [[NaN]] }, { tuples: [[2 ** 53]] }, { tuples: [["x".repeat(2000)]] }, { tuples: [["a"]].flatMap(() => Array.from({ length: 21 }, () => ["a"])) }, { window: { lower: null, upper: "x" } }])("rejects unbounded or lossy keys: %j", (patch) => {
    expect(canonicalizeChartSelectedContext({ ...selector, ...patch })).toBeNull();
  });
  it("uses only the inspected view's visible measures and exact observation grain", () => {
    const root = timeWindowFixture().spec;
    const binding = evidencePlanForViewer(root.presentation_plan)!.bindings![scope.bindingId] as FocusBinding;
    const child = root.views!.find((view) => view.id === "detail")!.spec;
    expect(selectedContextForView(scope, binding, { bindingId: scope.bindingId, tuple: ["2026-02"] }, { ...child, columns: child.columns.filter((column) => column.key !== "cost") })).toEqual({ ...selector, measureKeys: ["revenue"] });
    expect(selectedContextForView(scope, binding, { bindingId: scope.bindingId, tuple: ["outside"] }, child)).toBeNull();
    expect(selectedContextForView(scope, binding, null, child)).toBeNull();
    expect(selectedContextForView({ ...scope, window: { lower: "2026-02", upper: "2026-04" } }, binding, null, child)?.window).toEqual({ lower: "2026-02", upper: "2026-04" });
    expect(selectedContextForView(scope, binding, { bindingId: scope.bindingId, tuple: ["2026-02"] }, { ...child, _view_folded: true })).toBeNull();
  });
  it("retains only a supplied frame and the inspected measures without echoing values", () => {
    const root = preparedFramesFixture().spec;
    const binding = evidencePlanForViewer(root.presentation_plan)!.bindings!["frame.fixture"] as FrameBinding;
    const child = prepareFrameViews(new Map(root.views!.map((view) => [view.id, view.spec])), binding, binding.eligible_views)!.get("relationship")!.get(JSON.stringify(["2026-02"]))!;
    const frameScope = { ...scope, viewId: "relationship", bindingId: "frame.fixture", frameKey: "2026-02" };
    const selected = selectedContextForView(frameScope, binding, null, child);
    expect(selected).toEqual({ ...frameScope, measureKeys: ["revenue", "people"] });
    expect(canonicalizeComposerAttachmentRef({ fileId: "art", filename: "frames.csv", size: 12, mime: "text/csv", referenceKind: "result", selectedContext: selected })?.selectedContext).toEqual(selected);
    expect(selectedContextForView({ ...frameScope, frameKey: "2026-04" }, binding, null, child)).toBeNull();
    expect(selectedContextForView(frameScope, binding, null, { ...child, rows: root.views![0].spec.rows })).toBeNull();
    expect(canonicalizeChartSelectedContext({ ...selected, tuples: [["A"]] })).toBeNull();
    expect(canonicalizeChartSelectedContext({ ...selected, window: { lower: "2026-01", upper: "2026-02" } })).toBeNull();
    expect(canonicalizeChartSelectedContext({ ...selected, frameKey: true })).toBeNull();
    expect(canonicalizeChartSelectedContext({ ...selected, frameKey: null })).toBeNull();
  });
});
