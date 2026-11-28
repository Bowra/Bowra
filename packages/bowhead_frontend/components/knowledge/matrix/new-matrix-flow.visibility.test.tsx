/**
 * "谁能看到" in the new-matrix flow (document matrix step 1, S8): offered only while only-me matrices are enabled,
 * Space members by default; "仅自己" also lists the viewer's personal files and creates a private matrix.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({ createMatrix: vi.fn(), fetchKbFiles: vi.fn() }));

vi.mock("@/lib/kb-matrix/run-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/run-api")>();
  return { ...actual, createMatrix: (...args: unknown[]) => mocks.createMatrix(...args) };
});
vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return { ...actual, fetchKbFiles: (...args: unknown[]) => mocks.fetchKbFiles(...args) };
});

import { NewMatrixFlow } from "./new-matrix-flow";

const m = messagesEn.kbMatrix;

function page(files: Array<{ id: string; name: string }>) {
  return {
    files: files.map((f) => ({ file_id: f.id, filename: f.name, mime_type: "text/markdown", parse_status: "done" })),
    has_more: false,
  };
}

function renderFlow(privateOption: boolean) {
  render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <NewMatrixFlow workspaceId="ws_a" privateOption={privateOption} onCancel={vi.fn()} onCreated={vi.fn()} onError={vi.fn()} />
    </NextIntlClientProvider>,
  );
}

async function toFiles() {
  fireEvent.click(screen.getByRole("button", { name: m.newflow_next }));
  await screen.findByText("space.md");
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.createMatrix.mockResolvedValue({ matrix_id: "mx_new" });
  mocks.fetchKbFiles.mockImplementation(async ({ scope }: { scope: string }) =>
    scope === "personal" ? page([{ id: "kbf_mine", name: "mine.md" }]) : page([{ id: "kbf_space", name: "space.md" }]),
  );
});
afterEach(cleanup);

describe("who can see a new matrix", () => {
  it("is not offered while only-me matrices are off, and nothing is sent", async () => {
    renderFlow(false);
    await toFiles();
    expect(screen.queryByTestId("kb-matrix-newflow-visibility")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Create (1 files)" }));
    await waitFor(() => expect(mocks.createMatrix).toHaveBeenCalledWith("ws_a", m.blank_matrix_name, ["kbf_space"], [], undefined));
    expect(mocks.fetchKbFiles).not.toHaveBeenCalledWith(expect.objectContaining({ scope: "personal" }));
  });

  it("defaults to Space members, and only me adds the viewer's personal files", async () => {
    renderFlow(true);
    await toFiles();
    const space = screen.getByRole("radio", { name: new RegExp(m.newflow_visibility_space) });
    expect(space).toBeChecked();
    expect(screen.queryByText("mine.md")).toBeNull();

    fireEvent.click(screen.getByRole("radio", { name: new RegExp(m.newflow_visibility_private) }));
    expect(await screen.findByText("mine.md")).toBeInTheDocument();
    expect(screen.getByText(m.add_files_group_personal)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /space\.md/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /mine\.md/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create (2 files)" }));
    await waitFor(() =>
      expect(mocks.createMatrix).toHaveBeenCalledWith("ws_a", m.blank_matrix_name, ["kbf_space", "kbf_mine"], [], "private"),
    );
  });

  it("unchecks personal files when switching back to Space members", async () => {
    renderFlow(true);
    await toFiles();
    fireEvent.click(screen.getByRole("radio", { name: new RegExp(m.newflow_visibility_private) }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /mine\.md/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /space\.md/ }));
    fireEvent.click(screen.getByRole("radio", { name: new RegExp(m.newflow_visibility_space) }));
    expect(screen.queryByText("mine.md")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Create (1 files)" }));
    await waitFor(() => expect(mocks.createMatrix).toHaveBeenCalledWith("ws_a", m.blank_matrix_name, ["kbf_space"], [], "space"));
  });
});
