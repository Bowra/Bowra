/**
 * Knowledge base navigation discovery #1: matrix View Original opens file details in place and locates the source; closing retains matrix and side panel.
 * Previously switched to Files (leaving the matrix, wrong background scope, no source positioning).
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { KbFileDTO } from "@/lib/kb-api";
import type { MatrixCellVM, MatrixColumnVM, MatrixRowVM } from "@/lib/kb-matrix/types";
import messagesZh from "@/messages/zh-CN.json";

vi.mock("@/lib/canvas-pm-adapter/kb-chunk-cache", () => ({
  useKbChunkContent: () => ({ status: "idle" }),
}));

const FILE = {
  file_id: "kbf_1",
  filename: "评测.md",
  scope: "space",
  scope_id: "ws_1",
  parse_status: "done",
  chunks_count: 2,
  size_bytes: 600,
  uploaded_at: "2026-09-01T00:00:00Z",
  uploaded_by: "u_1",
  // Offer no deletion here even with permission: in-place viewing is read-only.
  can_delete: true,
} as unknown as KbFileDTO;

vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return {
    ...actual,
    getKbFile: vi.fn(async () => FILE),
    fetchKbFileChunks: vi.fn(async () => ({
      chunks: [
        { chunk_id: "chunk_1", content: "第一段正文", page: null, section_title: null, role: "intro", chunk_index: 1 },
        { chunk_id: "chunk_3", content: "官方指导价 6499 元起", page: null, section_title: null, role: "body", chunk_index: 3 },
      ],
      total: 2,
    })),
  };
});

import { SidePanel } from "./side-panel";

const ROW: MatrixRowVM = { fileId: "kbf_1", fileName: "评测.md", missing: false, extractFailed: false, recordId: "rec_1", approved: false, cells: {} };
const COL: MatrixColumnVM = { columnId: "c1", name: "提到的价格", type: "text", options: [] };
const CELL: MatrixCellVM = { status: "done", value: "6499 元", variants: [], edited: false, verified: false, chunkId: "chunk_3", sourceExcerpt: "6499 元起" };

afterEach(cleanup);

describe("SidePanel · 查看原文", () => {
  it("原地打开文件详情并置顶出处，关掉后侧板仍在，不跳页", async () => {
    const href = window.location.href;
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <SidePanel row={ROW} col={COL} cell={CELL} canEdit={false} saving={false} onClose={vi.fn()} onSaveValue={vi.fn()} onMarkVerified={vi.fn()} />
      </NextIntlClientProvider>,
    );
    const view = screen.getByRole("button", { name: /查看原文/ });
    expect(view.tagName).toBe("BUTTON");
    fireEvent.click(view);

    const drawer = await screen.findByTestId("kb-file-drawer");
    expect(window.location.href).toBe(href);
    await waitFor(() => expect(drawer.textContent).toContain("矩阵出处"));
    const pinnedLabel = screen.getByText(/矩阵出处/);
    expect(pinnedLabel.closest("li, article, section, div")?.textContent ?? "").toContain("6499");
    expect(screen.queryByTestId("kb-delete-button")).toBeNull();

    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("kb-file-drawer")).toBeNull());
    expect(screen.getByText("6499 元")).toBeTruthy();
  });
});
