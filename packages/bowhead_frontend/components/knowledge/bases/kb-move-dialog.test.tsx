// Knowledge bases (FEATURE_KNOWLEDGE_BASES): "Move to another knowledge base" (PRD §4.6, mockup ⑦).
import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { KnowledgeBaseMoveResult } from "@/lib/knowledge-bases-api";
import messagesZh from "@/messages/zh-CN.json";

import { SPACES_FIXTURE, directoryFixture } from "./kb-target-picker.fixtures";

const mocks = vi.hoisted(() => ({
  move: vi.fn(),
  notify: vi.fn(),
  directory: null as ReturnType<typeof directoryFixture> | null,
}));

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: SPACES_FIXTURE, activeId: "ws_1", loading: false, error: null, setActiveId: () => {}, refresh: () => {} }),
}));
vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/knowledge-bases-api")>()),
  moveKnowledgeBaseFiles: mocks.move,
  notifyKnowledgeBasesChanged: mocks.notify,
}));
vi.mock("./use-knowledge-bases", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./use-knowledge-bases")>()),
  useKnowledgeBaseDirectory: () => ({ directory: mocks.directory, loading: false, error: null, refresh: vi.fn() }),
}));

import { KbMoveDialog } from "./kb-move-dialog";

const ONE = [{ file_id: "kbf_1", filename: "开店检查清单 2026.pdf" }];
const TWO = [...ONE, { file_id: "kbf_2", filename: "生鲜陈列标准.docx" }];

function result(over: Partial<KnowledgeBaseMoveResult> = {}): KnowledgeBaseMoveResult {
  return { dry_run: true, target_knowledge_base_id: "kb_space", results: [{ file_id: "kbf_1", ok: true }], succeeded: 1, failed: 0, unchanged: 0, movable: 1, gains: 0, losses: 0, ...over };
}

function renderDialog(files = ONE, current: string | null = "kb_ops") {
  const onClose = vi.fn();
  const onMoved = vi.fn();
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <KbMoveDialog files={files} currentKnowledgeBaseId={current} onClose={onClose} onMoved={onMoved} />
    </NextIntlClientProvider>,
  );
  return { onClose, onMoved };
}

beforeEach(() => {
  mocks.directory = directoryFixture();
  mocks.move.mockReset();
  mocks.notify.mockReset();
});
afterEach(cleanup);

