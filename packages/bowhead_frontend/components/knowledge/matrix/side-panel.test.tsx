import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MatrixCellVM, MatrixColumnVM, MatrixRowVM } from "@/lib/kb-matrix/types";
import messagesZh from "@/messages/zh-CN.json";

vi.mock("@/lib/canvas-pm-adapter/kb-chunk-cache", () => ({
  useKbChunkContent: () => ({ status: "idle" }),
}));

import { SidePanel } from "./side-panel";

const ROW: MatrixRowVM = {
  fileId: "kbf_1",
  fileName: "a.pdf",
  missing: false,
  extractFailed: false,
  recordId: "rec_1",
  approved: false,
  cells: {},
};

const CELL: MatrixCellVM = { status: "done", value: "乙", variants: [], edited: false, verified: false };

function renderPanel(type: string, value = "乙") {
  const col: MatrixColumnVM = { columnId: "c1", name: "等级", type, options: ["甲", "乙", "丙"] };
  const onSaveValue = vi.fn();
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <SidePanel
        row={ROW}
        col={col}
        cell={{ ...CELL, value }}
        canEdit
        saving={false}
        onClose={vi.fn()}
        onSaveValue={onSaveValue}
        onMarkVerified={vi.fn()}
      />
    </NextIntlClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: messagesZh.kbMatrix.edit_value }));
  return onSaveValue;
}

afterEach(cleanup);

describe("SidePanel · 选项编辑", () => {
  it("单选：方向键与 Home/End 移动并选中，Tab 只停选中项", () => {
    const onSaveValue = renderPanel("select");
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual(["false", "true", "false"]);
    expect(radios.map((r) => r.tabIndex)).toEqual([-1, 0, -1]);

    radios[1].focus();
    fireEvent.keyDown(radios[1], { key: "ArrowDown" });
    expect(document.activeElement).toBe(radios[2]);
    expect(radios[2].getAttribute("aria-checked")).toBe("true");
    expect(radios[2].className).toContain("on");
    fireEvent.keyDown(radios[2], { key: "Home" });
    expect(document.activeElement).toBe(radios[0]);
    expect(radios[0].getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(radios[0], { key: "End" });
    expect(radios[2].getAttribute("aria-checked")).toBe("true");
    expect(radios.map((r) => r.tabIndex)).toEqual([-1, -1, 0]);

    fireEvent.click(screen.getByRole("button", { name: messagesZh.kbMatrix.save }));
    expect(onSaveValue).toHaveBeenCalledWith("丙");
  });

  it("多选：复选框各自可 Tab，方向键不改选", () => {
    renderPanel("multi_select", "甲");
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes.map((b) => b.tabIndex)).toEqual([0, 0, 0]);
    boxes[0].focus();
    fireEvent.keyDown(boxes[0], { key: "ArrowDown" });
    expect(boxes.map((b) => b.getAttribute("aria-checked"))).toEqual(["true", "false", "false"]);
  });
});
