/**
 * KB file view P1 — KB UX cleanup interaction design §4 / designs 02, 03, 06, 01b·2c.
 *
 * Scope filter names space; deep search shows count within scope and hit location; no hits offers scope change / filename search
 * as two exits; opening hit row drawer puts hit fragment first; Use in chat shares library-file handoff;
 * ≤ 1280 hides type column; ≤ 640 uses two-line list retaining status.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installFakeIntersectionObserver } from "@/__tests__/fake-intersection-observer";
import { ToastContainer } from "@/components/chat/toast-container";
import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import type { KbFileDTO, KbSearchResultChunk } from "@/lib/kb-api";
import { _clearAllToasts } from "@/lib/use-toast";
import messagesZh from "@/messages/zh-CN.json";

const zh = messagesZh.knowledge_base;

const mocks = vi.hoisted(() => ({
  fetchFiles: vi.fn(),
  uploadFile: vi.fn(),
  push: vi.fn(),
  searchKb: vi.fn(),
  getKbFile: vi.fn(),
  fetchChunks: vi.fn(),
  fetchMe: vi.fn(),
  listDocTypes: vi.fn(),
  handoff: vi.fn(),
  reparse: vi.fn(),
  deleteFile: vi.fn(),
  bulkDelete: vi.fn(),
  evictChunks: vi.fn(),
  loadPref: vi.fn(),
  savePref: vi.fn(),
}));

vi.mock("@/lib/canvas-pm-adapter/kb-chunk-cache", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/canvas-pm-adapter/kb-chunk-cache")>();
  return { ...actual, evictKbChunks: (...args: unknown[]) => mocks.evictChunks(...args) };
});


vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: mocks.push, replace: vi.fn() }),
}));
vi.mock("@/lib/kb-ingestion-readiness", () => ({
  useKbIngestionReadiness: () => ({ state: "ready", retry: vi.fn() }),
}));


vi.mock("@/lib/composer-handoff", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/composer-handoff")>();
  return { ...actual, handoffAttachmentsToNewChat: (...args: unknown[]) => mocks.handoff(...args) };
});

vi.mock("@/lib/use-space", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-space")>();
  return {
    ...actual,
    useSpaceSelection: () => ({
      items: [{ id: "ws_main", name: "商超零售场景" }],
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
    uploadKbFile: (...args: unknown[]) => mocks.uploadFile(...args),
    searchKb: (...args: unknown[]) => mocks.searchKb(...args),
    getKbFile: (...args: unknown[]) => mocks.getKbFile(...args),
    fetchKbFileChunks: (...args: unknown[]) => mocks.fetchChunks(...args),
    reparseKbFile: (...args: unknown[]) => mocks.reparse(...args),
    deleteKbFile: (...args: unknown[]) => mocks.deleteFile(...args),
    bulkDeleteKbFiles: (...args: unknown[]) => mocks.bulkDelete(...args),
    loadKbFileScopePreference: (...args: unknown[]) => mocks.loadPref(...args),
    saveKbFileScopePreference: (...args: unknown[]) => mocks.savePref(...args),
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

import { KbFilesManager } from "./kb-files-manager";

function file(overrides: Partial<KbFileDTO> = {}): KbFileDTO {
  return {
    file_id: "kbf_1",
    artifact_id: "art_1",
    filename: "供应商合同-华东区.pdf",
    mime_type: "application/pdf",
    size_bytes: 3_200_000,
    scope: "personal",
    scope_id: "u_me",
    workspace_id: "ws_main",
    team_id: "team_main",
    uploaded_by: "u_me",
    uploaded_at: "2026-09-24T05:10:00Z",
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
    ...overrides,
  } as KbFileDTO;
}

function hit(overrides: Partial<KbSearchResultChunk> = {}): KbSearchResultChunk {
  return {
    chunk_id: "c_hit",
    content: "华东区门店的付款方式为月结",
    file_id: "kbf_1",
    file_name: "供应商合同-华东区.pdf",
    page: 3,
    section_title: null,
    role: "intro",
    score: 0.8,
    scope: "personal",
    chunk_index: 5,
    ...overrides,
  };
}

let mediaMatches: Record<string, boolean> = {};
function installMatchMedia() {
  window.matchMedia = ((query: string) => ({
    matches: mediaMatches[query] ?? false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

beforeEach(() => {
  vi.clearAllMocks();
  mediaMatches = {};
  installMatchMedia();
  mocks.fetchMe.mockResolvedValue({ user_id: "u_me", team_id: "team_main", is_team_admin: false });
  mocks.listDocTypes.mockResolvedValue([]);
  // The remembered scope is personal, so the list starts there.
  mocks.loadPref.mockResolvedValue("personal");
  mocks.savePref.mockResolvedValue(undefined);
  mocks.fetchFiles.mockResolvedValue({ files: [file()], total: 1, has_more: false });
  mocks.searchKb.mockResolvedValue({ chunks: [], total_candidates: 0, rerank_used: true, latency_ms: 1 });
  mocks.getKbFile.mockResolvedValue(file());
  mocks.fetchChunks.mockResolvedValue({
    chunks: [
      { chunk_id: "c_1", content: "第一段", page: null, section_title: null, role: "intro", chunk_index: 1 },
      { chunk_id: "c_hit", content: "华东区门店的付款方式为月结", page: 3, section_title: null, role: "intro", chunk_index: 5 },
    ],
    total: 2,
  });
});

afterEach(() => {
  cleanup();
  _clearAllToasts();
  // @ts-expect-error jsdom lacks matchMedia; restore original state.
  delete window.matchMedia;
});

function renderManager() {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <KbFilesManager />
      <ToastContainer />
    </NextIntlClientProvider>,
  );
}

/** Choose a scope through the toolbar scope filter. */
async function selectScope(id: "personal" | "space" | "team") {
  fireEvent.click(await screen.findByTestId("kb-scope-filter"), { detail: 0 });
  fireEvent.click(await screen.findByTestId(`kb-scope-filter-${id}`));
}

