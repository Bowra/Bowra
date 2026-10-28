// Google Drive import from the knowledge-base upload modal: entry gated by the Google capability,
// import uses the dialog's scope, per-file results (no re-import), imported files reach the list.
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import zhMessages from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  googleWorkspace: true,
  pickDriveFiles: vi.fn(),
  importKbFromGoogleDrive: vi.fn(),
  getKbFile: vi.fn(),
}));

vi.mock("./use-kb-me", () => ({
  useKbMeState: () => ({ me: { user_id: "u_1", team_id: "team_1", is_team_admin: false, workspace_id: "ws_1" }, status: "ready", retry: vi.fn() }),
}));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: [{ id: "ws_1", name: "销售", desc: "" }], activeId: "ws_1", loading: false, error: null, setActiveId: () => {}, refresh: () => {} }),
}));
vi.mock("@/lib/kb-capabilities-api", () => ({ fetchKbCapabilities: () => Promise.resolve(null) }));
vi.mock("@/lib/kb-ingestion-readiness", () => ({
  useKbIngestionReadiness: () => ({ state: "ready", retry: vi.fn() }),
}));
vi.mock("@/lib/kb-knowledge-tables-api", () => ({ listKnowledgeTables: () => Promise.resolve([]) }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { capabilities: { google_workspace: mocks.googleWorkspace } } }),
}));
vi.mock("@/lib/google-picker", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/google-picker")>()),
  pickDriveFiles: mocks.pickDriveFiles,
}));
vi.mock("@/lib/kb-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/kb-api")>()),
  importKbFromGoogleDrive: mocks.importKbFromGoogleDrive,
  getKbFile: mocks.getKbFile,
}));

import { KbUploadModal } from "./kb-upload-modal";

function renderModal() {
  const onClose = vi.fn();
  const onUploaded = vi.fn();
  render(
    <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
      <KbUploadModal defaultScope="personal" onClose={onClose} onUploaded={onUploaded} />
    </NextIntlClientProvider>,
  );
  return { onClose, onUploaded };
}

describe("KbUploadModal Google Drive import", () => {
  beforeEach(() => {
    mocks.googleWorkspace = true;
    mocks.pickDriveFiles.mockReset();
    mocks.importKbFromGoogleDrive.mockReset();
    mocks.getKbFile.mockReset();
  });
  afterEach(cleanup);

  it("hides the entry without the Google capability", () => {
    mocks.googleWorkspace = false;
    renderModal();
    expect(screen.queryByTestId("kb-google-drive-import")).not.toBeInTheDocument();
  });

  it("imports picked files into the selected scope and lists per-file results", async () => {
    mocks.pickDriveFiles.mockResolvedValue([
      { id: "doc1", name: "季度报告", mimeType: "application/vnd.google-apps.document", url: "u1" },
      { id: "big1", name: "大表", mimeType: "application/vnd.google-apps.spreadsheet", url: "u2" },
    ]);
    mocks.importKbFromGoogleDrive.mockResolvedValue([
      { file_id: "doc1", name: "季度报告.docx", ok: true, kb_file_id: "kbf_1" },
      { file_id: "big1", name: null, ok: false, error: "google_export_too_large" },
    ]);
    mocks.getKbFile.mockResolvedValue({ file_id: "kbf_1", filename: "季度报告.docx" });
    const { onUploaded, onClose } = renderModal();

    fireEvent.click(screen.getByRole("button", { name: zhMessages.google_drive.kb_import_entry }));

    await screen.findByTestId("kb-google-drive-results");
    expect(mocks.importKbFromGoogleDrive).toHaveBeenCalledWith(["doc1", "big1"], { scope: "personal", scope_id: "u_1" });
    expect(screen.getByText("季度报告.docx")).toBeInTheDocument();
    expect(screen.getByText(zhMessages.google_drive.error.google_export_too_large)).toBeInTheDocument();
    expect(screen.queryByText(/重新导入/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("kb-google-drive-done"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onUploaded).toHaveBeenCalledWith({ file_id: "kbf_1", filename: "季度报告.docx" });
  });

  it("imports at most 20 picked files and says how many were left out", async () => {
    const picked = Array.from({ length: 23 }, (_, index) => ({ id: `f${index}`, name: `f${index}`, mimeType: "text/plain", url: `u${index}` }));
    mocks.pickDriveFiles.mockResolvedValue(picked);
    mocks.importKbFromGoogleDrive.mockResolvedValue([{ file_id: "f0", name: "f0", ok: true, kb_file_id: "kbf_0" }]);
    renderModal();

    fireEvent.click(screen.getByRole("button", { name: zhMessages.google_drive.kb_import_entry }));

    await screen.findByTestId("kb-google-drive-results");
    expect(mocks.importKbFromGoogleDrive.mock.calls[0][0]).toHaveLength(20);
    expect(screen.getByText(/其余 3 个/)).toBeInTheDocument();
  });

  it("explains picker failures without importing", async () => {
    const { GooglePickerError } = await import("@/lib/google-picker");
    mocks.pickDriveFiles.mockRejectedValue(new GooglePickerError("google_origin_not_registered"));
    renderModal();

    fireEvent.click(screen.getByRole("button", { name: zhMessages.google_drive.kb_import_entry }));

    expect(await screen.findByText(zhMessages.google_drive.origin)).toBeInTheDocument();
    expect(mocks.importKbFromGoogleDrive).not.toHaveBeenCalled();
  });
});
