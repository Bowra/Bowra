// Knowledge bases (FEATURE_KNOWLEDGE_BASES): Delete knowledge base… confirms with the file count (PRD Q5).
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";

import { SPACES, kbFixture } from "./kb-directory.fixtures";

const mocks = vi.hoisted(() => ({
  remove: vi.fn(),
  notifyChanged: vi.fn(),
  notify: vi.fn(),
}));

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: SPACES, activeId: "ws_1", loading: false, error: null, refresh: vi.fn() }),
}));
vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-bases-api")>();
  return {
    ...actual,
    deleteKnowledgeBase: (...args: unknown[]) => mocks.remove(...args),
    notifyKnowledgeBasesChanged: () => mocks.notifyChanged(),
  };
});
vi.mock("@/lib/use-toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-toast")>();
  return { ...actual, notify: (...args: unknown[]) => mocks.notify(...args) };
});

import { KbDeleteDialog } from "./kb-delete-dialog";

const kb = kbFixture({ id: "kb_ops", file_count: 21, revision: 4, level: "manage" });

beforeEach(() => {
  mocks.remove.mockReset();
  mocks.notifyChanged.mockReset();
  mocks.notify.mockReset();
});
afterEach(cleanup);

function renderDialog() {
  const onClose = vi.fn();
  const onDeleted = vi.fn();
  renderIntl(<KbDeleteDialog kb={kb} onClose={onClose} onDeleted={onDeleted} />);
  return { onClose, onDeleted, dialog: screen.getByRole("dialog", { name: "删除知识库「门店运营手册」？" }) };
}

describe("KbDeleteDialog", () => {
  it("names the file count and that originals stay in Files; Cancel deletes nothing", () => {
    const { dialog, onClose } = renderDialog();
    expect(within(dialog).getByTestId("kb-delete-files")).toHaveTextContent("其中的 21 个文件会移出知识库，原件仍在「文件」中。");
    expect(dialog).toHaveTextContent("这个操作不能撤销");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(mocks.remove).not.toHaveBeenCalled();
  });

  it("deletes with the confirmed count and revision, then refreshes readers", async () => {
    mocks.remove.mockResolvedValue({ deleted: true, knowledge_base_id: "kb_ops", files_removed: 21 });
    const { dialog, onDeleted } = renderDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除知识库" }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    expect(mocks.remove).toHaveBeenCalledWith("kb_ops", { file_count: 21, revision: 4 });
    expect(mocks.notifyChanged).toHaveBeenCalled();
    expect(mocks.notify).toHaveBeenCalledWith("success", "已删除知识库「门店运营手册」");
  });

  it("shows the new count when files changed meanwhile and confirms again with it", async () => {
    mocks.remove.mockRejectedValueOnce(
      new ApiError(409, "Files were added…", {
        code: "knowledge_base_files_changed",
        body: { detail: { error: { code: "knowledge_base_files_changed", file_count: 23 } } },
      }),
    );
    mocks.remove.mockResolvedValueOnce({ deleted: true, knowledge_base_id: "kb_ops", files_removed: 23 });
    const { dialog, onDeleted } = renderDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除知识库" }));
    expect(await within(dialog).findByTestId("kb-delete-count-changed")).toHaveTextContent("文件数量刚刚变成了 23 个。请核对后再确认删除。");
    expect(within(dialog).getByTestId("kb-delete-files")).toHaveTextContent("其中的 23 个文件会移出知识库");
    expect(onDeleted).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "删除知识库" }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    expect(mocks.remove).toHaveBeenLastCalledWith("kb_ops", { file_count: 23, revision: 4 });
  });

  it("explains other refusals with their finite code, never the server text", async () => {
    mocks.remove.mockRejectedValue(
      new ApiError(409, "Some files could not leave…", { code: "knowledge_base_files_remaining" }),
    );
    const { dialog, onDeleted } = renderDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除知识库" }));
    expect(await within(dialog).findByText(/部分文件无法移出知识库，因此知识库没有删除/)).toBeInTheDocument();
    expect(dialog).not.toHaveTextContent("Some files could not leave");
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("falls back to an actionable message when the failure has no code", async () => {
    mocks.remove.mockRejectedValue(new Error("socket hang up"));
    const { dialog } = renderDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除知识库" }));
    expect(await within(dialog).findByText("知识库没有删除，请稍后重试；如果一直失败，请联系管理员。")).toBeInTheDocument();
  });
});