async function deepSearch(q: string) {
  fireEvent.change(screen.getByPlaceholderText(zh.search_placeholder), { target: { value: q } });
  fireEvent.click(screen.getByRole("button", { name: new RegExp(zh.filter.deep_search) }));
}

describe("范围筛选", () => {
  it("names the current space in the space option and explains who can search each scope", async () => {
    renderManager();
    fireEvent.click(await screen.findByTestId("kb-scope-filter"), { detail: 0 });
    const space = await screen.findByTestId("kb-scope-filter-space");
    expect(space).toHaveTextContent("空间 · 商超零售场景");
    expect(space).toHaveAccessibleDescription(expect.stringContaining("商超零售场景的成员可检索"));
    expect(screen.getByTestId("kb-scope-filter-personal")).toHaveAccessibleDescription(zh.files_view.hint_personal);
  });
});

describe("内容深搜", () => {
  it("写出范围内找到的文件数，命中片段前标出页码", async () => {
    mocks.searchKb.mockResolvedValue({ chunks: [hit()], total_candidates: 1, rerank_used: true, latency_ms: 1 });
    renderManager();
    await deepSearch("门店");
    expect(await screen.findByTestId("kb-deep-count")).toHaveTextContent("在「个人」中找到 1 个文件");
    expect(screen.getByTestId("kb-hit-loc")).toHaveTextContent("第 3 页");
  });

  it("没有页码时用章节，再没有时用第 N 段", async () => {
    mocks.searchKb.mockResolvedValue({
      chunks: [
        hit({ page: null, section_title: "二、付款方式" }),
        hit({ chunk_id: "c2", file_id: "kbf_2", page: null, section_title: null, chunk_index: 7 }),
      ],
      total_candidates: 2,
      rerank_used: true,
      latency_ms: 1,
    });
    mocks.getKbFile.mockImplementation(async (id: string) => file({ file_id: id, filename: `${id}.pdf` }));
    renderManager();
    await deepSearch("付款");
    await waitFor(() => expect(screen.getAllByTestId("kb-hit-loc")).toHaveLength(2));
    expect(screen.getAllByTestId("kb-hit-loc").map((el) => el.textContent)).toEqual(["章节「二、付款方式」", "第 7 段"]);
  });

  it("无命中时可以换到空间里搜（保留关键词），或关闭深搜按文件名搜", async () => {
    renderManager();
    await deepSearch("量子纠缠火锅配方");
    expect(await screen.findByText("「个人」中没有找到相关内容")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-deep-count")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("kb-deep-try-scope"));
    await waitFor(() =>
      expect(mocks.searchKb).toHaveBeenLastCalledWith("量子纠缠火锅配方", expect.objectContaining({ scope_filter: ["space"] })),
    );
    expect(await screen.findByText("「商超零售场景」中没有找到相关内容")).toBeInTheDocument();

    const before = mocks.searchKb.mock.calls.length;
    fireEvent.click(screen.getByTestId("kb-deep-by-name"));
    await waitFor(() =>
      expect(mocks.fetchFiles).toHaveBeenLastCalledWith(expect.objectContaining({ q: "量子纠缠火锅配方", scope: "space" })),
    );
    expect(mocks.searchKb.mock.calls.length).toBe(before);
  });

  it("点命中行打开抽屉，命中片段置顶高亮", async () => {
    mocks.searchKb.mockResolvedValue({ chunks: [hit()], total_candidates: 1, rerank_used: true, latency_ms: 1 });
    renderManager();
    await deepSearch("门店");
    fireEvent.click(await screen.findByTestId("kb-files-row"));
    await waitFor(() => expect(screen.getAllByTestId("kb-chunk-card")).toHaveLength(2));
    const cards = screen.getAllByTestId("kb-chunk-card");
    expect(cards[0]).toHaveAttribute("data-chunk-id", "c_hit");
    expect(cards[0]).toHaveClass("hit");
    expect(cards[0]).toHaveTextContent("第 3 页 · 搜索命中");
    expect(cards[1]).not.toHaveClass("hit");
  });

  it("从普通列表打开抽屉时不置顶任何片段（对照）", async () => {
    renderManager();
    fireEvent.click(await screen.findByTestId("kb-files-row"));
    await waitFor(() => expect(screen.getAllByTestId("kb-chunk-card")).toHaveLength(2));
    const cards = screen.getAllByTestId("kb-chunk-card");
    expect(cards[0]).toHaveAttribute("data-chunk-id", "c_1");
    expect(cards.some((c) => c.classList.contains("hit"))).toBe(false);
  });
});

describe("在对话中使用", () => {
  it("本人在当前空间上传的可用文件：走资料库文件同一交接，带上 artifact", async () => {
    renderManager();
    fireEvent.click(await screen.findByTestId("kb-files-row"));
    fireEvent.click(await screen.findByTestId("kb-drawer-use-in-chat"));
    expect(mocks.handoff).toHaveBeenCalledWith([
      { fileId: "art_1", filename: "供应商合同-华东区.pdf", size: 3_200_000, mime: "application/pdf" },
    ]);
  });

  it("别人上传的文件不给这个入口（附件校验只收本人上传）", async () => {
    mocks.fetchFiles.mockResolvedValue({ files: [file({ uploaded_by: "u_other" })], total: 1, has_more: false });
    renderManager();
    fireEvent.click(await screen.findByTestId("kb-files-row"));
    await screen.findByTestId("kb-file-drawer");
    await waitFor(() => expect(mocks.fetchMe).toHaveBeenCalled());
    expect(screen.queryByTestId("kb-drawer-use-in-chat")).not.toBeInTheDocument();
  });
});

describe("Knowledge base empty states", () => {
  it("uses the Add knowledge label for the empty Space action", async () => {
    mocks.fetchFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    renderManager();
    await selectScope("space");
    const empty = await screen.findByTestId("kb-empty-space");
    const add = within(empty).getByRole("button", { name: "添加知识" });
    fireEvent.click(add);
    expect(await screen.findByTestId("kb-upload-modal")).toBeInTheDocument();
    expect(screen.getByTestId("kb-scope-space")).toBeChecked();
  });
});

describe("团队范围空状态", () => {
  it("非管理员：说明由管理员上传，并可改传到个人或空间", async () => {
    mocks.fetchFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    renderManager();
    await selectScope("team");
    const empty = await screen.findByTestId("kb-empty-team");
    await within(empty).findByText(zh.files_view.empty_team_member);
    expect(within(empty).queryByText(zh.files_view.upload_to_team)).not.toBeInTheDocument();
    fireEvent.click(within(empty).getByText(zh.files_view.upload_to_space));
    // Switches the list scope to Space and opens the upload dialog.
    await waitFor(() => expect(screen.getByTestId("kb-scope-filter")).toHaveTextContent("空间 · 商超零售场景"));
  });

  it("管理员：上传到团队，并给出三种范围说明", async () => {
    mocks.fetchMe.mockResolvedValue({ user_id: "u_me", team_id: "team_main", is_team_admin: true });
    mocks.fetchFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    renderManager();
    await selectScope("team");
    const empty = await screen.findByTestId("kb-empty-team");
    await within(empty).findByText(zh.files_view.upload_to_team);
    expect(within(empty).getByText(zh.files_view.explain_team)).toBeInTheDocument();
    expect(within(empty).queryByText(zh.files_view.upload_to_personal)).not.toBeInTheDocument();
  });
});

describe("响应式", () => {
  it("≤ 1280 去掉类型列", async () => {
    mediaMatches = { "(max-width: 1280px)": true };
    renderManager();
    await screen.findByTestId("kb-files-row");
    expect(screen.queryByTestId("kb-col-type")).not.toBeInTheDocument();
  });

  it("宽屏保留类型列（对照）", async () => {
    renderManager();
    await screen.findByTestId("kb-files-row");
    expect(screen.getByTestId("kb-col-type")).toBeInTheDocument();
  });

  it("≤ 640 改两行列表，保留状态的最短形式，不提供多选", async () => {
    mediaMatches = { "(max-width: 640px)": true, "(max-width: 1280px)": true };
    mocks.fetchFiles.mockResolvedValue({
      files: [file(), file({ file_id: "kbf_2", filename: "deck.pptx", parse_status: "failed", parse_error_code: "timeout" })],
      total: 2,
      has_more: false,
    });
    renderManager();
    const list = await screen.findByTestId("kb-files-list");
    await waitFor(() => expect(within(list).getAllByTestId("kb-files-row")).toHaveLength(2));
    expect(screen.queryByTestId("kb-files-table")).not.toBeInTheDocument();
    const rows = within(list).getAllByTestId("kb-files-row");
    expect(rows[0]).toHaveTextContent(zh.file_state.ready);
    expect(rows[1]).toHaveTextContent(zh.file_state.failed);
    expect(within(rows[1]).queryByTestId("kb-pill-retry")).not.toBeInTheDocument();
    expect(list.querySelector('[role="checkbox"]')).toBeNull();
    expect(screen.getByRole("button", { name: new RegExp(zh.files_view.filters) })).toBeInTheDocument();
  });
});

describe("页头上传按钮（左栏导航 2b）", () => {
  function managerWith(uploadRequest?: number) {
    return (
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbFilesManager uploadRequest={uploadRequest} />
      </NextIntlClientProvider>
    );
  }

  it("页头持有按钮时控制栏不画上传按钮；每次请求以当前范围打开上传弹窗", async () => {
    const { rerender } = render(managerWith(0));
    await screen.findByPlaceholderText(zh.search_placeholder);
    expect(screen.queryByRole("button", { name: new RegExp(zh.upload_button) })).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-upload-modal")).not.toBeInTheDocument();

    rerender(managerWith(1));
    expect(await screen.findByTestId("kb-upload-modal")).toBeInTheDocument();
  });

  it("未传 uploadRequest 时控制栏照常显示上传按钮（对照）", () => {
    render(managerWith(undefined));
    expect(screen.getByRole("button", { name: new RegExp(zh.upload_button) })).toBeInTheDocument();
  });
});

describe("上次处理时原文件未找到", () => {
  it("原件恢复后重试成功会移除同一文件先前的失败提示", async () => {
    const gone = file({ file_id: "kbf_gone", parse_status: "failed", parse_error_code: "source_missing" });
    mocks.fetchFiles.mockResolvedValue({ files: [gone], total: 1, has_more: false });
    mocks.reparse.mockRejectedValueOnce(new Error("source missing")).mockResolvedValueOnce(gone);
    renderManager();
    const row = await screen.findByTestId("kb-files-row");
    const retry = within(row).getByTestId("kb-pill-retry");

    fireEvent.click(retry);
    expect(await screen.findByTestId("toast-error")).toBeInTheDocument();
    fireEvent.click(retry);
    await waitFor(() => expect(screen.queryByTestId("toast-error")).not.toBeInTheDocument());
    expect(screen.getByText(zh.toast.requeued_parse.replace("{name}", gone.filename))).toBeInTheDocument();
  });

  it("恢复后可从状态、菜单或抽屉重试；其他失败照常可重试（对照）", async () => {
    const gone = file({ file_id: "kbf_gone", filename: "deck.pptx", parse_status: "failed", parse_error_code: "source_missing" });
    const timedOut = file({ file_id: "kbf_slow", filename: "big.pdf", parse_status: "failed", parse_error_code: "timeout" });
    mocks.fetchFiles.mockResolvedValue({ files: [gone, timedOut], total: 2, has_more: false });
    mocks.getKbFile.mockImplementation(async (id: string) => (id === gone.file_id ? gone : timedOut));
    renderManager();
    await waitFor(() => expect(screen.getAllByTestId("kb-files-row")).toHaveLength(2));
    const [goneRow, slowRow] = screen.getAllByTestId("kb-files-row");

    // The pill names only the state; retry is a separate button whose accessible name carries the file name.
    expect(within(goneRow).getByRole("button", { name: zh.pill.retry_file.replace("{name}", "deck.pptx") })).toHaveAttribute("data-testid", "kb-pill-retry");
    expect(within(goneRow).getByTitle(zh.fail_reason.source_missing)).toHaveTextContent(new RegExp(`^${zh.pill.parse_failed}$`));
    expect(within(slowRow).getByTestId("kb-pill-retry")).toBeInTheDocument();

    fireEvent.click(within(goneRow).getByRole("button", { name: zh.manager.row_menu.more }));
    expect(screen.getByText(zh.manager.row_menu.reparse)).toBeInTheDocument();
    fireEvent.click(within(slowRow).getByRole("button", { name: zh.manager.row_menu.more }));
    expect(screen.getByText(zh.manager.row_menu.reparse)).toBeInTheDocument();

    fireEvent.click(within(goneRow).getByText("deck.pptx"));
    await screen.findByTestId("kb-file-drawer");
    expect(screen.getByTestId("kb-file-drawer")).toHaveTextContent(zh.fail_reason.source_missing);
    expect(screen.getByTestId("kb-drawer-retry")).toBeInTheDocument();
  });

  it("只读文件仍显示失败原因，但不提供重试入口", async () => {
    const readOnly = file({ file_id: "kbf_readonly", parse_status: "failed", parse_error_code: "source_missing", can_delete: false });
    mocks.fetchFiles.mockResolvedValue({ files: [readOnly], total: 1, has_more: false });
    mocks.getKbFile.mockResolvedValue(readOnly);
    renderManager();
    const row = await screen.findByTestId("kb-files-row");
    expect(within(row).getByTitle(zh.fail_reason.source_missing)).toHaveTextContent(zh.pill.parse_failed);
    expect(within(row).queryByTestId("kb-pill-retry")).not.toBeInTheDocument();
    fireEvent.click(within(row).getByRole("button", { name: zh.manager.row_menu.more }));
    expect(screen.queryByText(zh.manager.row_menu.reparse)).not.toBeInTheDocument();
    fireEvent.click(within(row).getByText(readOnly.filename));
    await screen.findByTestId("kb-file-drawer");
    expect(screen.getByTestId("kb-file-drawer")).toHaveTextContent(zh.fail_reason.source_missing);
    expect(screen.queryByTestId("kb-drawer-retry")).not.toBeInTheDocument();
  });
});

describe("对话来源卡的片段缓存", () => {
  it("重新处理、删除成功后按文件驱逐缓存（对话里再展开来源卡会重取原文）", async () => {
    const failed = file({ file_id: "kbf_slow", filename: "big.pdf", parse_status: "failed", parse_error_code: "timeout" });
    const ready = file({ file_id: "kbf_ok", filename: "ok.pdf" });
    mocks.fetchFiles.mockResolvedValue({ files: [failed, ready], total: 2, has_more: false });
    mocks.fetchChunks.mockResolvedValue({ chunks: [], total: 0 });
    mocks.reparse.mockResolvedValue(failed);
    mocks.deleteFile.mockResolvedValue(undefined);
    renderManager();
    await waitFor(() => expect(screen.getAllByTestId("kb-files-row")).toHaveLength(2));
    const [slowRow, okRow] = screen.getAllByTestId("kb-files-row");

    fireEvent.click(within(slowRow).getByTestId("kb-pill-retry"));
    await waitFor(() => expect(mocks.evictChunks).toHaveBeenCalledWith("kbf_slow"));

    fireEvent.click(within(okRow).getByText("ok.pdf"));
    await screen.findByTestId("kb-file-drawer");
    fireEvent.click(screen.getAllByTestId("kb-delete-button")[0]);
    fireEvent.click(screen.getByTestId("kb-delete-confirm-button"));
    await waitFor(() => expect(mocks.evictChunks).toHaveBeenCalledWith("kbf_ok"));
  });
});

describe("行「⋯」菜单图层", () => {
  it("挂在表格外（不被 .kb-tablewrap 的 overflow 裁掉），是可用键盘操作的菜单", async () => {
    // Real-browser measurements (chromium/webkit): old inline overlay showed only an edge on last row. jsdom cannot measure stacking/clipping;
    // enforce menu outside table subtree and menu semantics here.
    renderManager();
    const row = await screen.findByTestId("kb-files-row");
    fireEvent.click(within(row).getByRole("button", { name: zh.manager.row_menu.more }), { detail: 0 });
    const menu = await screen.findByRole("menu");
    expect(screen.getByTestId("kb-files-table").contains(menu)).toBe(false);
    await waitFor(() => expect(document.activeElement).toBe(within(menu).getAllByRole("menuitem")[0]));
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  });
});

describe("删除后撤销", () => {
  async function deleteFromRowMenu() {
    mocks.bulkDelete.mockResolvedValue({ succeeded: 1, failed: 0 });
    renderManager();
    const row = await screen.findByTestId("kb-files-row");
    fireEvent.click(within(row).getByRole("button", { name: zh.manager.row_menu.more }));
    fireEvent.click(screen.getByRole("menuitem", { name: `${zh.manager.row_menu.delete}…` }));
    fireEvent.click(screen.getByRole("button", { name: /删除 1 个/ }));
    await waitFor(() => expect(screen.queryByTestId("kb-files-row")).not.toBeInTheDocument());
    return within(await screen.findByTestId("toast-success"));
  }

  it("撤销：不删除服务端文件，并重新拉取列表把文件放回来", async () => {
    const toastEl = await deleteFromRowMenu();
    expect(toastEl.getByText("1")).toBeInTheDocument();
    const fetchesBefore = mocks.fetchFiles.mock.calls.length;

    fireEvent.click(toastEl.getByRole("button", { name: zh.toast.undo }));

    expect(await screen.findByTestId("kb-files-row")).toBeInTheDocument();
    expect(mocks.fetchFiles.mock.calls.length).toBeGreaterThan(fetchesBefore);
    await waitFor(() => expect(screen.queryByTestId("toast-success")).not.toBeInTheDocument());
    expect(mocks.bulkDelete).not.toHaveBeenCalled();
  });

  it("presents the undo toast without a status marker", async () => {
    await deleteFromRowMenu();
    expect(screen.getByTestId("toast-success").className).toContain("ui-toast--undo");
  });

  it("提示离开（手动关闭）的同一刻才真正删除", async () => {
    const toastEl = await deleteFromRowMenu();
    expect(mocks.bulkDelete).not.toHaveBeenCalled();

    fireEvent.click(toastEl.getByRole("button", { name: "关闭" }));

    await waitFor(() => expect(mocks.bulkDelete).toHaveBeenCalledWith(["kbf_1"]));
    await waitFor(() => expect(mocks.evictChunks.mock.calls.map((c) => c[0])).toContain("kbf_1"));
  });

  it("撤销窗口内的轮询不把已删除（尚未提交）的文件放回列表", async () => {
    // Another row is processing: list polls every 3 seconds; server still returns the file pending deletion.
    mocks.fetchFiles.mockResolvedValue({
      files: [file(), file({ file_id: "kbf_2", filename: "处理中.pdf", parse_status: "parsing" } as Partial<KbFileDTO>)],
      total: 2,
      has_more: false,
    });
    mocks.bulkDelete.mockResolvedValue({ succeeded: 1, failed: 0 });
    renderManager();
    await waitFor(() => expect(screen.getAllByTestId("kb-files-row")).toHaveLength(2));
    const row = screen.getAllByTestId("kb-files-row").find((r) => r.textContent?.includes("供应商合同"))!;
    fireEvent.click(within(row).getByRole("button", { name: zh.manager.row_menu.more }));
    fireEvent.click(screen.getByRole("menuitem", { name: `${zh.manager.row_menu.delete}…` }));
    fireEvent.click(screen.getByRole("button", { name: /删除 1 个/ }));
    await waitFor(() => expect(screen.getAllByTestId("kb-files-row")).toHaveLength(1));
    const fetches = mocks.fetchFiles.mock.calls.length;

    await waitFor(() => expect(mocks.fetchFiles.mock.calls.length).toBeGreaterThan(fetches), { timeout: 4_500 });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getAllByTestId("kb-files-row")).toHaveLength(1);
    expect(screen.queryByText("供应商合同-华东区.pdf")).toBeNull();

    // Returns after undo.
    fireEvent.click(within(screen.getByTestId("toast-success")).getByRole("button", { name: zh.toast.undo }));
    await waitFor(() => expect(screen.getAllByTestId("kb-files-row")).toHaveLength(2));
    expect(mocks.bulkDelete).not.toHaveBeenCalled();
  }, 10_000);

  it.each([
    ["撤销", zh.toast.undo],
    ["关闭提示", "关闭"],
  ])("删除后切换范围再%s：列表按当前范围刷新，不变回删除时的范围", async (_label, button) => {
    const toastEl = await deleteFromRowMenu();
    mocks.fetchFiles.mockImplementation(async (params: { scope?: string }) =>
      params.scope === "team"
        ? { files: [file({ file_id: "kbf_team", filename: "团队手册.pdf", scope: "team" } as Partial<KbFileDTO>)], total: 1, has_more: false }
        : { files: [file()], total: 1, has_more: false },
    );
    await selectScope("team");
    expect(await screen.findByText("团队手册.pdf")).toBeInTheDocument();

    fireEvent.click(toastEl.getByRole("button", { name: button }));
    await waitFor(() => expect(screen.queryByTestId("toast-success")).not.toBeInTheDocument());
    await new Promise((resolve) => setTimeout(resolve, 50));

    const lastListCall = mocks.fetchFiles.mock.calls.filter((call) => call[0]?.limit !== 1).at(-1);
    expect(lastListCall?.[0]).toMatchObject({ scope: "team" });
    expect(screen.getByText("团队手册.pdf")).toBeInTheDocument();
    expect(screen.queryByText("供应商合同-华东区.pdf")).toBeNull();
  });
});

