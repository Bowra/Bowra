// Knowledge bases (FEATURE_KNOWLEDGE_BASES): the upload dialog's "Add to" target (PRD §4.7, mockup ⑧).
// Switch on: the target picker replaces the scope cards, its home sets scope/scope_id and `knowledge_base_id` is sent on
// single files, ZIPs and Google Drive imports. Switch off: today's scope cards and requests, unchanged.
import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import zhMessages from "@/messages/zh-CN.json";

import { SPACES_FIXTURE, directoryFixture } from "./bases/kb-target-picker.fixtures";

const mocks = vi.hoisted(() => ({
  knowledgeBases: true,
  googleWorkspace: false,
  directory: null as ReturnType<typeof directoryFixture> | null,
  directoryError: null as Error | null,
  refresh: vi.fn(),
  notify: vi.fn(),
  pickDriveFiles: vi.fn(),
  importKbFromGoogleDrive: vi.fn(),
  docTypes: [] as Array<{ doc_type_id: string; name: string; fields: unknown[] }>,
}));

vi.mock("./use-kb-me", () => ({
  useKbMeState: () => ({ me: { user_id: "u_1", team_id: "team_1", is_team_admin: false, workspace_id: "ws_1" }, status: "ready", retry: vi.fn() }),
}));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: SPACES_FIXTURE, activeId: "ws_1", loading: false, error: null, setActiveId: () => {}, refresh: () => {} }),
}));
vi.mock("@/lib/kb-capabilities-api", () => ({ fetchKbCapabilities: () => Promise.resolve(null) }));
vi.mock("@/lib/kb-ingestion-readiness", () => ({ useKbIngestionReadiness: () => ({ state: "ready", retry: vi.fn() }) }));
vi.mock("@/lib/kb-knowledge-tables-api", () => ({ listKnowledgeTables: () => Promise.resolve(mocks.docTypes) }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { capabilities: { knowledge_bases: mocks.knowledgeBases, google_workspace: mocks.googleWorkspace } } }),
}));
vi.mock("./bases/use-knowledge-bases", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./bases/use-knowledge-bases")>()),
  useKnowledgeBaseDirectory: ({ enabled = true }: { enabled?: boolean } = {}) => ({
    directory: enabled ? mocks.directory : null,
    loading: false,
    error: enabled ? mocks.directoryError : null,
    refresh: mocks.refresh,
  }),
}));
vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/knowledge-bases-api")>()),
  notifyKnowledgeBasesChanged: mocks.notify,
}));
vi.mock("@/lib/google-picker", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/google-picker")>()),
  prepareGooglePicker: () => Promise.resolve(),
  pickDriveFiles: mocks.pickDriveFiles,
}));
vi.mock("@/lib/kb-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/kb-api")>()),
  importKbFromGoogleDrive: mocks.importKbFromGoogleDrive,
}));

import { KbUploadModal } from "./kb-upload-modal";

class MockXhr {
  static instances: MockXhr[] = [];
  upload: { onprogress?: (e: ProgressEvent) => void; onload?: () => void } = {};
  onload?: () => void;
  onerror?: () => void;
  onabort?: () => void;
  ontimeout?: () => void;
  status = 0;
  responseText = "";
  body: FormData | null = null;
  constructor() { MockXhr.instances.push(this); }
  open() {}
  setRequestHeader() {}
  send(body: FormData) { this.body = body; }
  abort() { this.onabort?.(); }
}

function renderModal(props: { defaultKnowledgeBaseId?: string | null; defaultScope?: "personal" | "space" | "team" } = {}) {
  const onClose = vi.fn();
  const onUploaded = vi.fn();
  render(
    <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
      <KbUploadModal defaultScope={props.defaultScope ?? "personal"} defaultKnowledgeBaseId={props.defaultKnowledgeBaseId} onClose={onClose} onUploaded={onUploaded} />
    </NextIntlClientProvider>,
  );
  return { onClose, onUploaded };
}

function pick(name: string) {
  fireEvent.change(screen.getByTestId("kb-file-input"), { target: { files: [new File([new Uint8Array(100)], name)] } });
}

