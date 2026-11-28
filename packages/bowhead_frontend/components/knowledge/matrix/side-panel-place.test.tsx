/**
 * Document matrix step 1 S7: the source line names the page when the file has pages (PDF,
 * slides, images) and the paragraph (chunk position) otherwise. Cells written before S7 keep the
 * page they stored and have no chunk position.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Variant } from "@/lib/kb-knowledge-tables-api";
import type { MatrixCellVM, MatrixColumnVM, MatrixRowVM } from "@/lib/kb-matrix/types";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

vi.mock("@/lib/canvas-pm-adapter/kb-chunk-cache", () => ({
  useKbChunkContent: () => ({ status: "idle" }),
}));

import { CitePopover } from "./cite-popover";
import { SidePanel } from "./side-panel";

const ROW: MatrixRowVM = { fileId: "kbf_1", fileName: "合同.docx", missing: false, extractFailed: false, recordId: "", approved: false, cells: {} };
const COL: MatrixColumnVM = { columnId: "c1", name: "金额", type: "text", options: [] };
const BASE: MatrixCellVM = { status: "done", value: "500 万", variants: [], edited: false, verified: false, chunkId: "chk_3", sourceExcerpt: "合同总金额" };

function variant(extra: Partial<Variant>): Variant {
  return {
    raw: null,
    normalized: { value: "500 万" },
    effective_at: null,
    is_primary: true,
    extracted_by: "tier1",
    confidence: 0.9,
    source_excerpt: "合同总金额",
    chunk_id: "chk_3",
    page: null,
    ...extra,
  };
}

function renderPanel(cell: MatrixCellVM, messages: typeof messagesZh = messagesZh, locale = "zh-CN") {
  render(
    <NextIntlClientProvider locale={locale} messages={messages}>
      <SidePanel row={ROW} col={COL} cell={cell} canEdit={false} saving={false} onClose={vi.fn()} onSaveValue={vi.fn()} onMarkVerified={vi.fn()} />
    </NextIntlClientProvider>,
  );
}

function sourceLine(): string {
  return document.querySelector(".mx-srcmeta")?.textContent ?? "";
}

afterEach(cleanup);

describe("Matrix source place (step 1 S7)", () => {
  it("shows the paragraph when the cell has no page", () => {
    renderPanel({ ...BASE, chunkIndex: 3 });
    expect(sourceLine()).toBe("合同.docx · 第 3 段");
  });

  it("shows the page when there is one, including old cells without a chunk position", () => {
    renderPanel({ ...BASE, page: 4, chunkIndex: 2 });
    expect(sourceLine()).toBe("合同.docx · 第 4 页");
    cleanup();
    renderPanel({ ...BASE, page: 7 });
    expect(sourceLine()).toBe("合同.docx · 第 7 页");
  });

  it("shows neither when the quote was not located", () => {
    renderPanel({ ...BASE, chunkId: undefined });
    expect(sourceLine()).toBe("合同.docx");
  });

  it("uses the reader's language", () => {
    renderPanel({ ...BASE, chunkIndex: 3 }, messagesEn as typeof messagesZh, "en-US");
    expect(sourceLine()).toBe("合同.docx · Paragraph 3");
  });

  it("labels each source card the same way", () => {
    const variants = [variant({ chunk_index: 3 }), variant({ is_primary: false, extracted_by: "user_edit_superseded", page: 2, chunk_index: null })];
    renderPanel({ ...BASE, chunkIndex: 3, variants });
    expect(screen.getByText("tier1 · 第 3 段")).toBeTruthy();
    expect(screen.getByText("user_edit_superseded · 第 2 页")).toBeTruthy();
  });

  it("the hover preview names the paragraph too", () => {
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <CitePopover anchor={new DOMRect(0, 0, 10, 10)} fileId="kbf_1" fileName="合同.docx" cell={{ ...BASE, chunkIndex: 3 }} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId("kb-matrix-citepop").querySelector(".src")?.textContent).toBe("合同.docx · 第 3 段");
  });
});
