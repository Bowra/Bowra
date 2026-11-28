/**
 * Review on the matrix page (document matrix step 1, S4): the toolbar filters by status and value, sorts, and keeps
 * the view in the URL (reload restores it); the verified progress; "整列标记已核" with its confirmation and result;
 * "View failed" as the failed status filter; arrow keys follow the rows shown.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastContainer } from "@/components/chat/toast-container";
import { setMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixCellVM, MatrixColumnVM, MatrixRowVM, MatrixSnapshot } from "@/lib/kb-matrix/types";
import { _clearAllToasts } from "@/lib/use-toast";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({ reload: vi.fn(), verifyCells: vi.fn() }));

vi.mock("@/lib/kb-matrix/run-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/run-api")>();
  return { ...actual, verifyCells: (...args: unknown[]) => mocks.verifyCells(...args) };
});
vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/kb-matrix/use-matrix-poll", () => ({ useMatrixPolling: () => ({ reload: mocks.reload }) }));
vi.mock("./run-banner", () => ({
  RunBanners: ({ onViewFailed }: { onViewFailed: (runId?: string) => void }) => (
    <button type="button" onClick={() => onViewFailed("run_1")}>
      view failed
    </button>
  ),
}));
// The grid's rendering is covered elsewhere; this stub lists the rows it is given, in order, and the cell focus.
vi.mock("./matrix-grid", () => ({
  MatrixGrid: ({
    snapshot,
    focus,
    onCell,
    onColMenu,
  }: {
    snapshot: MatrixSnapshot;
    focus: { fileId: string; columnId: string } | null;
    onCell: (fileId: string, columnId: string) => void;
    onColMenu?: (col: MatrixColumnVM, e: React.MouseEvent) => void;
  }) => (
    <div data-testid="matrix-grid" data-focus={focus ? `${focus.fileId}:${focus.columnId}` : ""}>
      <ol>
        {snapshot.rows.map((r) => (
          <li key={r.fileId}>{r.fileName || r.fileId}</li>
        ))}
      </ol>
      {snapshot.columns.map((c) => (
        // Like the real caret, the click stays with the header (the matrix closes menus on clicks that reach it).
        <button
          key={c.columnId}
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onColMenu?.(c, e);
          }}
        >
          {`menu ${c.name}`}
        </button>
      ))}
      {snapshot.rows[0] && (
        <button type="button" onClick={() => onCell(snapshot.rows[0].fileId, snapshot.columns[0].columnId)}>
          open first
        </button>
      )}
    </div>
  ),
}));

import { MatrixViewM2 } from "./matrix-view-m2";

const m = messagesEn.kbMatrix;

const cell = (over: Partial<MatrixCellVM> = {}): MatrixCellVM => ({
  status: "done",
  value: "v",
  variants: [],
  edited: false,
  verified: false,
  confidence: 0.9,
  sourceExcerpt: "quote",
  chunkId: "chk_1",
  ...over,
});
const row = (fileId: string, cells: MatrixRowVM["cells"], over: Partial<MatrixRowVM> = {}): MatrixRowVM => ({
  fileId,
  fileName: `${fileId}.pdf`,
  missing: false,
  extractFailed: false,
  recordId: "",
  approved: false,
  cells,
  ...over,
});

const SNAPSHOT: MatrixSnapshot = {
  matrixId: "mx_test",
  title: "Contracts",
  readOnly: false,
  seq: 1,
  canEdit: true,
  runs: [],
  columns: [
    { columnId: "amount", name: "Amount", type: "currency" },
    { columnId: "due", name: "Due", type: "date" },
    { columnId: "pay", name: "Pay", type: "select", options: ["prepaid", "monthly"] },
    { columnId: "note", name: "Note", type: "question" },
  ],
  rows: [
    row("a", { amount: cell({ value: "1280000", verified: true }), due: cell({ value: "2026-02-28" }), pay: cell({ value: "prepaid" }), note: cell({ edited: true }) }),
    row("b", { amount: cell({ value: "860000", confidence: 0.5 }), due: cell({ value: "2026-06-14", stale: true }), pay: cell({ value: "monthly" }), note: cell({ status: "failed", value: "" }) }),
    row("c", { amount: cell({ value: "245000", selfConsistencyAgree: 0.5 }), pay: cell({ value: "prepaid", verified: true }) }),
    row("gone", { amount: cell({ status: "failed", value: "", failReason: "source_unavailable" }) }, { missing: true, fileName: "" }),
  ],
};

function shownRows(): string[] {
  return within(screen.getByTestId("matrix-grid"))
    .getAllByRole("listitem")
    .map((li) => li.textContent ?? "");
}

function query(): URLSearchParams {
  return new URLSearchParams(window.location.search);
}

function renderView(snapshot: MatrixSnapshot = SNAPSHOT) {
  mocks.reload.mockImplementation(async () => setMatrixSnapshot(snapshot));
  return render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <MatrixViewM2 matrixId="mx_test" onBack={vi.fn()} />
      <ToastContainer />
    </NextIntlClientProvider>,
  );
}

async function pickStatus(label: string) {
  fireEvent.click(screen.getByTestId("kb-matrix-status-filter"));
  fireEvent.click(within(await screen.findByTestId("kb-matrix-status-filter-menu")).getByRole("menuitemradio", { name: label }));
}

async function pickSort(label: string) {
  fireEvent.click(screen.getByTestId("kb-matrix-sort"));
  fireEvent.click(within(await screen.findByTestId("kb-matrix-sort-menu")).getByRole("menuitemradio", { name: label }));
}

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/knowledge?tab=matrix&view=mx_test");
});
afterEach(() => {
  cleanup();
  _clearAllToasts();
  setMatrixSnapshot(null);
});

describe("matrix review toolbar", () => {
  it("filters by status, then by value, sorts, and writes the view to the URL", async () => {
    renderView();
    await screen.findByTestId("kb-matrix-toolbar");
    expect(shownRows()).toEqual(["a.pdf", "b.pdf", "c.pdf", "gone"]);

    await pickStatus(m.status_low_confidence);
    await waitFor(() => expect(shownRows()).toEqual(["b.pdf"]));
    expect(query().getAll("f")).toEqual(["status:low_confidence"]);
    expect(screen.getByTestId("kb-matrix-filtercount")).toHaveTextContent("Showing 1 of 3 rows");
    await pickStatus(m.status_cross_check);
    await waitFor(() => expect(shownRows()).toEqual(["c.pdf"]));
    await pickStatus(m.status_all);
    await waitFor(() => expect(shownRows()).toEqual(["a.pdf", "b.pdf", "c.pdf", "gone"]));

    fireEvent.click(screen.getByTestId("kb-matrix-vfilter-add"));
    const form = await screen.findByTestId("kb-matrix-vfilter-form");
    fireEvent.change(within(form).getByLabelText(m.filter_column), { target: { value: "pay" } });
    expect(within(form).getByLabelText(m.filter_condition)).toHaveValue("eq");
    fireEvent.change(within(form).getByLabelText(m.filter_value), { target: { value: "prepaid" } });
    fireEvent.click(within(form).getByRole("button", { name: m.filter_apply }));
    await waitFor(() => expect(shownRows()).toEqual(["a.pdf", "c.pdf"]));
    expect(screen.getByTestId("kb-matrix-vfilter-pay")).toHaveTextContent("Pay: prepaid");
    expect(query().getAll("f")).toEqual(["eq:pay:prepaid"]);

    await pickSort("Amount");
    await waitFor(() => expect(shownRows()).toEqual(["c.pdf", "a.pdf"]));
    await pickSort(m.sort_desc_number);
    await waitFor(() => expect(shownRows()).toEqual(["a.pdf", "c.pdf"]));
    expect(screen.getByTestId("kb-matrix-sort")).toHaveTextContent("Amount · largest first");
    expect(query().get("sort")).toBe("desc:amount");
    expect(query().get("view")).toBe("mx_test");

    // Clearing keeps the sort; the unreadable row stays last.
    fireEvent.click(within(screen.getByTestId("kb-matrix-filtercount")).getByRole("button", { name: m.filter_clear }));
    await waitFor(() => expect(shownRows()).toEqual(["a.pdf", "b.pdf", "c.pdf", "gone"]));
    expect(query().getAll("f")).toEqual([]);
    expect(query().get("sort")).toBe("desc:amount");
  });

  it("closes its menus and the filter form on Esc without clearing the filters", async () => {
    window.history.replaceState(null, "", "/knowledge?tab=matrix&view=mx_test&f=status:edited");
    renderView();
    await waitFor(() => expect(shownRows()).toEqual(["a.pdf"]));
    fireEvent.click(screen.getByTestId("kb-matrix-status-filter"));
    const menu = await screen.findByTestId("kb-matrix-status-filter-menu");
    fireEvent.keyDown(within(menu).getAllByRole("menuitemradio")[0], { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("kb-matrix-status-filter-menu")).toBeNull());
    fireEvent.click(screen.getByTestId("kb-matrix-vfilter-add"));
    const form = await screen.findByTestId("kb-matrix-vfilter-form");
    fireEvent.keyDown(within(form).getByLabelText(m.filter_condition), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("kb-matrix-vfilter-form")).toBeNull());
    expect(shownRows()).toEqual(["a.pdf"]);
    expect(query().getAll("f")).toEqual(["status:edited"]);
  });

  it("offers each operator for the column type and filters numbers and dates by comparison", async () => {
    renderView();
    fireEvent.click(await screen.findByTestId("kb-matrix-vfilter-add"));
    const form = await screen.findByTestId("kb-matrix-vfilter-form");
    const condition = () => within(form).getByLabelText(m.filter_condition) as HTMLSelectElement;
    expect([...condition().options].map((o) => o.value)).toEqual(["eq", "gt", "lt", "empty"]);
    fireEvent.change(condition(), { target: { value: "gt" } });
    fireEvent.change(within(form).getByLabelText(m.filter_value), { target: { value: "500,000" } });
    fireEvent.click(within(form).getByRole("button", { name: m.filter_apply }));
    await waitFor(() => expect(shownRows()).toEqual(["a.pdf", "b.pdf"]));
    expect(screen.getByTestId("kb-matrix-vfilter-amount")).toHaveTextContent("Amount: greater than 500,000");

    // Reopen it from its button and turn it into a date filter.
    fireEvent.click(screen.getByTestId("kb-matrix-vfilter-amount"));
    const edit = await screen.findByTestId("kb-matrix-vfilter-form");
    fireEvent.click(within(edit).getByRole("button", { name: m.filter_remove }));
    await waitFor(() => expect(shownRows()).toHaveLength(4));
    fireEvent.click(screen.getByTestId("kb-matrix-vfilter-add"));
    const dates = await screen.findByTestId("kb-matrix-vfilter-form");
    fireEvent.change(within(dates).getByLabelText(m.filter_column), { target: { value: "due" } });
    fireEvent.change(within(dates).getByLabelText(m.filter_condition), { target: { value: "lt" } });
    expect(within(dates).getByRole("option", { name: m.filter_op_lt_date })).toBeInTheDocument();
    fireEvent.change(within(dates).getByLabelText(m.filter_value), { target: { value: "2026-03-01" } });
    fireEvent.click(within(dates).getByRole("button", { name: m.filter_apply }));
    await waitFor(() => expect(shownRows()).toEqual(["a.pdf"]));

    // Text columns contain; any column can be empty.
    fireEvent.click(screen.getByRole("button", { name: `Remove filter Due: ${m.filter_op_lt_date} 2026-03-01` }));
    fireEvent.click(screen.getByTestId("kb-matrix-vfilter-add"));
    const empties = await screen.findByTestId("kb-matrix-vfilter-form");
    fireEvent.change(within(empties).getByLabelText(m.filter_column), { target: { value: "note" } });
    expect([...(within(empties).getByLabelText(m.filter_condition) as HTMLSelectElement).options].map((o) => o.value)).toEqual(["eq", "contains", "empty"]);
    fireEvent.change(within(empties).getByLabelText(m.filter_condition), { target: { value: "empty" } });
    expect(within(empties).queryByLabelText(m.filter_value)).toBeNull();
    fireEvent.click(within(empties).getByRole("button", { name: m.filter_apply }));
    await waitFor(() => expect(shownRows()).toEqual(["b.pdf", "c.pdf"]));
    expect(query().getAll("f")).toEqual(["empty:note"]);
  });

  it("restores search, filters and sort from the URL on load", async () => {
    window.history.replaceState(null, "", "/knowledge?tab=matrix&view=mx_test&q=pdf&f=status:unverified&f=eq:pay:prepaid&sort=asc:amount");
    renderView();
    await waitFor(() => expect(shownRows()).toEqual(["c.pdf", "a.pdf"]));
    expect(screen.getByTestId("kb-matrix-search")).toHaveValue("pdf");
    expect(screen.getByTestId("kb-matrix-status-filter")).toHaveTextContent(`Status: ${m.status_unverified}`);
    expect(screen.getByTestId("kb-matrix-vfilter-pay")).toBeInTheDocument();
    // Nothing rewrote the query on load.
    expect(window.location.search).toBe("?tab=matrix&view=mx_test&q=pdf&f=status:unverified&f=eq:pay:prepaid&sort=asc:amount");
  });

  it("searches as the user types and shows a cleared empty state", async () => {
    renderView();
    fireEvent.change(await screen.findByTestId("kb-matrix-search"), { target: { value: "monthly" } });
    await waitFor(() => expect(shownRows()).toEqual(["b.pdf"]));
    expect(query().get("q")).toBe("monthly");
    fireEvent.change(screen.getByTestId("kb-matrix-search"), { target: { value: "nothing like this" } });
    const empty = await screen.findByTestId("kb-matrix-filterempty");
    expect(empty).toHaveTextContent(m.filter_empty_title);
    fireEvent.click(within(empty).getByRole("button", { name: m.filter_clear }));
    await waitFor(() => expect(shownRows()).toHaveLength(4));
    expect(screen.getByTestId("kb-matrix-search")).toHaveValue("");
  });

  it("shows verified progress over the readable rows", async () => {
    renderView();
    const progress = await screen.findByTestId("kb-matrix-progress");
    // 3 readable rows × 4 columns; a's amount and c's pay are verified.
    expect(progress).toHaveTextContent("Verified 2 / 12");
    expect(progress).toHaveAttribute("aria-valuenow", "2");
    expect(progress).toHaveAttribute("aria-valuemax", "12");
  });
});

describe("bulk verify from the column menu", () => {
  it("confirms how many cells it marks, then reports what was skipped", async () => {
    mocks.verifyCells.mockResolvedValue({ changed: 2, skipped: { not_done: 0, stale: 0, unavailable: 1, already: 1 }, batch_id: "mxbat_1" });
    renderView();
    await pickStatus(m.status_failed);
    fireEvent.click(await screen.findByRole("button", { name: "menu Amount" }));
    const item = await screen.findByTestId("kb-matrix-colmenu-verify");
    expect(item).toHaveTextContent(`${m.colmenu_verify}…`);
    fireEvent.click(item);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Mark column “Amount” as verified?");
    expect(dialog).toHaveTextContent("2 cells are marked verified, with your name and the time.");
    // The failed filter hides rows; the whole column is marked.
    expect(dialog).toHaveTextContent(m.verify_column_hidden);
    fireEvent.click(within(dialog).getByRole("button", { name: m.verify_column_confirm }));
    await waitFor(() => expect(mocks.verifyCells).toHaveBeenCalledWith("mx_test", { column_id: "amount", verified: true }));
    expect(await screen.findByText("Marked 2 cells as verified. Not marked: 1 cell you can’t view.")).toBeInTheDocument();
    expect(mocks.reload).toHaveBeenCalledTimes(2);
  });

  it("disables the item with the reason when nothing in the column can be marked", async () => {
    // Note: a's edited value is already verified, b's failed, c has none.
    const [a, ...rest] = SNAPSHOT.rows;
    renderView({ ...SNAPSHOT, rows: [{ ...a, cells: { ...a.cells, note: cell({ edited: true, verified: true }) } }, ...rest] });
    fireEvent.click(await screen.findByRole("button", { name: "menu Note" }));
    const item = await screen.findByTestId("kb-matrix-colmenu-verify");
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText(m.colmenu_verify_none)).toBeInTheDocument();
    fireEvent.click(item);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("names the stale and unfinished cells it leaves", async () => {
    renderView();
    fireEvent.click(await screen.findByRole("button", { name: "menu Due" }));
    fireEvent.click(await screen.findByTestId("kb-matrix-colmenu-verify"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("1 cell is marked verified");
    expect(dialog).toHaveTextContent("2 unfinished or stale cells are not marked.");
    expect(dialog).not.toHaveTextContent(m.verify_column_hidden);
  });
});

describe("failed view and keyboard", () => {
  it("View failed sets the failed status filter and focuses the first failed cell", async () => {
    renderView();
    fireEvent.click(await screen.findByRole("button", { name: "view failed" }));
    await waitFor(() => expect(shownRows()).toEqual(["b.pdf"]));
    expect(query().getAll("f")).toEqual(["status:failed"]);
    expect(screen.getByTestId("matrix-grid")).toHaveAttribute("data-focus", "b:note");
    expect(screen.getByText(m.filter_hint)).toBeInTheDocument();
    // Esc on the matrix clears the filter.
    fireEvent.keyDown(screen.getByTestId("kb-matrix-app-m2"), { key: "Escape" });
    await waitFor(() => expect(shownRows()).toHaveLength(4));
  });

  it("moves the cell focus through the rows in the order shown", async () => {
    window.history.replaceState(null, "", "/knowledge?tab=matrix&view=mx_test&sort=asc:amount");
    renderView();
    await waitFor(() => expect(shownRows()).toEqual(["c.pdf", "b.pdf", "a.pdf", "gone"]));
    fireEvent.click(screen.getByRole("button", { name: "open first" }));
    const app = screen.getByTestId("kb-matrix-app-m2");
    fireEvent.keyDown(app, { key: "Escape" });
    await waitFor(() => expect(screen.getByTestId("matrix-grid")).toHaveAttribute("data-focus", "c:amount"));
    fireEvent.keyDown(app, { key: "ArrowDown" });
    expect(screen.getByTestId("matrix-grid")).toHaveAttribute("data-focus", "b:amount");
    // Typing in the search box does not move the cell focus.
    fireEvent.keyDown(screen.getByTestId("kb-matrix-search"), { key: "ArrowDown" });
    expect(screen.getByTestId("matrix-grid")).toHaveAttribute("data-focus", "b:amount");
  });
});