describe("行「⋯」菜单", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("行滚出可见区域时关闭菜单，不留在视口边上", async () => {
    const io = installFakeIntersectionObserver();
    renderManager();
    const row = await screen.findByTestId("kb-files-row");
    const more = within(row).getByRole("button", { name: zh.manager.row_menu.more });
    fireEvent.click(more);
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await waitFor(() => expect(io.watching(more)).toBe(1));

    // Partial visibility (a few pixels scrolled for focus) does not close it.
    io.setVisible(more, true);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    io.setVisible(more, false);
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(io.watching(more)).toBe(0);
  });
});

// Behavior batch A: unavailable Change visibility/Delete explain reason; confirmation-first deletion carries “…”;
// Retry parsing deletes no data and executes directly, no longer red.
describe("行「⋯」菜单：禁用原因与危险项（行为批 A）", () => {
  const reasonOf = (item: HTMLElement) => {
    const id = item.getAttribute("aria-describedby");
    return id ? document.getElementById(id)?.textContent : null;
  };

  it.each([
    ["team", "change_scope_reason_team", "delete_reason_team"],
    ["space", "change_scope_reason", "delete_reason"],
    ["personal", "change_scope_reason_personal", "delete_reason_personal"],
  ] as const)("没有权限（%s 范围）：按范围写明谁能操作，第二行常显，选中不执行", async (scope, scopeKey, deleteKey) => {
    mocks.fetchFiles.mockResolvedValue({
      files: [file({ scope, can_delete: false, can_change_scope: false } as never)],
      total: 1,
      has_more: false,
    });
    renderManager();
    const row = await screen.findByTestId("kb-files-row");
    fireEvent.click(within(row).getByRole("button", { name: zh.manager.row_menu.more }));
    const menu = await screen.findByRole("menu");
    const change = within(menu).getByRole("menuitem", { name: zh.manager.row_menu.change_scope });
    const del = within(menu).getByRole("menuitem", { name: zh.manager.row_menu.delete });
    expect(reasonOf(change)).toBe(zh.manager.row_menu[scopeKey]);
    expect(reasonOf(del)).toBe(zh.manager.row_menu[deleteKey]);
    fireEvent.click(del);
    expect(screen.queryByRole("button", { name: /删除 1 个/ })).not.toBeInTheDocument();
  });

  it("可删除：删除带「…」、是危险项，选中先确认；重试解析不标红", async () => {
    mocks.fetchFiles.mockResolvedValue({
      files: [file({ parse_status: "failed", parse_error_code: "timeout" } as never)],
      total: 1,
      has_more: false,
    });
    renderManager();
    const row = await screen.findByTestId("kb-files-row");
    fireEvent.click(within(row).getByRole("button", { name: zh.manager.row_menu.more }));
    const menu = await screen.findByRole("menu");
    const reparse = within(menu).getByRole("menuitem", { name: zh.manager.row_menu.reparse });
    expect(reparse.classList.contains("danger")).toBe(false);
    const del = within(menu).getByRole("menuitem", { name: `${zh.manager.row_menu.delete}…` });
    expect(del.classList.contains("danger")).toBe(true);
    expect(del.getAttribute("aria-describedby")).toBeNull();
    fireEvent.click(del);
    expect(await screen.findByRole("button", { name: /删除 1 个/ })).toBeInTheDocument();
    expect(mocks.bulkDelete).not.toHaveBeenCalled();
    expect(mocks.deleteFile).not.toHaveBeenCalled();
  });
});


