/**
 * KbFilesManager knowledge base mode (FEATURE_KNOWLEDGE_BASES, PRD §4.2 Files tab, mockups ② and ⑩b).
 *
 * Contract: with `knowledgeBase` the table reads that knowledge base only (`GET /kb/knowledge-bases/{id}/files`, no
 * scope, no scope counts or remembered scope), deep search names the knowledge base, there is no scope filter, an
 * "Added by" column is added, "Move to another knowledge base…" replaces "Change visibility" (row, bulk bar), the
 * view state stays on `/knowledge/base/?id=…`, and the empty state depends on whether you can add files. Without
 * `knowledgeBase` the manager behaves as before.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { KbFileDTO } from "@/lib/kb-api";
import type { KnowledgeBaseDTO } from "@/lib/knowledge-bases-api";
import messagesZh from "@/messages/zh-CN.json";

const zh = messagesZh.knowledge_base;
const zhFiles = messagesZh.knowledge_bases.files;

const mocks = vi.hoisted(() => ({
  fetchFiles: vi.fn(),
  listKbFiles: vi.fn(),
  notifyChanged: vi.fn(),
  searchKb: vi.fn(),
  getKbFile: vi.fn(),
  fetchMe: vi.fn(),
  listDocTypes: vi.fn(),
  loadPref: vi.fn(),
  savePref: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: mocks.push, replace: mocks.replace, back: vi.fn() }),
}));

vi.mock("@/lib/use-space", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-space")>();
  return {
    ...actual,
    useSpaceSelection: () => ({
      items: [{ id: "ws_main", name: "商超零售场景", desc: "" }],
      activeId: "ws_main",
      loading: false,
      error: null,
      setActiveId: vi.fn(),
      refresh: vi.fn(),
    }),
  };
});

vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return {
    ...actual,
    fetchKbFiles: (...args: unknown[]) => mocks.fetchFiles(...args),
    searchKb: (...args: unknown[]) => mocks.searchKb(...args),
    getKbFile: (...args: unknown[]) => mocks.getKbFile(...args),
    loadKbFileScopePreference: (...args: unknown[]) => mocks.loadPref(...args),
    saveKbFileScopePreference: (...args: unknown[]) => mocks.savePref(...args),
  };
});

vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-bases-api")>();
  return {
    ...actual,
    listKnowledgeBaseFiles: (...args: unknown[]) => mocks.listKbFiles(...args),
    notifyKnowledgeBasesChanged: () => mocks.notifyChanged(),
  };
});

vi.mock("./kb-me", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./kb-me")>();
  return { ...actual, fetchKbMe: (...args: unknown[]) => mocks.fetchMe(...args) };
});

vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>();
  return { ...actual, listKnowledgeTables: (...args: unknown[]) => mocks.listDocTypes(...args) };
});

// Other workers' dialogs: only their contract props matter here.
vi.mock("./kb-upload-modal", () => ({
  KbUploadModal: (props: { defaultScope: string; defaultKnowledgeBaseId?: string | null }) => (
    <div data-testid="upload-modal" data-scope={props.defaultScope} data-kb={props.defaultKnowledgeBaseId ?? ""} />
  ),
}));
vi.mock("./bases/kb-move-dialog", () => ({
  KbMoveDialog: (props: {
    files: Array<{ file_id: string; filename: string }>;
    currentKnowledgeBaseId?: string | null;
    onMoved: (result: { failedIds: string[]; targetId: string }) => void;
  }) => (
    <div data-testid="move-dialog" data-files={JSON.stringify(props.files)} data-current={props.currentKnowledgeBaseId ?? ""}>
      <button type="button" onClick={() => props.onMoved({ failedIds: [], targetId: "kbase_target" })}>moved</button>
    </div>
  ),
}));

import { KbFilesManager } from "./kb-files-manager";

function file(overrides: Partial<KbFileDTO> = {}): KbFileDTO {
  return {
    file_id: "kbf_1",
    artifact_id: "art_1",
    filename: "开店检查清单 2026.pdf",
    mime_type: "application/pdf",
    size_bytes: 2_100_000,
    scope: "space",
    scope_id: "ws_main",
    workspace_id: "ws_main",
    team_id: "team_main",
    uploaded_by: "u_chen",
    uploaded_by_name: "陈思远",
    uploaded_at: "2026-11-24T05:10:00Z",
    parse_status: "done",
    parse_progress_pct: null,
    parse_layer: null,
    parse_error: null,
    parse_duration_ms: null,
    chunks_count: 3,
    cite_count: 0,
    last_cited_at: null,
    use_llm_marker: false,
    doc_type_id: null,
    summary_json: null,
    version: 1,
    soft_deleted_at: null,
    can_delete: true,
    can_change_scope: true,
    knowledge_base_id: "kbase_ops",
    ...overrides,
  } as KbFileDTO;
}

function knowledgeBase(overrides: Partial<KnowledgeBaseDTO> = {}): KnowledgeBaseDTO {
  return {
    id: "kbase_ops",
    is_default: false,
    home: "space",
    home_id: "ws_main",
    name: "门店运营手册",
    description: "开店、巡检、陈列标准。",
    audience: "space",
    audience_level: "use",
    level: "manage",
    owner: { user_id: "u_owner", name: "王蕾" },
    can: { add_files: true, manage: true, transfer: true, delete: true },
    file_count: 2,
    failed_count: 0,
    updated_at: "2026-11-24T05:10:00Z",
    revision: 3,
    ...overrides,
  };
}

function renderManager(kb?: KnowledgeBaseDTO) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      {kb ? <KbFilesManager knowledgeBase={kb} viewQuery={`id=${kb.id}`} uploadRequest={0} /> : <KbFilesManager />}
    </NextIntlClientProvider>,
  );
}

function reasonOf(item: HTMLElement): string | null | undefined {
  const id = item.getAttribute("aria-describedby");
  return id ? document.getElementById(id)?.textContent : null;
}

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/knowledge/base/?id=kbase_ops");
  mocks.fetchMe.mockResolvedValue({ user_id: "u_me", team_id: "team_main", is_team_admin: false });
  mocks.listDocTypes.mockResolvedValue([]);
  mocks.loadPref.mockResolvedValue("space");
  mocks.savePref.mockResolvedValue(undefined);
  mocks.fetchFiles.mockResolvedValue({ files: [file()], total: 1, has_more: false, next_cursor: null });
  mocks.listKbFiles.mockResolvedValue({
    files: [file(), file({ file_id: "kbf_2", filename: "生鲜陈列标准.docx", uploaded_by_name: null })],
    total: 2,
    has_more: false,
  });
  mocks.searchKb.mockResolvedValue({ chunks: [], total_candidates: 0, rerank_used: true, latency_ms: 1 });
  mocks.getKbFile.mockResolvedValue(file());
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
});

describe("KbFilesManager knowledge base mode", () => {
  it("lists the knowledge base's own files without a scope, scope counts or a remembered scope", async () => {
    renderManager(knowledgeBase());
    expect(await screen.findAllByTestId("kb-files-row")).toHaveLength(2);
    expect(mocks.listKbFiles).toHaveBeenCalled();
    const [id, params] = mocks.listKbFiles.mock.calls[0] as [string, Record<string, unknown>];
    expect(id).toBe("kbase_ops");
    expect(params).not.toHaveProperty("scope");
    expect(params).toMatchObject({ offset: 0, limit: 20, order_by: "uploaded_at", order_dir: "desc" });
    expect(mocks.fetchFiles).not.toHaveBeenCalled();
    expect(mocks.loadPref).not.toHaveBeenCalled();
    expect(screen.queryByTestId("kb-scope-filter")).not.toBeInTheDocument();
  });

  it("adds an Added by column with a neutral fallback for unknown names", async () => {
    renderManager(knowledgeBase());
    const rows = await screen.findAllByTestId("kb-files-row");
    expect(screen.getByTestId("kb-col-added-by")).toHaveTextContent(zhFiles.col_added_by);
    expect(within(rows[0]).getByTestId("kb-added-by")).toHaveTextContent("陈思远");
    expect(within(rows[1]).getByTestId("kb-added-by")).toHaveTextContent(zhFiles.added_by_unknown);
    expect(screen.queryByTestId("kb-col-scope")).not.toBeInTheDocument();
  });

  it("deep search names the knowledge base instead of scopes", async () => {
    renderManager(knowledgeBase());
    await screen.findAllByTestId("kb-files-row");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "陈列" } });
    fireEvent.click(screen.getByRole("button", { name: new RegExp(zh.filter.deep_search) }));
    await waitFor(() => expect(mocks.searchKb).toHaveBeenCalled());
    expect(mocks.searchKb.mock.calls.at(-1)).toEqual(["陈列", { knowledge_base_id: "kbase_ops" }]);
  });

  it("offers Move to another knowledge base in the row menu, disabled with the reason when the file may not move", async () => {
    mocks.listKbFiles.mockResolvedValue({ files: [file({ can_change_scope: false, can_delete: false })], total: 1, has_more: false });
    renderManager(knowledgeBase({ level: "edit", can: { add_files: true, manage: false, transfer: false, delete: false } }));
    const row = await screen.findByTestId("kb-files-row");
    fireEvent.click(within(row).getByRole("button", { name: zh.manager.row_menu.more }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).queryByRole("menuitem", { name: zh.manager.row_menu.change_scope })).not.toBeInTheDocument();
    const move = within(menu).getByRole("menuitem", { name: zhFiles.move });
    expect(reasonOf(move)).toBe(zhFiles.move_reason);
    expect(reasonOf(within(menu).getByRole("menuitem", { name: zh.manager.row_menu.delete }))).toBe(zhFiles.delete_reason);
    fireEvent.click(move);
    expect(screen.queryByTestId("move-dialog")).not.toBeInTheDocument();
  });

  it("opens the move dialog from the row menu with the current knowledge base", async () => {
    renderManager(knowledgeBase());
    const [row] = await screen.findAllByTestId("kb-files-row");
    fireEvent.click(within(row).getByRole("button", { name: zh.manager.row_menu.more }));
    fireEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: zhFiles.move }));
    const dialog = await screen.findByTestId("move-dialog");
    expect(dialog).toHaveAttribute("data-current", "kbase_ops");
    expect(JSON.parse(dialog.getAttribute("data-files") ?? "[]")).toEqual([{ file_id: "kbf_1", filename: "开店检查清单 2026.pdf" }]);
  });

  it("moves the selected files from the bulk bar, then refreshes the list and the knowledge base", async () => {
    renderManager(knowledgeBase());
    const rows = await screen.findAllByTestId("kb-files-row");
    fireEvent.click(within(rows[0]).getByRole("checkbox"));
    fireEvent.click(within(rows[1]).getByRole("checkbox"));
    const bar = await screen.findByTestId("kb-bulk-bar");
    expect(within(bar).queryByText(zh.manager.bulk.change_scope)).not.toBeInTheDocument();
    expect(within(bar).getByText(zh.manager.bulk.bind_doctype)).toBeInTheDocument();
    expect(within(bar).getByTestId("kb-open-as-matrix")).toBeInTheDocument();
    fireEvent.click(within(bar).getByTestId("kb-bulk-move"));
    const dialog = await screen.findByTestId("move-dialog");
    expect(JSON.parse(dialog.getAttribute("data-files") ?? "[]").map((f: { file_id: string }) => f.file_id)).toEqual(["kbf_1", "kbf_2"]);
    const listCalls = mocks.listKbFiles.mock.calls.length;
    fireEvent.click(within(dialog).getByRole("button", { name: "moved" }));
    await waitFor(() => expect(mocks.listKbFiles.mock.calls.length).toBeGreaterThan(listCalls));
    expect(mocks.notifyChanged).toHaveBeenCalled();
    expect(screen.queryByTestId("move-dialog")).not.toBeInTheDocument();
  });

  it("keeps only Open as matrix in the bulk bar for people who can only use the knowledge base", async () => {
    renderManager(knowledgeBase({ level: "use", can: { add_files: false, manage: false, transfer: false, delete: false } }));
    const [row] = await screen.findAllByTestId("kb-files-row");
    fireEvent.click(within(row).getByRole("checkbox"));
    const bar = await screen.findByTestId("kb-bulk-bar");
    expect(within(bar).getByTestId("kb-open-as-matrix")).toBeInTheDocument();
    expect(within(bar).queryByTestId("kb-bulk-move")).not.toBeInTheDocument();
    expect(within(bar).queryByText(zh.manager.bulk.delete)).not.toBeInTheDocument();
    expect(within(bar).queryByText(zh.manager.bulk.bind_doctype)).not.toBeInTheDocument();
  });

  it("writes the opened file to the knowledge base page address, without a scope or Space", async () => {
    renderManager(knowledgeBase());
    const [row] = await screen.findAllByTestId("kb-files-row");
    fireEvent.click(within(row).getByRole("button", { name: "开店检查清单 2026.pdf" }));
    await waitFor(() => expect(mocks.push).toHaveBeenCalled());
    const url = String(mocks.push.mock.calls.at(-1)?.[0]);
    expect(url.startsWith("/knowledge/base/?")).toBe(true);
    const params = new URLSearchParams(url.split("?")[1]);
    expect(params.get("id")).toBe("kbase_ops");
    expect(params.get("file_id")).toBe("kbf_1");
    expect(params.has("scope")).toBe(false);
    expect(params.has("space")).toBe(false);
  });

  it("gives editors of an empty knowledge base Add files and Choose from Files", async () => {
    mocks.listKbFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    renderManager(knowledgeBase({ file_count: 0 }));
    const empty = await screen.findByTestId("kb-empty-knowledge-base");
    expect(empty).toHaveTextContent(zhFiles.empty_title);
    expect(empty).toHaveTextContent(zhFiles.empty_editor_body);
    expect(within(empty).getByTestId("kb-empty-from-files")).toHaveAttribute("href", expect.stringContaining("/files/?add_to_kb=kbase_ops"));
    fireEvent.click(within(empty).getByTestId("kb-empty-add-files"));
    const upload = await screen.findByTestId("upload-modal");
    expect(upload).toHaveAttribute("data-kb", "kbase_ops");
    expect(upload).toHaveAttribute("data-scope", "space");
    expect(mocks.savePref).not.toHaveBeenCalled();
  });

  it("tells people who cannot add files whom to ask, without add entry points", async () => {
    mocks.listKbFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    renderManager(knowledgeBase({ level: "use", file_count: 0, can: { add_files: false, manage: false, transfer: false, delete: false } }));
    const empty = await screen.findByTestId("kb-empty-knowledge-base");
    expect(empty).toHaveTextContent("请联系所有者王蕾");
    expect(within(empty).queryByTestId("kb-empty-add-files")).not.toBeInTheDocument();
    expect(within(empty).queryByTestId("kb-empty-from-files")).not.toBeInTheDocument();
  });

  it("names the Team admins for an empty Team default knowledge base", async () => {
    mocks.listKbFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    renderManager(knowledgeBase({
      is_default: true,
      home: "team",
      home_id: "team_main",
      name: null,
      owner: null,
      audience: "team",
      level: "use",
      file_count: 0,
      can: { add_files: false, manage: false, transfer: false, delete: false },
    }));
    expect(await screen.findByTestId("kb-empty-knowledge-base")).toHaveTextContent("请联系团队管理员");
  });

  it("keeps the scope list, scope filter and Change visibility without a knowledge base", async () => {
    renderManager();
    const row = await screen.findByTestId("kb-files-row");
    await waitFor(() => expect(mocks.fetchFiles.mock.calls.some((c) => (c[0] as { limit?: number }).limit === 20)).toBe(true));
    expect(mocks.listKbFiles).not.toHaveBeenCalled();
    expect(screen.getByTestId("kb-scope-filter")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-col-added-by")).not.toBeInTheDocument();
    fireEvent.click(within(row).getByRole("button", { name: zh.manager.row_menu.more }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: zh.manager.row_menu.change_scope })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: zhFiles.move })).not.toBeInTheDocument();
  });
});