async function submit() {
  await act(async () => { fireEvent.click(screen.getByTestId("kb-upload-submit")); });
  return MockXhr.instances[MockXhr.instances.length - 1];
}

const checked = (id: string) => screen.getByTestId(`kb-target-${id}`).getAttribute("aria-checked") === "true";

beforeEach(() => {
  mocks.knowledgeBases = true;
  mocks.googleWorkspace = false;
  mocks.directory = directoryFixture();
  mocks.directoryError = null;
  mocks.docTypes = [];
  mocks.notify.mockReset();
  mocks.pickDriveFiles.mockReset();
  mocks.importKbFromGoogleDrive.mockReset();
  MockXhr.instances = [];
  vi.stubGlobal("XMLHttpRequest", MockXhr);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("KbUploadModal · knowledge bases on", () => {
  it("replaces the scope cards with 添加到 and the grouped target picker", () => {
    renderModal();
    expect(screen.getByRole("radiogroup", { name: "添加到" })).toBeInTheDocument();
    expect(screen.queryByTestId("kb-scope-personal")).toBeNull();
    expect(screen.queryByText(zhMessages.knowledge_base.upload_modal.scope_title)).toBeNull();
    expect(screen.getByTestId("kb-target-kb_policy")).toBeDisabled();
    expect(screen.getByTestId("kb-target-kb_policy")).toHaveTextContent("请联系所有者王蕾");
  });

  it("defaults to the knowledge base you are in when you can add to it", () => {
    renderModal({ defaultKnowledgeBaseId: "kb_ops" });
    expect(checked("kb_ops")).toBe(true);
  });

  it("falls back to the current Space's default, then My knowledge", () => {
    renderModal({ defaultKnowledgeBaseId: "kb_contracts" });
    expect(checked("kb_space")).toBe(true);
    cleanup();
    mocks.directory = directoryFixture({ spaceDefaultEditable: false });
    renderModal({ defaultScope: "space" });
    expect(checked("kb_my")).toBe(true);
  });

  it("sends the target's home and knowledge_base_id for a single file, names it in the subtitle and refreshes readers", async () => {
    const { onUploaded } = renderModal({ defaultKnowledgeBaseId: "kb_ops" });
    pick("开店检查清单.pdf");
    const xhr = await submit();
    expect(xhr.body?.get("scope")).toBe("space");
    expect(xhr.body?.get("scope_id")).toBe("ws_1");
    expect(xhr.body?.get("knowledge_base_id")).toBe("kb_ops");
    expect(screen.getByTestId("kb-upload-subtitle")).toHaveTextContent("上传到：门店运营手册");
    await act(async () => {
      xhr.status = 200;
      xhr.responseText = JSON.stringify({ file_id: "kbf_9", filename: "开店检查清单.pdf" });
      xhr.onload?.();
    });
    expect(onUploaded).toHaveBeenCalledWith(expect.objectContaining({ file_id: "kbf_9" }));
    expect(mocks.notify).toHaveBeenCalledOnce();
  });

  it("follows the chosen target's home: My knowledge uploads as personal", async () => {
    renderModal();
    fireEvent.click(screen.getByTestId("kb-target-kb_comp"));
    pick("竞品.pdf");
    const xhr = await submit();
    expect(xhr.body?.get("scope")).toBe("personal");
    expect(xhr.body?.get("scope_id")).toBe("u_1");
    expect(xhr.body?.get("knowledge_base_id")).toBe("kb_comp");
  });

  it("sends knowledge_base_id with a ZIP", async () => {
    renderModal({ defaultKnowledgeBaseId: "kb_ops" });
    pick("batch.zip");
    const xhr = await submit();
    expect(xhr.body?.get("knowledge_base_id")).toBe("kb_ops");
    await act(async () => {
      xhr.status = 200;
      xhr.responseText = JSON.stringify({
        uploaded: [{ file_id: "kb_1", filename: "a.txt", parse_status: "done", scope: "space" }],
        skipped: [], total_entries: 1, bundle_filename: "batch.zip", bundle_artifact_id: "art_1",
      });
      xhr.onload?.();
    });
    expect(screen.getByTestId("kb-bundle-summary")).toBeInTheDocument();
    expect(mocks.notify).toHaveBeenCalledOnce();
  });

  it("imports from Google Drive into the target knowledge base", async () => {
    mocks.googleWorkspace = true;
    mocks.pickDriveFiles.mockResolvedValue([{ id: "doc1", name: "季度报告", mimeType: "application/vnd.google-apps.document", url: "u1" }]);
    mocks.importKbFromGoogleDrive.mockResolvedValue([{ file_id: "doc1", name: "季度报告.docx", ok: true, kb_file_id: "kbf_1" }]);
    renderModal({ defaultKnowledgeBaseId: "kb_ops" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: zhMessages.google_drive.kb_import_entry })); });
    await screen.findByTestId("kb-google-drive-results");
    expect(mocks.importKbFromGoogleDrive).toHaveBeenCalledWith(["doc1"], { scope: "space", scope_id: "ws_1", knowledge_base_id: "kb_ops" });
    expect(mocks.notify).toHaveBeenCalledOnce();
  });

  it("links to Add from Files with the target preselected", () => {
    renderModal({ defaultKnowledgeBaseId: "kb_ops" });
    expect(screen.getByRole("link", { name: "「文件」" })).toHaveAttribute("href", "/files/?add_to_kb=kb_ops");
  });

  it("explains a directory that did not load and blocks the upload until it does", () => {
    mocks.directory = null;
    mocks.directoryError = new Error("boom");
    renderModal();
    expect(screen.getByTestId("kb-upload-target-error")).toHaveTextContent(zhMessages.knowledge_bases.upload.directory_failed);
    fireEvent.click(screen.getByRole("button", { name: zhMessages.knowledge_bases.common.retry }));
    expect(mocks.refresh).toHaveBeenCalled();
    pick("a.pdf");
    expect(screen.getByTestId("kb-upload-submit")).toBeDisabled();
  });
});

