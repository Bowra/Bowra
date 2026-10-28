/**
 * Knowledge-base deep links (Library › Files Knowledge Base column, or View Processing Progress after adding):
 *   /knowledge/?scope=<personal|space|team>[&space=<space id>]&file_id=<knowledge-base file id>
 *
 * Contract:
 *   1. scope selects the corresponding scope (list fetches that scope)
 *   2. space in your space list → switch there, then read the file/open drawer only after switching
 *   3. space absent from your list → ignore it, retain current space and read the file directly
 *   4. Unreadable file (deleted / no space permission) → explicit in-page notice, no silent fallback to list
 *   5. Closing drawer removes file_id / chunk_id from URL (retain other parameters); reentering the same deep link reopens it
 */

import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { KbFileDTO, KbScope } from "@/lib/kb-api";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({
  fetchFiles: vi.fn(),
  getFile: vi.fn(),
  fetchMe: vi.fn(),
  listDocTypes: vi.fn(),
  setActiveId: vi.fn(),
  replace: vi.fn(),
  activeId: { current: "ws_a" as string | null },
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: mocks.replace }),
}));


vi.mock("@/lib/use-space", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-space")>();
  return {
    ...actual,
    useSpaceSelection: () => ({
      items: [
        { id: "ws_a", name: "Space A", desc: "" },
        { id: "ws_b", name: "Space B", desc: "" },
      ],
      activeId: mocks.activeId.current,
      loading: false,
      error: null,
      setActiveId: mocks.setActiveId,
      refresh: vi.fn(),
    }),
  };
});

vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return {
    ...actual,
    fetchKbFiles: (...args: unknown[]) => mocks.fetchFiles(...args),
    getKbFile: (...args: unknown[]) => mocks.getFile(...args),
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

function ui(props: { initialScope?: KbScope; initialSpaceId?: string; initialFileId?: string | null; initialChunkId?: string | null }) {
  return (
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <KbFilesManager {...props} />
    </NextIntlClientProvider>
  );
}

/** Scopes used by list requests (exclude count requests with limit=1). */
function listScopes(): string[] {
  return mocks.fetchFiles.mock.calls
    .map((c) => c[0] as { scope?: string; limit?: number })
    .filter((p) => p?.limit !== 1)
    .map((p) => String(p?.scope));
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.activeId.current = "ws_a";
  mocks.fetchMe.mockResolvedValue({ user_id: "u_me", workspace_id: "ws_a", team_id: "team_main", role: "user" });
  mocks.listDocTypes.mockResolvedValue({ items: [] });
  mocks.fetchFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
  mocks.getFile.mockRejectedValue(new Error("404"));
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
});

describe("KbFilesManager · 深链范围与空间", () => {
  it("scope 参数选中对应范围", async () => {
    render(ui({ initialScope: "team" }));
    await waitFor(() => expect(listScopes().length).toBeGreaterThan(0));
    expect(listScopes()[0]).toBe("team");
    fireEvent.click(await screen.findByTestId("kb-scope-filter"), { detail: 0 });
    const menu = await screen.findByTestId("kb-scope-filter-menu");
    expect(within(menu).getByTestId("kb-scope-filter-team")).toHaveAttribute("aria-checked", "true");
    // Control: without parameters, a first visit defaults to the current space.
    cleanup();
    vi.clearAllMocks();
    mocks.fetchFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    render(ui({}));
    await waitFor(() => expect(listScopes().length).toBeGreaterThan(0));
    expect(listScopes()[0]).toBe("space");
  });

  it("space 参数在列表里：切到该空间，切过去之后才打开文件", async () => {
    const { rerender } = render(ui({ initialScope: "space", initialSpaceId: "ws_b", initialFileId: "kbf_1" }));
    await waitFor(() => expect(mocks.setActiveId).toHaveBeenCalledWith("ws_b"));
    // Still in the old space: space files authorize by current space; reading now would be denied, so defer it.
    expect(mocks.getFile).not.toHaveBeenCalled();

    mocks.activeId.current = "ws_b";
    rerender(ui({ initialScope: "space", initialSpaceId: "ws_b", initialFileId: "kbf_1" }));
    await waitFor(() => expect(mocks.getFile).toHaveBeenCalledWith("kbf_1"));
    expect(mocks.setActiveId).toHaveBeenCalledTimes(1);
  });

  it("space 参数不在你的列表里：忽略，不切空间，直接读文件", async () => {
    render(ui({ initialScope: "space", initialSpaceId: "ws_foreign", initialFileId: "kbf_2" }));
    await waitFor(() => expect(mocks.getFile).toHaveBeenCalledWith("kbf_2"));
    expect(mocks.setActiveId).not.toHaveBeenCalled();
  });
});

