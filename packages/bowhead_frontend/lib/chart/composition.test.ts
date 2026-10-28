import { describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import { CanvasDocument } from "../canvas-pm-adapter/schema";
import { getAllCanvasExtensions } from "../canvas-pm-adapter/extensions";
import { canvasToPMDoc } from "../canvas-pm-adapter/canvas-to-pm";
import { pmDocToSections } from "../canvas-pm-adapter/pm-to-canvas";
import { applyChartEdit, chartLayoutPreset } from "./composition";
import type { ChartSpec } from "./types";
import type { ReportSection } from "../types";
import { annotationsFixture } from "../../e2e/_chart-annotations-fixture";

const child: ChartSpec = {
  kind: "table", title: "Values", data_artifact_id: "art_example", row_count: 2,
  columns: [{ key: "region", header: "Region", type: "categorical" }, { key: "value", header: "Value", type: "numeric", unit: "USD" }],
  rows: [{ region: "A", value: 12 }, { region: "B", value: 21 }],
};
const root: ChartSpec = {
  ...child, title: "Regional results",
  views: [{ id: "comparison", kind: "chart", spec: { ...child, kind: "bar" } }, { id: "details", kind: "chart", spec: child }],
  layout: { type: "split", children: [{ view: "comparison" }, { view: "details" }] },
};

describe("native composition editing", () => {
  it("changes only arrangement in authored order and retains protected plans through undo and reload", () => {
    const section = annotationsFixture(true);
    const saved = section.spec;
    saved.layout = { type: "stack", children: [{ view: "detail" }, { type: "split", children: [{ view: "comparison" }, { view: "relationship" }] }] };
    const layout = chartLayoutPreset(saved, "grid2");
    expect(layout).toEqual({ type: "grid2", children: [{ view: "detail" }, { view: "comparison" }, { view: "relationship" }] });
    const changed = applyChartEdit(saved, { scope: "root" }, { layout });
    expect(changed.layout).toEqual(layout);
    expect(changed.views).toBe(saved.views);
    expect(changed.presentation_plan).toBe(saved.presentation_plan);
    const editor = new Editor({ extensions: [CanvasDocument, StarterKit.configure({ document: false }), ...getAllCanvasExtensions()], content: canvasToPMDoc([section]) });
    try {
      const node = editor.state.doc.firstChild!;
      editor.view.dispatch(editor.state.tr.setNodeMarkup(0, undefined, { ...node.attrs, payload: { ...node.attrs.payload, spec: changed } }));
      expect(pmDocToSections(editor.getJSON(), [section])[0]!.patch).toEqual({ spec: changed });
      expect(editor.commands.undo()).toBe(true);
      expect(pmDocToSections(editor.getJSON(), [section])[0]!.changed).toBe(false);
      expect(editor.commands.redo()).toBe(true);
      editor.commands.setContent(canvasToPMDoc([{ ...section, spec: JSON.parse(JSON.stringify(changed)), rev: 2 }]));
      expect(editor.state.doc.firstChild!.attrs.payload.spec).toEqual(changed);
    } finally { editor.destroy(); }
    expect(() => chartLayoutPreset(saved, "split")).toThrow();
    expect(() => applyChartEdit(saved, { scope: "view", viewId: "detail" }, { layout })).toThrow();
    expect(() => applyChartEdit(saved, { scope: "root" }, { layout: { type: "stack", children: [{ view: "detail" }, { view: "detail" }] } })).toThrow();
  });
  it("saves one complete PM root, preserves its sibling and supports native undo", () => {
    const section = { id: "sec_root", kind: "chart", rev: 3, spec: root } as ReportSection;
    const editor = new Editor({ extensions: [CanvasDocument, StarterKit.configure({ document: false }), ...getAllCanvasExtensions()],
      content: canvasToPMDoc([section]) });
    try {
      const changed = applyChartEdit(root, { scope: "view", viewId: "details" }, { kind: "kpi", show_data_labels: true });
      const node = editor.state.doc.firstChild!;
      editor.view.dispatch(editor.state.tr.setNodeMarkup(0, undefined, { ...node.attrs, payload: { ...node.attrs.payload, spec: changed } }));
      const result = pmDocToSections(editor.getJSON(), [section]);
      expect(result).toHaveLength(1);
      expect(result[0].sectionId).toBe("sec_root");
      expect(result[0].patch).toEqual({ spec: changed });
      expect(changed.views![0]).toBe(root.views![0]);
      expect(changed.layout).toBe(root.layout);
      expect(root.views![1]!.spec.kind).toBe("table");
      expect(editor.commands.undo()).toBe(true);
      expect(pmDocToSections(editor.getJSON(), [section])[0]!.changed).toBe(false);
      expect(editor.commands.redo()).toBe(true);
      expect(pmDocToSections(editor.getJSON(), [section])[0]!.patch).toEqual({ spec: changed });
    } finally { editor.destroy(); }
  });

  it("rejects stale targets, unsupported fields and incompatible units before changing the root", () => {
    expect(() => applyChartEdit(root, { scope: "view", viewId: "deleted" }, { kind: "kpi" })).toThrow();
    expect(() => applyChartEdit(root, { scope: "view", viewId: "details" }, { rows: [] } as never)).toThrow();
    const mixed: ChartSpec = { ...child, columns: [...child.columns, { key: "count", header: "Count", type: "numeric", unit: "people" }],
      rows: [{ region: "A", value: 12, count: 1 }], row_count: 1 };
    expect(() => applyChartEdit(mixed, { scope: "root" }, { kind: "line" })).toThrow();
    expect(root.views![1]!.spec.kind).toBe("table");
  });
});