describe("KbUploadModal · extraction table link", () => {
  const spreadsheetLink = async () => {
    mocks.docTypes = [{ doc_type_id: "dt_1", name: "合同", fields: [] }];
    renderModal();
    await act(async () => { await Promise.resolve(); });
    pick("台账.xlsx");
    return (await screen.findByText(zhMessages.knowledge_base.upload_modal.manage_link)).getAttribute("href");
  };

  it("points to the Extraction tables tab while knowledge bases are on (Q1)", async () => {
    expect(await spreadsheetLink()).toBe("/knowledge/?tab=records");
  });

  it("keeps today's Templates link with the switch off", async () => {
    mocks.knowledgeBases = false;
    expect(await spreadsheetLink()).toBe("/knowledge/?tab=templates");
  });
});

describe("KbUploadModal · knowledge bases off", () => {
  it("keeps today's scope cards and requests, ignoring defaultKnowledgeBaseId", async () => {
    mocks.knowledgeBases = false;
    renderModal({ defaultKnowledgeBaseId: "kb_ops", defaultScope: "space" });
    expect(screen.getByText(zhMessages.knowledge_base.upload_modal.scope_title)).toBeInTheDocument();
    expect(screen.getByTestId("kb-scope-space")).toBeChecked();
    expect(screen.queryByTestId("kb-target-picker")).toBeNull();
    expect(screen.queryByTestId("kb-upload-from-files")).toBeNull();
    pick("a.pdf");
    const xhr = await submit();
    expect(xhr.body?.get("scope")).toBe("space");
    expect(xhr.body?.get("scope_id")).toBe("ws_1");
    expect(xhr.body?.has("knowledge_base_id")).toBe(false);
    expect(screen.getByTestId("kb-upload-subtitle")).toHaveTextContent("上传到：空间 · 商超零售场景");
    await act(async () => {
      xhr.status = 200;
      xhr.responseText = JSON.stringify({ file_id: "kbf_9", filename: "a.pdf" });
      xhr.onload?.();
    });
    expect(mocks.notify).not.toHaveBeenCalled();
  });
});
