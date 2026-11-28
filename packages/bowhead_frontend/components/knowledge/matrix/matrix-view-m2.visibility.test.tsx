/**
 * Only-me matrices on the matrix page (document matrix step 1, S8): the "仅自己可见" badge, the creator's ⋯ item that
 * changes who can see the matrix, its confirmation naming who loses access, refusals said before asking (personal
 * files, other members' runs) or from the server, and the column editor without the source discovery purpose.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastContainer } from "@/components/chat/toast-container";
import { MatrixApiError } from "@/lib/kb-matrix/api-error";
import type { MatrixVisibilityPreview } from "@/lib/kb-matrix/run-api";
import { setMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixColumnVM, MatrixSnapshot } from "@/lib/kb-matrix/types";
import { _clearAllToasts } from "@/lib/use-toast";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({ reload: vi.fn(), fetchMatrixVisibility: vi.fn(), setMatrixVisibility: vi.fn() }));

vi.mock("@/lib/kb-matrix/run-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/run-api")>();
  return {
    ...actual,
    fetchMatrixVisibility: (...args: unknown[]) => mocks.fetchMatrixVisibility(...args),
    setMatrixVisibility: (...args: unknown[]) => mocks.setMatrixVisibility(...args),
  };
});
vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/kb-matrix/use-matrix-poll", () => ({ useMatrixPolling: () => ({ reload: mocks.reload }) }));
vi.mock("./run-banner", () => ({ RunBanners: () => null }));
vi.mock("./matrix-grid", () => ({
  MatrixGrid: ({ snapshot, onColMenu }: { snapshot: MatrixSnapshot; onColMenu?: (col: MatrixColumnVM, e: React.MouseEvent) => void }) => (
    <div data-testid="matrix-grid">
      {snapshot.columns.map((c) => (
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
    </div>
  ),
}));

import { MatrixViewM2 } from "./matrix-view-m2";

const m = messagesEn.kbMatrix;

function snapshot(over: Partial<MatrixSnapshot> = {}): MatrixSnapshot {
  return {
    matrixId: "mx_test",
    title: "Contracts",
    readOnly: false,
    seq: 1,
    canEdit: true,
    canManage: true,
    visibility: "space",
    canChangeVisibility: true,
    runs: [],
    columns: [{ columnId: "c1", name: "Amount", type: "question" }],
    rows: [{ fileId: "kbf_a", fileName: "a.pdf", missing: false, extractFailed: false, recordId: "", approved: false, cells: {} }],
    ...over,
  };
}

function preview(over: Partial<MatrixVisibilityPreview> = {}): MatrixVisibilityPreview {
  return {
    visibility: "space",
    can_change: true,
    contributors: [],
    contributors_count: 0,
    personal_files: [],
    personal_files_count: 0,
    hidden_personal_files: 0,
    active_runs_by_others: 0,
    ...over,
  };
}

function renderView(snap: MatrixSnapshot) {
  mocks.reload.mockImplementation(async () => setMatrixSnapshot(snap));
  render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <MatrixViewM2 matrixId="mx_test" onBack={vi.fn()} />
      <ToastContainer />
    </NextIntlClientProvider>,
  );
}

async function openMenu() {
  fireEvent.click(await screen.findByRole("button", { name: m.matrix_more }));
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.setItem("bowhead.space_id", "ws_a");
});
afterEach(() => {
  cleanup();
  _clearAllToasts();
  setMatrixSnapshot(null);
  window.localStorage.clear();
});

describe("who can see the matrix", () => {
  it("lets the creator make it private after a confirmation that names who loses access", async () => {
    mocks.fetchMatrixVisibility.mockResolvedValue(preview({ contributors: ["Jun", "Mei"], contributors_count: 3 }));
    mocks.setMatrixVisibility.mockResolvedValue("private");
    renderView(snapshot());
    expect(screen.queryByTestId("kb-matrix-private-badge")).toBeNull();
    await openMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: m.menu_visibility_private }));
    const dialog = await screen.findByRole("dialog", { name: "Make “Contracts” visible to only you?" });
    expect(within(dialog).getByTestId("kb-matrix-visibility-contributors")).toHaveTextContent(
      "3 members have edited or verified cells or run this matrix (Jun, Mei). Their edits and verifications stay, but they will no longer be able to open it.",
    );
    expect(mocks.fetchMatrixVisibility).toHaveBeenCalledWith("mx_test");
    fireEvent.click(within(dialog).getByRole("button", { name: m.visibility_private_confirm }));
    await waitFor(() => expect(mocks.setMatrixVisibility).toHaveBeenCalledWith("mx_test", "private"));
    expect(await screen.findByText("“Contracts” is now visible to only you")).toBeInTheDocument();
  });

  it("shows the badge and names the personal files that keep a private matrix from being shared", async () => {
    mocks.fetchMatrixVisibility.mockResolvedValue(
      preview({ visibility: "private", personal_files: [{ file_id: "kbf_p", name: "notes.docx" }], personal_files_count: 1 }),
    );
    renderView(snapshot({ visibility: "private" }));
    expect(await screen.findByTestId("kb-matrix-private-badge")).toHaveTextContent(m.visibility_badge_private);
    await openMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: m.menu_visibility_space }));
    expect(
      await screen.findByText(
        "This matrix holds your personal file notes.docx. Personal files can only stay in matrices visible to only you; remove them from the matrix first, then make it visible to Space members.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.setMatrixVisibility).not.toHaveBeenCalled();
  });

  it("says why other members' runs block the change, and shows the server's refusal when it decides", async () => {
    mocks.fetchMatrixVisibility.mockResolvedValueOnce(preview({ active_runs_by_others: 2 }));
    renderView(snapshot());
    await openMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: m.menu_visibility_private }));
    expect(await screen.findByText(/Other members still have 2 runs in progress/)).toBeInTheDocument();
    expect(mocks.setMatrixVisibility).not.toHaveBeenCalled();

    mocks.fetchMatrixVisibility.mockResolvedValueOnce(preview());
    mocks.setMatrixVisibility.mockRejectedValue(new MatrixApiError(409, "matrix_visibility_runs_active", "RAW", { count: 1 }));
    await openMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: m.menu_visibility_private }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: m.visibility_private_confirm }));
    expect(await screen.findByText(/Other members still have 1 run in progress/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("matrix_visibility_runs_active");
  });

  it("is not offered to anyone but the creator", async () => {
    renderView(snapshot({ canChangeVisibility: false }));
    await openMenu();
    await screen.findByRole("menuitem", { name: m.menu_rename });
    expect(screen.queryByRole("menuitem", { name: m.menu_visibility_private })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: m.menu_visibility_space })).toBeNull();
  });

  it("leaves the source discovery purpose out of the column editor of a private matrix", async () => {
    renderView(snapshot({ visibility: "private" }));
    fireEvent.click(await screen.findByRole("button", { name: "menu Amount" }));
    fireEvent.click(await screen.findByText(m.colmenu_edit));
    expect(await screen.findByTestId("kb-matrix-editcol-navigation-private")).toHaveTextContent(m.navigation_private_note);
    expect(screen.queryByText(m.navigation_role)).toBeNull();
  });
});
