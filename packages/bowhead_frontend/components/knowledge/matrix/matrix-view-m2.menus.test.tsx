/**
 * Matrix run menu (P2 batch 5 accessibility fix): a shared menu on body with menu semantics, arrow keys and Esc back
 * to the caret. Step 1 (S3): unfinished / stale / failed with snapshot counts, then trial and rerun all ("…"), whose
 * second line says what it keeps; a trial opens the confirmation with the trial rows priced.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixCellVM, MatrixRowVM, MatrixSnapshot } from "@/lib/kb-matrix/types";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({ estimateRun: vi.fn(), startRun: vi.fn(), reload: vi.fn() }));

vi.mock("@/lib/kb-matrix/run-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/run-api")>();
  return {
    ...actual,
    estimateRun: (...args: unknown[]) => mocks.estimateRun(...args),
    startRun: (...args: unknown[]) => mocks.startRun(...args),
  };
});
vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/kb-matrix/use-matrix-poll", () => ({ useMatrixPolling: () => ({ reload: mocks.reload }) }));
vi.mock("./matrix-grid", () => ({ MatrixGrid: () => <div data-testid="matrix-grid" /> }));
vi.mock("./run-banner", () => ({ RunBanners: () => null }));

import { MatrixViewM2 } from "./matrix-view-m2";

const snapshot: MatrixSnapshot = {
  matrixId: "mx_test", title: "Contract matrix", readOnly: false, columns: [], rows: [], seq: 1, canEdit: true, runs: [],
};
const m = messagesEn.kbMatrix;

const cell = (over: Partial<MatrixCellVM>): MatrixCellVM => ({ status: "done", value: "v", variants: [], edited: false, verified: false, ...over });
const row = (fileId: string, cells: MatrixRowVM["cells"]): MatrixRowVM => ({
  fileId, fileName: `${fileId}.pdf`, missing: false, extractFailed: false, recordId: "", approved: false, cells,
});
/** 5 rows × 2 columns: 3 empty (rows 3–4), 2 failed (one edited), 1 stale, 3 verified (one failed, one running), 1 edited. */
const counted: MatrixSnapshot = {
  ...snapshot,
  columns: [{ columnId: "a", name: "Amount", type: "currency" }, { columnId: "b", name: "Term", type: "date" }],
  rows: [
    row("f1", { a: cell({ verified: true }), b: cell({ verified: true, stale: true }) }),
    row("f2", { a: cell({ status: "failed" }), b: cell({ status: "failed", edited: true }) }),
    row("f3", { a: cell({ status: "failed", verified: true }), b: undefined }),
    row("f4", { a: undefined, b: cell({ status: "running", verified: true }) }),
    row("f5", { a: undefined, b: cell({ edited: true, stale: true }) }),
  ],
};

function renderView(snap: MatrixSnapshot = snapshot) {
  mocks.reload.mockImplementation(async () => setMatrixSnapshot(snap));
  return render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <MatrixViewM2 matrixId="mx_test" onBack={vi.fn()} />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.estimateRun.mockReturnValue(new Promise(() => {}));
});
afterEach(() => {
  cleanup();
  setMatrixSnapshot(null);
});