describe("KbMoveDialog", () => {
  it("names where the file is, lists only editable knowledge bases and marks the current one", () => {
    renderDialog();
    expect(screen.getByRole("dialog", { name: "移到其他知识库" })).toBeInTheDocument();
    expect(screen.getByText(/「开店检查清单 2026\.pdf」现在在「门店运营手册」/)).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "目标知识库" })).toBeInTheDocument();
    expect(screen.queryByTestId("kb-target-kb_policy")).toBeNull();
    expect(screen.getByTestId("kb-target-kb_ops")).toBeDisabled();
    expect(screen.getByTestId("kb-move-confirm")).toBeDisabled();
    expect(mocks.move).not.toHaveBeenCalled();
  });

  it("states who gains and loses access after a dry run, then moves on confirmation", async () => {
    mocks.move
      .mockResolvedValueOnce(result({ gains: 12, losses: 3 }))
      .mockResolvedValueOnce(result({ dry_run: false }));
    const { onMoved } = renderDialog();
    fireEvent.click(screen.getByTestId("kb-target-kb_space"));
    expect(mocks.move).toHaveBeenLastCalledWith({ file_ids: ["kbf_1"], target_knowledge_base_id: "kb_space", dry_run: true });
    expect(await screen.findByTestId("kb-move-access")).toHaveTextContent("12 人将获得访问权限 · 3 人将失去访问权限");
    expect(screen.getByText("移动后，原文件、版本、引用、文档矩阵和提取结果都会保留。")).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByTestId("kb-move-confirm")); });
    expect(mocks.move).toHaveBeenLastCalledWith({ file_ids: ["kbf_1"], target_knowledge_base_id: "kb_space" });
    expect(mocks.notify).toHaveBeenCalledOnce();
    expect(onMoved).toHaveBeenCalledWith({ failedIds: [], targetId: "kb_space" });
  });

  it("says access stays the same when nobody gains or loses it", async () => {
    mocks.move.mockResolvedValueOnce(result());
    renderDialog();
    fireEvent.click(screen.getByTestId("kb-target-kb_space"));
    expect(await screen.findByTestId("kb-move-access")).toHaveTextContent("谁能访问不变");
  });

  it("lists files that cannot move, with their reasons, and moves only the rest", async () => {
    mocks.move.mockResolvedValueOnce(result({
      target_knowledge_base_id: "kb_comp",
      results: [{ file_id: "kbf_1", ok: true }, { file_id: "kbf_2", ok: false, error_code: "kb_file_access_denied" }],
      succeeded: 1,
      failed: 1,
      movable: 1,
      gains: 0,
      losses: 5,
    }));
    renderDialog(TWO, null);
    expect(screen.getByText(/^2 个文件将移到所选知识库/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("kb-target-kb_comp"));
    const blocked = await screen.findByTestId("kb-move-blocked");
    expect(blocked).toHaveTextContent("生鲜陈列标准.docx：移动文件需要这个知识库的“可编辑”权限");
    expect(screen.getByTestId("kb-move-access")).toHaveTextContent("0 人将获得访问权限 · 5 人将失去访问权限");
    expect(screen.getByTestId("kb-move-confirm")).toHaveTextContent("移动 1 个文件");
  });

  it("blocks the move when no file can move", async () => {
    mocks.move.mockResolvedValueOnce(result({
      results: [{ file_id: "kbf_1", ok: false, error_code: "knowledge_base_move_conflict" }],
      succeeded: 0,
      failed: 1,
      movable: 0,
    }));
    renderDialog();
    fireEvent.click(screen.getByTestId("kb-target-kb_space"));
    expect(await screen.findByTestId("kb-move-blocked")).toHaveTextContent(messagesZh.knowledge_base.errors.knowledge_base_move_conflict);
    expect(screen.getByText(/所选文件都不能移到这个知识库/)).toBeInTheDocument();
    expect(screen.getByTestId("kb-move-confirm")).toBeDisabled();
  });

  it("explains a target that is gone or needs a higher level, without moving", async () => {
    mocks.move.mockRejectedValueOnce(new ApiError(404, "x", { code: "knowledge_base_not_found" }));
    renderDialog();
    fireEvent.click(screen.getByTestId("kb-target-kb_space"));
    expect(await screen.findByText(messagesZh.knowledge_base.errors.knowledge_base_not_found)).toBeInTheDocument();
    expect(screen.getByTestId("kb-move-confirm")).toBeDisabled();
    mocks.move.mockRejectedValueOnce(new ApiError(403, "x", { code: "knowledge_base_level_required" }));
    fireEvent.click(screen.getByTestId("kb-target-kb_my"));
    expect(await screen.findByText(messagesZh.knowledge_base.errors.knowledge_base_level_required)).toBeInTheDocument();
  });

  it("reports per-file failures after the move and hands them over when dismissed", async () => {
    mocks.move
      .mockResolvedValueOnce(result({ results: [{ file_id: "kbf_1", ok: true }, { file_id: "kbf_2", ok: true }], succeeded: 2, movable: 2 }))
      .mockResolvedValueOnce(result({
        dry_run: false,
        results: [{ file_id: "kbf_1", ok: true }, { file_id: "kbf_2", ok: false, error_code: "knowledge_base_move_conflict" }],
        succeeded: 1,
        failed: 1,
      }));
    const { onMoved, onClose } = renderDialog(TWO, null);
    fireEvent.click(screen.getByTestId("kb-target-kb_space"));
    await waitFor(() => expect(screen.getByTestId("kb-move-confirm")).toBeEnabled());
    await act(async () => { fireEvent.click(screen.getByTestId("kb-move-confirm")); });
    const outcome = screen.getByTestId("kb-move-outcome");
    expect(outcome).toHaveTextContent("已移动 1 个文件。");
    expect(outcome).toHaveTextContent(`生鲜陈列标准.docx：${messagesZh.knowledge_base.errors.knowledge_base_move_conflict}`);
    expect(mocks.notify).toHaveBeenCalledOnce();
    expect(onMoved).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("kb-move-done"));
    expect(onMoved).toHaveBeenCalledWith({ failedIds: ["kbf_2"], targetId: "kb_space" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps the dialog open with an actionable error when the move request fails", async () => {
    mocks.move
      .mockResolvedValueOnce(result())
      .mockRejectedValueOnce(new ApiError(403, "x", { code: "knowledge_base_level_required" }));
    const { onMoved } = renderDialog();
    fireEvent.click(screen.getByTestId("kb-target-kb_space"));
    await waitFor(() => expect(screen.getByTestId("kb-move-confirm")).toBeEnabled());
    await act(async () => { fireEvent.click(screen.getByTestId("kb-move-confirm")); });
    expect(screen.getByText(`移动没有完成：${messagesZh.knowledge_base.errors.knowledge_base_level_required}`)).toBeInTheDocument();
    expect(onMoved).not.toHaveBeenCalled();
    expect(mocks.notify).not.toHaveBeenCalled();
  });
});
