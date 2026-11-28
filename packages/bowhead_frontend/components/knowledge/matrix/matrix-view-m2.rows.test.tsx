/**
 * Managing a matrix's rows and the matrix itself (document matrix step 1, S2): remove confirmation states what goes,
 * a refusal names who can remove, bulk removal, the add-files row, and delete with its effect on cells and runs.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MatrixApiError } from "@/lib/kb-matrix/api-error";
import { setMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixCellVM, MatrixRowVM, MatrixSnapshot } from "@/lib/kb-matrix/types";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({
  reload: vi.fn(),
  removeMatrixFiles: vi.fn(),
  addMatrixFiles: vi.fn(),
  deleteMatrix: vi.fn(),
  fetchKbFiles: vi.fn(),
}));

vi.mock("@/lib/kb-matrix/run-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/run-api")>();
  return {
    ...actual,
    removeMatrixFiles: (...args: unknown[]) => mocks.removeMatrixFiles(...args),
    addMatrixFiles: (...args: unknown[]) => mocks.addMatrixFiles(...args),
    deleteMatrix: (...args: unknown[]) => mocks.deleteMatrix(...args),
  };
});
vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return { ...actual, fetchKbFiles: (...args: unknown[]) => mocks.fetchKbFiles(...args) };
});
vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/kb-matrix/use-matrix-poll", () => ({ useMatrixPolling: () => ({ reload: mocks.reload }) }));
vi.mock("./run-banner", () => ({ RunBanners: () => null }));
// The grid's own rendering is covered elsewhere; this stub exposes its row controls.
vi.mock("./matrix-grid", () => ({
  MatrixGrid: ({ snapshot, rowControls }: { snapshot: MatrixSnapshot; rowControls?: { onRemove: (r: MatrixRowVM) => void; onToggleAll: (ids: string[], on: boolean) => void } }) => (
    <div data-testid="matrix-grid">
      {rowControls &&
        snapshot.rows.map((r) => (
          <button key={r.fileId} type="button" onClick={() => rowControls.onRemove(r)}>
            {`remove ${r.fileName}`}
          </button>
        ))}
      {rowControls && (
        <button type="button" onClick={() => rowControls.onToggleAll(snapshot.rows.map((r) => r.fileId), true)}>
          select all
        </button>
      )}
    </div>
  ),
}));

import { MatrixViewM2 } from "./matrix-view-m2";

const m = messagesEn.kbMatrix;

const done = (over: Partial<MatrixCellVM> = {}): MatrixCellVM => ({ status: "done", value: "x", variants: [], edited: false, verified: false, ...over });

function snapshotWith(canManage: boolean): MatrixSnapshot {
  return {
    matrixId: "mx_test",
    title: "Contracts",
    readOnly: false,
    seq: 1,
    canEdit: true,
    canManage,
    runs: [],
    columns: [
      { columnId: "c1", name: "Amount", type: "question" },
      { columnId: "c2", name: "Term", type: "question" },
    ],
    rows: [
      { fileId: "kbf_a", fileName: "a.pdf", missing: false, extractFailed: false, recordId: "", approved: false, cells: { c1: done({ edited: true }), c2: done({ verified: true }) } },
      { fileId: "kbf_b", fileName: "b.pdf", missing: false, extractFailed: false, recordId: "", approved: false, cells: { c1: done() } },
    ],
  };
}

function renderView(snapshot: MatrixSnapshot, onBack = vi.fn()) {
  mocks.reload.mockImplementation(async () => setMatrixSnapshot(snapshot));
  render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <MatrixViewM2 matrixId="mx_test" onBack={onBack} />
    </NextIntlClientProvider>,
  );
  return onBack;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.fetchKbFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
});
afterEach(() => {
  cleanup();
  setMatrixSnapshot(null);
});

describe("removing files", () => {
  it("states the edited and verified cells, and who can remove them, without a confirm for members", async () => {
    renderView(snapshotWith(false));
    fireEvent.click(await screen.findByRole("button", { name: "remove a.pdf" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Remove “a.pdf” from the matrix?")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Their 2 cells in this matrix are deleted.");
    expect(dialog).toHaveTextContent("Of these, 1 were edited by hand and 1 were verified");
    expect(dialog).toHaveTextContent(m.remove_blocked);
    expect(within(dialog).queryByTestId("kb-matrix-remove-confirm")).toBeNull();
  });

  it("maps the server's refusal to who can remove the files, with its counts", async () => {
    mocks.removeMatrixFiles.mockRejectedValue(
      new MatrixApiError(403, "matrix_remove_requires_owner", "RAW", { code: "matrix_remove_requires_owner", edited: 3, verified: 1 }),
    );
    renderView(snapshotWith(false));
    fireEvent.click(await screen.findByRole("button", { name: "remove b.pdf" }));
    fireEvent.click(await screen.findByTestId("kb-matrix-remove-confirm"));
    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() =>
      expect(within(dialog).getByRole("alert")).toHaveTextContent(
        "These files have 3 edited and 1 verified cells. Only the matrix creator, a Space admin or a Team admin can remove them",
      ),
    );
    expect(mocks.removeMatrixFiles).toHaveBeenCalledWith("mx_test", ["kbf_b"]);
    expect(within(dialog).queryByTestId("kb-matrix-remove-confirm")).toBeNull();
  });

  it("removes the selected rows together for the creator or an admin", async () => {
    mocks.removeMatrixFiles.mockResolvedValue({ removed: ["kbf_a", "kbf_b"], files_count: 0, edited: 1, verified: 1 });
    renderView(snapshotWith(true));
    fireEvent.click(await screen.findByRole("button", { name: "select all" }));
    const bar = await screen.findByTestId("kb-matrix-selbar");
    expect(bar).toHaveTextContent("2 files selected");
    fireEvent.click(within(bar).getByTestId("kb-matrix-selbar-remove"));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Remove 2 files from the matrix?")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByTestId("kb-matrix-remove-confirm"));
    await waitFor(() => expect(mocks.removeMatrixFiles).toHaveBeenCalledWith("mx_test", ["kbf_a", "kbf_b"]));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(screen.queryByTestId("kb-matrix-selbar")).toBeNull();
  });
});

describe("adding files and deleting the matrix", () => {
  it("opens the file picker from the row under the grid and states a refused add in it", async () => {
    mocks.fetchKbFiles.mockResolvedValue({
      files: [{ file_id: "kbf_c", filename: "c.pdf", mime_type: "application/pdf", size_bytes: 10, scope: "space", parse_status: "done", uploaded_at: "2026-11-20T08:00:00Z" }],
      total: 1,
      has_more: false,
    });
    mocks.addMatrixFiles.mockRejectedValue(
      new MatrixApiError(422, "matrix_file_limit", "RAW", { code: "matrix_file_limit", limit: 500, count: 500, requested: 501 }),
    );
    renderView(snapshotWith(false));
    fireEvent.click(await screen.findByTestId("kb-matrix-addfiles-row"));
    const dialog = await screen.findByRole("dialog", { name: "Add files to “Contracts”" });
    fireEvent.click(within(await within(dialog).findByTestId("kb-matrix-addfiles-kbf_c")).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByTestId("kb-matrix-addfiles-confirm"));
    await waitFor(() => expect(mocks.addMatrixFiles).toHaveBeenCalledWith("mx_test", ["kbf_c"]));
    expect(await within(dialog).findByTestId("kb-matrix-addfiles-error")).toHaveTextContent(
      "A matrix can hold up to 500 files. This one has 500, and this change would bring it to 501.",
    );
  });

  it("confirms the delete with its effect on cells, then leaves for the list", async () => {
    mocks.deleteMatrix.mockResolvedValue(undefined);
    const onBack = renderView(snapshotWith(true));
    fireEvent.click(await screen.findByRole("button", { name: m.matrix_more }));
    fireEvent.click(await screen.findByRole("menuitem", { name: `${m.menu_delete_matrix}…` }));
    const dialog = await screen.findByRole("dialog", { name: "Delete matrix “Contracts”?" });
    expect(dialog).toHaveTextContent("The results in its 3 cells are deleted with it");
    fireEvent.click(within(dialog).getByRole("button", { name: m.delete_matrix_confirm }));
    await waitFor(() => expect(mocks.deleteMatrix).toHaveBeenCalledWith("mx_test"));
    await waitFor(() => expect(onBack).toHaveBeenCalled());
  });
});
