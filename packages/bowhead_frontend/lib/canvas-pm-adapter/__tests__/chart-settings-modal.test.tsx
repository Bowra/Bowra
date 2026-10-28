/**
 * Report-edit Chart Settings dialog layers (design document, Layers).
 *
 * Previously rendered in place inside the chart NodeView: report wrappers `.canvas-report-shell` and `.v2-ws-right-body` have
 * isolation:isolate, and desktop `.v2-app-root` is fixed, putting z-index 9999 effectively in layer 0. Real-browser tests
 * (chromium, webkit) showed the sidebar and divider painted above the overlay and panel. Use body-mounted DialogShell instead.
 * jsdom has no stacking, so lock placement outside the report subtree and dialog behavior here; see the commit notes for observed stacking.
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

import { ChartSettingsModal } from "../extensions/atomic";
import type { ChartSpec } from "@/lib/chart/types";

afterEach(() => {
  cleanup();
});

describe("ChartSettingsModal", () => {
  it("renders outside the report subtree it was opened from", async () => {
    const { container } = renderIntl(
      <div className="canvas-report-shell" style={{ isolation: "isolate" }}>
        <ChartSettingsModal spec={{ kind: "bar" }} onSave={vi.fn()} onCancel={vi.fn()} />
      </div>,
    );
    const dialog = await screen.findByRole("dialog", { name: "图表设置" });
    expect(container.contains(dialog)).toBe(false);
  });

  it("closes on Escape and saves the chosen options", async () => {
    const onCancel = vi.fn();
    const onSave = vi.fn();
    renderIntl(<ChartSettingsModal spec={{ kind: "bar" }} onSave={onSave} onCancel={onCancel} />);
    const dialog = await screen.findByRole("dialog", { name: "图表设置" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("combobox")));
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("checkbox", { name: "显示数据标签" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ kind: "bar", show_data_labels: true }));
  });

  it("edits a selected child through the existing dialog and reloads its options", async () => {
    const first: ChartSpec = {
      kind: "table", title: "Revenue", row_count: 1,
      columns: [{ key: "revenue", header: "Revenue", type: "numeric" }], rows: [{ revenue: 12 }],
    };
    const second: ChartSpec = { ...first, title: "Cost", show_legend: false, show_data_labels: true };
    const onSave = vi.fn();
    renderIntl(<ChartSettingsModal spec={{ ...first, views: [
      { id: "revenue", kind: "chart", spec: first }, { id: "cost", kind: "chart", spec: second },
    ], layout: { type: "stack", children: [{ view: "revenue" }, { view: "cost" }] } }} onSave={onSave} onCancel={vi.fn()} />);
    fireEvent.change(await screen.findByLabelText("子图"), { target: { value: "cost" } });
    expect(screen.getByRole("checkbox", { name: "显示图例" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "显示数据标签" })).toBeChecked();
    fireEvent.change(screen.getByLabelText("类型"), { target: { value: "kpi" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ kind: "kpi", show_legend: false }), { scope: "view", viewId: "cost" });
  });

  it("keeps an incompatible kind draft in the dialog without saving", async () => {
    const onSave = vi.fn();
    renderIntl(<ChartSettingsModal spec={{ kind: "table", columns: [
      { key: "value", header: "Value", type: "numeric" },
    ], rows: [{ value: 12 }], row_count: 1, title: "Value" }} onSave={onSave} onCancel={vi.fn()} />);
    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "line" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("saves child copy or the complete figure arrangement through the existing target owner", async () => {
    const child: ChartSpec = { kind: "table", title: "Values", row_count: 1, columns: [{ key: "value", header: "Value", type: "numeric" }], rows: [{ value: 12 }] };
    const root: ChartSpec = { ...child, views: [
      { id: "first", kind: "chart", spec: child }, { id: "second", kind: "chart", spec: child },
    ], layout: { type: "split", children: [{ view: "second" }, { view: "first" }] } };
    const onSave = vi.fn();
    renderIntl(<ChartSettingsModal spec={root} onSave={onSave} onCancel={vi.fn()} />);
    fireEvent.change(await screen.findByLabelText("子图标题"), { target: { value: "Original values" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onSave).toHaveBeenLastCalledWith(expect.objectContaining({ title: "Original values" }), { scope: "view", viewId: "first" });
    fireEvent.change(screen.getByLabelText("子图"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("布局"), { target: { value: "stack" } });
    expect(screen.queryByLabelText("类型")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onSave).toHaveBeenLastCalledWith({ layout: { type: "stack", children: [{ view: "second" }, { view: "first" }] } }, { scope: "root" });
  });
});
