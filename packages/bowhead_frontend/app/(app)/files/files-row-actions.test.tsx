/**
 * File-page row-end “⋯” (appearance batch four): Download, Use in New Chat | Delete (no Delete for knowledge-base files);
 * recycle bin uses Restore | Delete Permanently. Flag off preserves inline buttons.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import type { FileDTO } from "@/lib/api";

const api = vi.hoisted(() => ({
  listFiles: vi.fn(),
  fetchFilesUsage: vi.fn(),
  deleteFile: vi.fn(),
  purgeFiles: vi.fn(),
  restoreFile: vi.fn(),
  promoteFileToKb: vi.fn(),
  removeFileFromKb: vi.fn(),
  uploadFile: vi.fn(),
}));
const handoff = vi.hoisted(() => ({ handoffAttachmentsToNewChat: vi.fn() }));

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<Record<string, unknown>>()), ...api }));
vi.mock("@/lib/composer-handoff", () => handoff);
vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn(async () => ({ team_role: "member" })) }));
vi.mock("@/lib/use-space", () => ({ useSpaceSelection: () => ({ activeId: "ws_1", items: [] }) }));
vi.mock("@/lib/kb-ingestion-readiness", () => ({
  useKbIngestionReadiness: () => ({ state: "ready", retry: vi.fn() }),
}));
vi.mock("@/components/shell/AppShell", () => ({ AppShell: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/components/shell/Topbar", () => ({ Topbar: () => null }));
vi.mock("@/components/files/file-drawer", () => ({ FileDrawer: () => null }));

import { ToastContainer } from "@/components/chat/toast-container";
import { _clearAllToasts } from "@/lib/use-toast";

import FilesPage from "./page";

const T = "2026-09-22T07:15:00Z";
const file = (id: string, name: string, extra: Partial<FileDTO> = {}): FileDTO => ({
  file_id: id, filename: name, mime: "application/pdf", mime_family: "pdf", size: 1024, source: "upload",
  origin: "upload", created_at: T, last_used_at: T, deleted_at: null, category: null, category_source: null,
  usage_count: 1, kind: "upload", trust: "user", in_kb: false, kb_promotable: true, kb: null,
  session_id: null, session_title: null, has_canvas_ref: false, retention_hint: null, url: `/api/v1/files/${id}`,
  ...extra,
});
const PLAIN = file("f_plain", "九月销售.pdf");
const IN_KB = file("f_kb", "经营手册.pdf", { in_kb: true });

beforeEach(() => {
  api.listFiles.mockImplementation(async (params: { include_deleted?: boolean }) => {
    const items = params.include_deleted ? [file("f_trash", "旧价目表.pdf", { deleted_at: T })] : [PLAIN, IN_KB];
    return { items, total: items.length, size_total_bytes: 2048 };
  });
  api.fetchFilesUsage.mockResolvedValue(null);
  api.deleteFile.mockResolvedValue(undefined);
  api.restoreFile.mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("文件页行尾「⋯」", () => {
  it("explains how to fix a missing knowledge-base embedding model", async () => {
    api.promoteFileToKb.mockRejectedValue(new Error("kb_embedding_not_configured"));
    renderIntl(<FilesPage />);
    await screen.findByText("九月销售.pdf");
    fireEvent.click(screen.getByTestId("kb-add"));
    fireEvent.click(screen.getByTestId("kb-add-confirm"));

    expect(await screen.findByText(/团队尚未配置并启用知识库嵌入模型/)).toBeTruthy();
  });

  it("row menu lists download, use in new chat | delete; knowledge-base files have no delete", async () => {
    renderIntl(<FilesPage />);
    const menu = await openRowMenu("九月销售.pdf 的更多操作");
    // Behavior batch B (user decision 2026-09-28): deletion is undoable, without confirmation or “…”.
    expect(menuItemNames(menu)).toEqual(["下载", "在新会话中使用", "删除"]);
    expect(within(menu).getByRole("menuitem", { name: "删除" }).className).toContain("is-danger");
    fireEvent.keyDown(menu, { key: "Escape" });
    const kbMenu = await openRowMenu("经营手册.pdf 的更多操作");
    expect(menuItemNames(kbMenu)).toEqual(["下载", "在新会话中使用"]);
  });

  it("use in new chat hands the file to a new chat without deleting it", async () => {
    renderIntl(<FilesPage />);
    const menu = await openRowMenu("九月销售.pdf 的更多操作");
    fireEvent.click(within(menu).getByRole("menuitem", { name: "在新会话中使用" }));
    expect(handoff.handoffAttachmentsToNewChat).toHaveBeenCalledWith([
      expect.objectContaining({ fileId: "f_plain", filename: "九月销售.pdf" }),
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.deleteFile).not.toHaveBeenCalled();
  });

  it("delete runs without confirmation; a file referenced by a report still explains the consequence first", async () => {
    api.listFiles.mockImplementation(async (params: { include_deleted?: boolean }) => {
      const items = params.include_deleted ? [] : [PLAIN, file("f_ref", "季度复盘附件.pdf", { has_canvas_ref: true })];
      return { items, total: items.length, size_total_bytes: 2048 };
    });
    renderIntl(<FilesPage />);
    const menu = await openRowMenu("九月销售.pdf 的更多操作");
    fireEvent.click(within(menu).getByRole("menuitem", { name: "删除" }));
    await waitFor(() => expect(api.deleteFile).toHaveBeenCalledWith("f_plain"));
    expect(screen.queryByRole("dialog")).toBeNull();
    const refMenu = await openRowMenu("季度复盘附件.pdf 的更多操作");
    fireEvent.click(within(refMenu).getByRole("menuitem", { name: "删除…" }));
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(api.deleteFile).not.toHaveBeenCalledWith("f_ref");
    expect(handoff.handoffAttachmentsToNewChat).not.toHaveBeenCalled();
  });

  it("trash menu lists restore | delete permanently; restore only restores", async () => {
    renderIntl(<FilesPage />);
    await screen.findByText("九月销售.pdf");
    fireEvent.click(screen.getByRole("tab", { name: /回收站/ }));
    const menu = await openRowMenu("旧价目表.pdf 的更多操作");
    expect(menuItemNames(menu)).toEqual(["恢复", "永久删除…"]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "恢复" }));
    await waitFor(() => expect(api.restoreFile).toHaveBeenCalledWith("f_trash"));
    expect(api.purgeFiles).not.toHaveBeenCalled();
  });
});

describe("文件页删除后撤销（行为批 B）", () => {
  beforeEach(() => _clearAllToasts());
  afterEach(() => _clearAllToasts());

  async function deletePlain() {
    renderIntl(<><FilesPage /><ToastContainer /></>);
    const menu = await openRowMenu("九月销售.pdf 的更多操作");
    fireEvent.click(within(menu).getByRole("menuitem", { name: "删除" }));
    await waitFor(() => expect(api.deleteFile).toHaveBeenCalledWith("f_plain"));
  }

  it("delete shows the global undo toast; undo restores and refreshes the list", async () => {
    await deletePlain();
    const toastNode = await screen.findByTestId("toast-info");
    expect(toastNode.textContent).toContain("已删除 九月销售.pdf");
    expect(toastNode.className).toContain("ui-toast--undo");
    // This page's old countdown notice no longer appears.
    expect(document.querySelector(".toasts .tundo")).toBeNull();
    const lists = api.listFiles.mock.calls.length;
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(api.restoreFile).toHaveBeenCalledWith("f_plain"));
    await waitFor(() => expect(api.listFiles.mock.calls.length).toBeGreaterThan(lists));
    expect(await screen.findByText("已撤销删除")).toBeTruthy();
  });

  it("when every restore fails the toast says so, stays actionable and retries", async () => {
    api.restoreFile.mockRejectedValueOnce(new Error("boom"));
    await deletePlain();
    const toastNode = await screen.findByTestId("toast-info");
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    expect(await screen.findByText("没能恢复这些文件，它们仍在回收站里保留 30 天。")).toBeTruthy();
    // Node stays in DOM during exit animation: check Undo is enabled again (failed Toast returns to actionable state rather than closing as completed).
    await waitFor(() => expect((within(toastNode).getByRole("button", { name: "撤销" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByText("已撤销删除")).toBeNull();
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(api.restoreFile).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("已撤销删除")).toBeTruthy();
  });

  it("partial restore failure after bulk delete reports counts and retries only the failed files", async () => {
    const OTHER = file("f_other", "十月销售.pdf");
    api.listFiles.mockImplementation(async (params: { include_deleted?: boolean }) => {
      const items = params.include_deleted ? [] : [PLAIN, OTHER];
      return { items, total: items.length, size_total_bytes: 2048 };
    });
    api.restoreFile.mockImplementation(async (id: string) => {
      if (id === "f_other" && api.restoreFile.mock.calls.filter((c) => c[0] === "f_other").length === 1) throw new Error("boom");
    });
    renderIntl(<><FilesPage /><ToastContainer /></>);
    await screen.findByText("十月销售.pdf");
    fireEvent.click(screen.getByRole("button", { name: "九月销售.pdf" }));
    fireEvent.click(screen.getByRole("button", { name: "十月销售.pdf" }));
    fireEvent.click(screen.getByRole("button", { name: "批量删除" }));
    // Bulk deletion likewise needs no confirmation.
    await waitFor(() => expect(api.deleteFile).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog")).toBeNull();
    const toastNode = await screen.findByTestId("toast-info");
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    expect(await screen.findByText(/已恢复 1 个文件，1 个没能恢复/)).toBeTruthy();
    expect(screen.queryByText("已撤销删除")).toBeNull();
    await waitFor(() => expect((within(toastNode).getByRole("button", { name: "撤销" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(api.restoreFile).toHaveBeenCalledTimes(3));
    expect(api.restoreFile.mock.calls.map((c) => c[0])).toEqual(["f_plain", "f_other", "f_other"]);
    expect(await screen.findByText("已撤销删除")).toBeTruthy();
  });
});