describe("上传后定位", () => {
  it("从空间列表上传个人文件后回到个人第一页并清除筛选", async () => {
    const uploaded = file({ file_id: "kbf_new", filename: "新文件.txt", mime_type: "text/plain" });
    let uploadedDone = false;
    mocks.uploadFile.mockImplementation(async () => { uploadedDone = true; return uploaded; });
    mocks.fetchFiles.mockImplementation(async (params: { scope: string; q?: string }) => ({
      files: uploadedDone && params.scope === "personal" && !params.q ? [uploaded] : [],
      total: uploadedDone && params.scope === "personal" ? 1 : 0, has_more: false,
    }));
    renderManager();
    await selectScope("space");
    fireEvent.change(screen.getByPlaceholderText(zh.search_placeholder), { target: { value: "旧名字" } });
    fireEvent.click(screen.getByRole("button", { name: new RegExp(zh.upload_button) }));
    fireEvent.click(within(screen.getByTestId("kb-upload-modal")).getByTestId("kb-scope-personal"));
    fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [new File(["sample"], "新文件.txt", { type: "text/plain" })] } });
    await waitFor(() => expect(screen.getByTestId("kb-upload-submit")).not.toBeDisabled());
    fireEvent.click(screen.getByTestId("kb-upload-submit"));
    expect(await screen.findByText("新文件.txt", { selector: ".ftext" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText(zh.search_placeholder)).toHaveValue("");
    expect(mocks.fetchFiles).toHaveBeenCalledWith(expect.objectContaining({ scope: "personal", offset: 0, order_by: "uploaded_at", order_dir: "desc", q: undefined }));
    const target = new URL(mocks.push.mock.calls.at(-1)![0], "https://test.invalid");
    expect(target.searchParams.get("scope")).toBe("personal");
    expect(target.searchParams.has("q")).toBe(false);
  });
});