function dto(id: string): KbFileDTO {
  return {
    file_id: id,
    artifact_id: `art_${id}`,
    filename: "contract.md",
    mime_type: "text/markdown",
    size_bytes: 100,
    scope: "space",
    scope_id: "ws_b",
    workspace_id: "ws_b",
    team_id: "team_main",
    uploaded_by: "u_me",
    uploaded_at: "2026-09-24T00:00:00Z",
    parse_status: "queued",
    parse_progress_pct: null,
    parse_layer: null,
    parse_error: null,
    parse_duration_ms: null,
    chunks_count: 0,
    cite_count: 0,
    last_cited_at: null,
    use_llm_marker: false,
    doc_type_id: null,
    summary_json: null,
    version: 1,
    soft_deleted_at: null,
  } as KbFileDTO;
}

describe("KbFilesManager · 深链失败提示与关闭后的地址", () => {
  it("文件读不到（404 / 无权限 / 空间不在你的列表里）：页内明确提示，并去掉失效的文件参数", async () => {
    window.history.replaceState(null, "", "/knowledge/?scope=space&space=ws_foreign&file_id=kbf_gone&chunk_id=c1");
    render(ui({ initialScope: "space", initialSpaceId: "ws_foreign", initialFileId: "kbf_gone", initialChunkId: "c1" }));
    const notice = await screen.findByTestId("kb-deeplink-failed");
    expect(notice).toHaveTextContent(messagesEn.knowledge_base.files_view.deep_link_failed);
    expect(mocks.replace).toHaveBeenCalledWith("/knowledge/?scope=space&space=ws_foreign", { scroll: false });
    fireEvent.click(screen.getByRole("button", { name: messagesEn.knowledge_base.files_view.deep_link_dismiss }));
    expect(screen.queryByTestId("kb-deeplink-failed")).toBeNull();
  });

  it("打开成功不显示提示；关闭抽屉后去掉 file_id / chunk_id，再次进入同一深链重新打开", async () => {
    mocks.getFile.mockResolvedValue(dto("kbf_1"));
    window.history.replaceState(null, "", "/knowledge/?scope=personal&file_id=kbf_1&chunk_id=c1");
    const { rerender } = render(ui({ initialScope: "personal", initialFileId: "kbf_1", initialChunkId: "c1" }));
    await screen.findByTestId("kb-file-drawer");
    expect(screen.queryByTestId("kb-deeplink-failed")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: messagesEn.knowledge_base.drawer.close_aria }));
    expect(screen.queryByTestId("kb-file-drawer")).toBeNull();
    expect(mocks.replace).toHaveBeenCalledWith("/knowledge/?scope=personal", { scroll: false });

    // Router removes parameters → page passes null; reenter same deep link → parameters return and drawer reopens.
    window.history.replaceState(null, "", "/knowledge/?scope=personal");
    rerender(ui({ initialScope: "personal", initialFileId: null, initialChunkId: null }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId("kb-file-drawer")).toBeNull();
    rerender(ui({ initialScope: "personal", initialFileId: "kbf_1", initialChunkId: "c1" }));
    await screen.findByTestId("kb-file-drawer");
    expect(mocks.getFile).toHaveBeenCalledTimes(2);
  });
});