describe("matrix run menu", () => {
  it("is a shared menu with menu items, keyboard focus and Esc back to the caret", async () => {
    renderView();
    const caret = await screen.findByRole("button", { name: m.run_options });
    caret.focus();
    fireEvent.click(caret, { detail: 0 });
    const menu = await screen.findByTestId("kb-matrix-runmenu");
    expect(menu).toHaveAttribute("role", "menu");
    expect(screen.getByTestId("kb-matrix-app-m2").contains(menu)).toBe(false);
    const items = within(menu).getAllByRole("menuitem", { hidden: true });
    expect(items.map((i) => i.querySelector("span")?.textContent)).toEqual([
      m.runmenu_all,
      m.runmenu_stale,
      m.runmenu_failed,
      "Try on 3 Files First…",
      `${m.runmenu_force}…`,
    ]);
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    fireEvent.keyDown(items[0], { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(items[1]));
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("kb-matrix-runmenu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(caret));
  });

  it("opens the run confirmation when an item is picked", async () => {
    renderView();
    fireEvent.click(await screen.findByRole("button", { name: m.run_options }), { detail: 0 });
    const menu = await screen.findByTestId("kb-matrix-runmenu");
    fireEvent.click(within(menu).getAllByRole("menuitem", { hidden: true })[4]);
    await waitFor(() => expect(mocks.estimateRun).toHaveBeenCalledWith("mx_test", { kind: "force_all" }));
  });

  it("prices failed cells only from Rerun Failed Only", async () => {
    renderView(counted);
    fireEvent.click(await screen.findByRole("button", { name: m.run_options }), { detail: 0 });
    fireEvent.click(await screen.findByTestId("kb-matrix-runmenu-failed"));
    await waitFor(() => expect(mocks.estimateRun).toHaveBeenCalledWith("mx_test", { kind: "failed" }));
  });

  it("counts cells from the snapshot and says what rerun all keeps", async () => {
    renderView(counted);
    fireEvent.click(await screen.findByRole("button", { name: m.run_options }), { detail: 0 });
    await screen.findByTestId("kb-matrix-runmenu");
    const tail = (id: string) => screen.getByTestId(id).querySelector(".tail")?.textContent;
    expect(tail("kb-matrix-runmenu-all")).toBe("5 cells"); // 3 empty + 2 failed without an edit
    expect(tail("kb-matrix-runmenu-stale")).toBe("1 cell");
    expect(tail("kb-matrix-runmenu-failed")).toBe("2 cells");
    expect(screen.getByTestId("kb-matrix-runmenu-trial").querySelector("span")?.textContent).toBe("Try on 3 Files First…"); // f2, f3, f4 of 4 rows
    const force = screen.getByTestId("kb-matrix-runmenu-force");
    const keeps = document.getElementById(force.getAttribute("aria-describedby") ?? "");
    // f1's two verified cells (the failed and the running verified cells are not kept); f2/b and f5/b are edited.
    expect(keeps?.textContent).toBe("Keeps 2 verified cells and 2 edited cells.");
  });

  it("disables the trial with its reason when every cell is finished", async () => {
    renderView({ ...counted, rows: [counted.rows[0]] });
    fireEvent.click(await screen.findByRole("button", { name: m.run_options }), { detail: 0 });
    const trial = await screen.findByTestId("kb-matrix-runmenu-trial");
    expect(trial).toHaveAttribute("aria-disabled", "true");
    expect(document.getElementById(trial.getAttribute("aria-describedby") ?? "")?.textContent).toBe(m.runmenu_trial_none);
    fireEvent.click(trial);
    expect(mocks.estimateRun).not.toHaveBeenCalled();
  });

  it("runs a trial on the rows the estimate names", async () => {
    const full = { ...ESTIMATE, files: 5, cells_pending: 8, trial_file_ids: ["f2", "f3", "f4"] };
    const trial = { ...ESTIMATE, files: 3, cells_pending: 3, credits_estimated: 9, trial_file_ids: ["f2", "f3", "f4"] };
    mocks.estimateRun.mockImplementation(async (_id: string, scope: { file_ids?: string[] }) => (scope.file_ids ? trial : full));
    mocks.startRun.mockResolvedValue({ run_id: "mxrun_1", cells_total: 3, skipped_edited: 0, credits_estimated: 9 });
    renderView(counted);
    fireEvent.click(await screen.findByRole("button", { name: m.run_options }), { detail: 0 });
    fireEvent.click(await screen.findByTestId("kb-matrix-runmenu-trial"));
    await waitFor(() => expect(mocks.estimateRun).toHaveBeenCalledWith("mx_test", { kind: "all_pending", file_ids: ["f2", "f3", "f4"] }));
    const option = within(await screen.findByTestId("kb-matrix-run-trial")).getByRole("checkbox");
    expect(option).toBeChecked();
    const confirm = await screen.findByTestId("kb-matrix-run-confirm");
    await waitFor(() => expect(confirm).toHaveTextContent("Try 3 files"));
    fireEvent.click(confirm);
    await waitFor(() => expect(mocks.startRun).toHaveBeenCalledWith("mx_test", { kind: "all_pending", file_ids: ["f2", "f3", "f4"] }));
  });

  it("offers the trial in the confirmation and runs the full scope while it stays off", async () => {
    const full = { ...ESTIMATE, files: 5, cells_pending: 8, trial_file_ids: ["f2", "f3", "f4"] };
    mocks.estimateRun.mockResolvedValue(full);
    mocks.startRun.mockResolvedValue({ run_id: "mxrun_1", cells_total: 8, skipped_edited: 0, credits_estimated: 30 });
    renderView(counted);
    fireEvent.click(await screen.findByTestId("kb-matrix-run"));
    const option = within(await screen.findByTestId("kb-matrix-run-trial")).getByRole("checkbox");
    expect(option).not.toBeChecked();
    fireEvent.click(await screen.findByTestId("kb-matrix-run-confirm"));
    await waitFor(() => expect(mocks.startRun).toHaveBeenCalledWith("mx_test", { kind: "all_pending" }));
  });
});

const ESTIMATE = {
  cells_pending: 1,
  calls: 1,
  credits_estimated: 3,
  eta_s: 30,
  skipped_edited: 0,
  available_credits: 1000,
  team_pool_tighter: false,
  warn: false,
  blocked: false,
  reading_credits: 1,
  columns: [],
};
