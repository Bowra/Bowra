/**
 * Grid parts of document matrix step 1, S2: the column question on hover and focus, and row selection.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MatrixSnapshot } from "@/lib/kb-matrix/types";
import messagesEn from "@/messages/en-US.json";

import { MatrixGrid, type MatrixRowControls } from "./matrix-grid";

const m = messagesEn.kbMatrix;

const snapshot = {
  matrixId: "mx_test", title: "Contracts", readOnly: false, seq: 1, canEdit: true, runs: [],
  columns: [{ columnId: "c1", name: "Is there a non-compete clause", type: "boolean", question: "Does the contract restrict the supplier from competing?" }],
  rows: [
    { fileId: "kbf_1", fileName: "a.pdf", missing: false, extractFailed: false, recordId: "", approved: false, cells: {} },
    { fileId: "kbf_2", fileName: "b.pdf", missing: false, extractFailed: false, recordId: "", approved: false, cells: {} },
  ],
} as unknown as MatrixSnapshot;

function renderGrid(controls?: MatrixRowControls) {
  render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <MatrixGrid snapshot={snapshot} focus={null} onCell={vi.fn()} onCiteHover={vi.fn()} rowControls={controls} />
    </NextIntlClientProvider>,
  );
}

afterEach(cleanup);

describe("matrix grid header and rows", () => {
  it("shows the full column name and its question on hover and on focus", () => {
    renderGrid();
    const name = screen.getByRole("button", { name: "Is there a non-compete clause" });
    expect(name.closest(".mx-colh")).not.toHaveAttribute("title");
    fireEvent.mouseEnter(name.closest(".mx-colh") as Element);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Does the contract restrict the supplier from competing?");
    fireEvent.mouseLeave(name.closest(".mx-colh") as Element);
    expect(screen.queryByRole("tooltip")).toBeNull();
    fireEvent.focus(name);
    expect(name).toHaveAttribute("aria-describedby", screen.getByRole("tooltip").id);
  });

  it("selects every shown row from the header checkbox", () => {
    const controls: MatrixRowControls = { selected: new Set(["kbf_1"]), onToggle: vi.fn(), onToggleAll: vi.fn(), onRemove: vi.fn() };
    renderGrid(controls);
    const all = screen.getByRole("checkbox", { name: m.rows_select_all });
    expect(all).not.toBeChecked();
    expect((all as HTMLInputElement).indeterminate).toBe(true);
    fireEvent.click(all);
    expect(controls.onToggleAll).toHaveBeenCalledWith(["kbf_1", "kbf_2"], true);
  });
});
