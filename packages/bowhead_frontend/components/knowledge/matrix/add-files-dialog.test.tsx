/**
 * Add files dialog (document matrix step 1, S2): search by name, groups by scope, disabled files keep their reason.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { KbFileDTO } from "@/lib/kb-api";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({ fetchKbFiles: vi.fn() }));
vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return { ...actual, fetchKbFiles: (...args: unknown[]) => mocks.fetchKbFiles(...args) };
});

import { AddFilesDialog } from "./add-files-dialog";

const m = messagesEn.kbMatrix;

function file(id: string, filename: string, over: Partial<KbFileDTO> = {}): KbFileDTO {
  return {
    file_id: id,
    filename,
    mime_type: "application/pdf",
    size_bytes: 2048,
    scope: "space",
    parse_status: "done",
    uploaded_at: "2026-11-20T08:00:00Z",
    ...over,
  } as KbFileDTO;
}

const FILES = [
  file("kbf_new", "bakery-supply.pdf"),
  file("kbf_in", "equipment-service.pdf"),
  file("kbf_team", "group-terms.docx", { scope: "team", mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" }),
  file("kbf_mine", "my-notes.docx", { scope: "personal" }),
  file("kbf_wait", "scan.pdf", { parse_status: "parsing" }),
];

function renderDialog(onAdd = vi.fn(), personalFiles?: "allowed" | "space_matrix" | "unavailable") {
  render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <AddFilesDialog
        matrixName="Supplier contracts"
        presentFileIds={new Set(["kbf_in"])}
        personalFiles={personalFiles}
        busy={false}
        onClose={vi.fn()}
        onAdd={onAdd}
      />
    </NextIntlClientProvider>,
  );
  return onAdd;
}

beforeEach(() => {
  mocks.fetchKbFiles.mockReset();
  mocks.fetchKbFiles.mockResolvedValue({ files: FILES, total: FILES.length, has_more: false });
});
afterEach(cleanup);

describe("add files dialog", () => {
  it("groups readable files by scope and explains every file that cannot be added", async () => {
    renderDialog();
    expect(await screen.findByText("bakery-supply.pdf")).toBeInTheDocument();
    expect(mocks.fetchKbFiles).toHaveBeenCalledWith(expect.objectContaining({ scope: "all" }), expect.anything());
    const groups = screen.getAllByRole("group").map((g) => g.firstElementChild?.textContent);
    expect(groups).toEqual([m.add_files_group_space, m.add_files_group_team, m.add_files_group_personal]);

    const reasonOf = (id: string) => within(screen.getByTestId(`kb-matrix-addfiles-${id}`));
    expect(reasonOf("kbf_in").getByRole("checkbox")).toBeDisabled();
    expect(reasonOf("kbf_in").getByText(m.add_files_reason_present)).toBeInTheDocument();
    expect(reasonOf("kbf_mine").getByRole("checkbox")).toBeDisabled();
    expect(reasonOf("kbf_mine").getByText(m.add_files_reason_personal)).toBeInTheDocument();
    expect(reasonOf("kbf_wait").getByRole("checkbox")).toBeDisabled();
    expect(reasonOf("kbf_wait").getByText(m.add_files_reason_not_ready)).toBeInTheDocument();
    expect(reasonOf("kbf_new").getByRole("checkbox")).toBeEnabled();
    expect(reasonOf("kbf_team").getByRole("checkbox")).toBeEnabled();
  });

  it("adds the checked files in the order they were checked", async () => {
    const onAdd = renderDialog();
    await screen.findByText("bakery-supply.pdf");
    const confirm = screen.getByTestId("kb-matrix-addfiles-confirm");
    expect(confirm).toBeDisabled();
    fireEvent.click(within(screen.getByTestId("kb-matrix-addfiles-kbf_team")).getByRole("checkbox"));
    fireEvent.click(within(screen.getByTestId("kb-matrix-addfiles-kbf_new")).getByRole("checkbox"));
    expect(confirm).toHaveTextContent("Add 2 files");
    fireEvent.click(confirm);
    expect(onAdd).toHaveBeenCalledWith(["kbf_team", "kbf_new"]);
  });

  it("searches file names on the server and says when nothing matches", async () => {
    renderDialog();
    await screen.findByText("bakery-supply.pdf");
    mocks.fetchKbFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    fireEvent.change(screen.getByRole("searchbox", { name: m.add_files_search }), { target: { value: "lease" } });
    await waitFor(() => expect(mocks.fetchKbFiles).toHaveBeenLastCalledWith(expect.objectContaining({ scope: "all", q: "lease" }), expect.anything()));
    expect(await screen.findByText("No file names contain “lease”.")).toBeInTheDocument();
  });

  it("takes personal files only in an only-me matrix and otherwise says this matrix is visible to Space members (S8)", async () => {
    const onAdd = renderDialog(vi.fn(), "allowed");
    await screen.findByText("my-notes.docx");
    const mine = within(screen.getByTestId("kb-matrix-addfiles-kbf_mine"));
    expect(mine.getByRole("checkbox")).toBeEnabled();
    fireEvent.click(mine.getByRole("checkbox"));
    fireEvent.click(screen.getByTestId("kb-matrix-addfiles-confirm"));
    expect(onAdd).toHaveBeenCalledWith(["kbf_mine"]);
    cleanup();

    renderDialog(vi.fn(), "space_matrix");
    await screen.findByText("my-notes.docx");
    const refused = within(screen.getByTestId("kb-matrix-addfiles-kbf_mine"));
    expect(refused.getByRole("checkbox")).toBeDisabled();
    expect(refused.getByText(m.add_files_reason_personal_space)).toBeInTheDocument();
  });

  it("states a failed load with a retry", async () => {
    mocks.fetchKbFiles.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    renderDialog();
    expect(await screen.findByRole("alert")).toHaveTextContent(m.add_files_load_failed);
    fireEvent.click(screen.getByRole("button", { name: m.matrix_unavailable_retry }));
    expect(await screen.findByText("bakery-supply.pdf")).toBeInTheDocument();
  });
});