describe("pagination glyphs", () => {
  it("renders ← before Prev and → after Next", async () => {
    mocks.fetchFiles.mockResolvedValue({ files: [file()], total: 45, has_more: true });
    const { container } = renderManager();
    await screen.findByText("供应商合同-华东区.pdf");
    const [prev, ...rest] = Array.from(container.querySelectorAll<HTMLButtonElement>(".kb-pagination .kb-pagebtn"));
    const next = rest[rest.length - 1];
    expectRefreshedGlyph(prev, "←", "start", catalogText("zh-CN", "knowledge_base.manager.pagination.prev"));
    expectRefreshedGlyph(next, "→", "end", catalogText("zh-CN", "knowledge_base.manager.pagination.next"));
  });

  it("hides the pager for a single page, an empty list and a failed load", async () => {
    const { container, unmount } = renderManager();
    await screen.findByText("供应商合同-华东区.pdf");
    expect(container.querySelector(".kb-pagination")).toBeNull();
    unmount();

    mocks.fetchFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    const empty = renderManager();
    await waitFor(() => expect(mocks.fetchFiles).toHaveBeenCalled());
    await waitFor(() => expect(empty.container.querySelector(".kb-skel")).toBeNull());
    expect(empty.container.querySelector(".kb-pagination")).toBeNull();
    empty.unmount();

    mocks.fetchFiles.mockRejectedValue(new Error("upstream unavailable"));
    const failed = renderManager();
    await screen.findByTestId("kb-error");
    expect(failed.container.querySelector(".kb-pagination")).toBeNull();
  });
});

describe("failed list load", () => {
  it("shows one error card with a retry instead of the empty state, and retry reloads the current view", async () => {
    // Every list call fails (scope count probes use the same endpoint), then the service comes back.
    mocks.fetchFiles.mockRejectedValue(new Error("upstream unavailable"));
    renderManager();
    const card = await screen.findByTestId("kb-error");
    expect(card).toHaveAttribute("role", "alert");
    expect(card).toHaveTextContent(zh.files_view.load_failed_title);
    expect(screen.queryByText(zh.files_view.empty_personal_title)).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-files-table")).not.toBeInTheDocument();

    mocks.fetchFiles.mockResolvedValue({ files: [file()], total: 1, has_more: false });
    const calls = mocks.fetchFiles.mock.calls.length;
    fireEvent.click(within(card).getByRole("button", { name: zh.files_view.load_retry }));
    expect(await screen.findByText("供应商合同-华东区.pdf")).toBeInTheDocument();
    expect(mocks.fetchFiles.mock.calls.length).toBeGreaterThan(calls);
    expect(screen.queryByTestId("kb-error")).not.toBeInTheDocument();
  });
});
