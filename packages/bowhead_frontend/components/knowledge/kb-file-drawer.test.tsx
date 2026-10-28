/**
 * File-details drawer — knowledge-base UX cleanup PRD FR2.3–2.6.
 *
 * Observed on 116: drawer showed kbf_ file ID, raw scope enum personal, parsing level L0; Esc failed to close;
 * disabled Preview explained itself only on hover; deletion confirmation promised “回收站 90 天” without any UI recovery entry.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { KbFileDTO } from "@/lib/kb-api";
import messagesZh from "@/messages/zh-CN.json";

vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return {
    ...actual,
    fetchKbFileChunks: vi.fn().mockResolvedValue({ chunks: [] }),
    downloadKbFileBlob: vi.fn().mockResolvedValue(undefined),
  };
});

// The browser build maps Mammoth's unzip to an ArrayBuffer reader; Node resolves the source entry,
// which rejects { arrayBuffer } internally and leaves an unhandled rejection behind. The DOCX case
// below only needs a conversion failure, so fail the conversion directly.
vi.mock("mammoth", () => ({
  convertToHtml: () => Promise.reject(new Error("not a DOCX archive")),
}));

import { KbFileDrawer } from "./kb-file-drawer";

function file(overrides: Partial<KbFileDTO> = {}): KbFileDTO {
  return {
    file_id: "kbf_adf0788de802",
    artifact_id: "art_1",
    filename: "deck-template.pptx",
    mime_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    size_bytes: 29_184,
    scope: "personal",
    scope_id: "usr_me",
    workspace_id: "",
    team_id: "team_1",
    uploaded_by: "usr_e9b84046e672",
    uploaded_at: "2026-08-08T00:18:56Z",
    parse_status: "queued",
    parse_progress_pct: null,
    parse_layer: "L0",
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
    can_delete: true,
    ...overrides,
  } as KbFileDTO;
}

function renderDrawer(f: KbFileDTO, onClose = vi.fn(), onRetry?: () => void) {
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <KbFileDrawer file={f} onClose={onClose} onDeleted={() => undefined} onRetry={onRetry} />
    </NextIntlClientProvider>,
  );
  return onClose;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("KbFileDrawer", () => {
  it("不显示内部 ID、原始范围枚举与解析层级", () => {
    renderDrawer(file());
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("kbf_adf0788de802");
    expect(text).not.toContain("usr_e9b84046e672");
    expect(text).not.toContain("personal");
    expect(text).not.toContain("L0");
    expect(screen.getByText(messagesZh.knowledge_base.drawer.scope_personal_desc)).toBeInTheDocument();
  });

  it("有上传者姓名时显示姓名", () => {
    renderDrawer(file({ uploaded_by_name: "liuyixin" }));
    expect(screen.getByText("liuyixin")).toBeInTheDocument();
  });

  it("focuses the panel, not the close button, when opened with a pointer; keyboard opens still start on the close button", async () => {
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbFileDrawer file={file()} onClose={vi.fn()} openedWithPointer />
      </NextIntlClientProvider>,
    );
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId("kb-file-drawer")));
    cleanup();
    renderDrawer(file());
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByRole("button", { name: messagesZh.knowledge_base.drawer.close_aria }),
    ));
  });

  it("按 Esc 关闭", () => {
    const onClose = renderDrawer(file());
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("父组件换了 onClose 后 Esc 调用最新的那个", () => {
    const first = vi.fn();
    const second = vi.fn();
    const f = file();
    const { rerender } = render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbFileDrawer file={f} onClose={first} onDeleted={() => undefined} />
      </NextIntlClientProvider>,
    );
    rerender(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbFileDrawer file={f} onClose={second} onDeleted={() => undefined} />
      </NextIntlClientProvider>,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
  });

  it("处理中时直接写出预览不可用的原因", () => {
    renderDrawer(file());
    expect(screen.getByTestId("kb-preview-button")).toBeDisabled();
    expect(screen.getByText(messagesZh.knowledge_base.drawer.preview_disabled_until_done)).toBeInTheDocument();
  });

  it("失败时写明原因并可重试（FR1.5 / FR2.4）", () => {
    const onRetry = vi.fn();
    renderDrawer(file({ parse_status: "failed", parse_error: "timeout: RAW", parse_error_code: "timeout" }), vi.fn(), onRetry);
    expect(screen.getByText(messagesZh.knowledge_base.file_state.failed)).toBeInTheDocument();
    expect(screen.getByText(messagesZh.knowledge_base.fail_reason.timeout)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("RAW");
    fireEvent.click(screen.getByTestId("kb-drawer-retry"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("原文件已丢失：写明原因，不给重试也不给下载原文件；对照：超时仍可下载原文件", () => {
    renderDrawer(file({ parse_status: "failed", parse_error_code: "source_missing" }), vi.fn(), undefined);
    const zh = messagesZh.knowledge_base;
    expect(screen.getByText(zh.drawer.fail_title.source_missing)).toBeInTheDocument();
    expect(screen.getByText(zh.fail_reason.source_missing)).toBeInTheDocument();
    expect(screen.queryByTestId("kb-drawer-retry")).not.toBeInTheDocument();
    expect(screen.queryByText(zh.drawer.download_original)).not.toBeInTheDocument();
    cleanup();

    renderDrawer(file({ parse_status: "failed", parse_error_code: "timeout" }), vi.fn(), vi.fn());
    expect(screen.getByText(zh.drawer.download_original)).toBeInTheDocument();
  });

  it("卡住的文件显示失败与中断原因，而不是永远排队", () => {
    renderDrawer(file({ parse_status: "pending", parse_stuck: true }), vi.fn(), vi.fn());
    expect(screen.getByText(messagesZh.knowledge_base.file_state.failed)).toBeInTheDocument();
    expect(screen.getByText(messagesZh.knowledge_base.fail_reason.interrupted)).toBeInTheDocument();
    expect(screen.getByTestId("kb-drawer-retry")).toBeInTheDocument();
  });

  it("引用次数只在被引用过时显示", () => {
    renderDrawer(file({ parse_status: "done", cite_count: 3 }));
    expect(screen.getByTestId("kb-drawer-cited")).toHaveTextContent("对话中被引用 3 次");
    cleanup();
    renderDrawer(file({ parse_status: "done", cite_count: 0 }));
    expect(screen.queryByTestId("kb-drawer-cited")).not.toBeInTheDocument();
  });

  it("可用时主按钮「在对话中使用」调用传入的共享动作；处理中不出现", () => {
    const onUseInChat = vi.fn();
    const { unmount } = render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbFileDrawer file={file({ parse_status: "done" })} onClose={vi.fn()} onDeleted={() => undefined} onUseInChat={onUseInChat} />
      </NextIntlClientProvider>,
    );
    fireEvent.click(screen.getByTestId("kb-drawer-use-in-chat"));
    expect(onUseInChat).toHaveBeenCalledTimes(1);
    unmount();
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbFileDrawer file={file({ parse_status: "parsing" })} onClose={vi.fn()} onDeleted={() => undefined} onUseInChat={onUseInChat} />
      </NextIntlClientProvider>,
    );
    expect(screen.queryByTestId("kb-drawer-use-in-chat")).not.toBeInTheDocument();
  });

  it("删除确认不承诺界面上不存在的回收站", () => {
    renderDrawer(file({ parse_status: "done" }));
    fireEvent.click(screen.getByTestId("kb-delete-button"));
    expect(screen.getByTestId("kb-delete-confirm")).not.toHaveTextContent("回收站");
  });
});

describe("KbFileDrawer · 从回答引用打开", () => {
  const chunks = [
    { chunk_id: "chunk_1", content: "第一段正文", page: null, section_title: null, role: "intro", chunk_index: 1 },
    { chunk_id: "chunk_3", content: "定价与渠道正文", page: null, section_title: null, role: "body", chunk_index: 3 },
  ];
  const hit = (chunk_id: string) => ({
    chunk_id, content: "", file_id: "kbf_adf0788de802", file_name: "x.md",
    page: null, section_title: null, role: "", score: 0, scope: "space",
  });

  async function renderCited(chunkId: string) {
    const api = await import("@/lib/kb-api");
    vi.mocked(api.fetchKbFileChunks).mockResolvedValueOnce({ chunks, total: chunks.length });
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbFileDrawer
          file={file({ parse_status: "done", chunks_count: 2 })}
          onClose={() => undefined}
          onDeleted={() => undefined}
          hitChunk={hit(chunkId)}
          hitSource="citation"
        />
      </NextIntlClientProvider>,
    );
  }

  it("被引用的片段置顶，标为「回答引用」而不是「搜索命中」", async () => {
    await renderCited("chunk_3");
    const label = await screen.findByText(/回答引用/);
    expect(document.body.textContent).not.toContain("搜索命中");
    const first = label.closest("li, article, section, div")?.textContent ?? "";
    expect(first).toContain("定价与渠道正文");
  });

  it("被引用的片段已不在：不置顶空片段，也不出现引用标签", async () => {
    await renderCited("chunk_gone");
    await screen.findByText("第一段正文");
    expect(document.body.textContent).not.toContain("回答引用");
  });
});

describe("KbFileDrawer · 叠层的 Esc 与焦点", () => {
  const zh = messagesZh.knowledge_base;

  it("删除确认打开时 Esc 只收起确认，再按一次才关抽屉", () => {
    const onClose = renderDrawer(file({ parse_status: "done" }));
    fireEvent.click(screen.getByTestId("kb-delete-button"));
    expect(screen.getByTestId("kb-delete-confirm")).toBeInTheDocument();
    // Dispatch keys from a focused drawer element (real path): the shell stops bubbling, so document listeners alone are insufficient.
    const inside = within(screen.getByTestId("kb-delete-confirm")).getAllByRole("button")[0];
    inside.focus();
    fireEvent.keyDown(inside, { key: "Escape" });
    expect(screen.queryByTestId("kb-delete-confirm")).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByTestId("kb-delete-button"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("预览打开时 Esc 只关预览（焦点在预览里或不在都一样），抽屉保留", async () => {
    const onClose = renderDrawer(file({ parse_status: "done" }));
    fireEvent.click(screen.getByTestId("kb-preview-button"));
    const dialog = screen.getByRole("dialog", { name: "deck-template.pptx" });
    expect(dialog).toBe(screen.getByTestId("kb-preview-modal"));
    expect(dialog).toHaveAttribute("aria-modal", "true");
    // Preview is an independent dialog; focus moves to its close button.
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: zh.preview.close_aria })));
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    expect(screen.queryByTestId("kb-preview-modal")).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    // Focus outside preview (event reaches document): still close only preview.
    fireEvent.click(screen.getByTestId("kb-preview-button"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("kb-preview-modal")).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByTestId("kb-file-drawer")).toBeInTheDocument();
  });


  it("预览失败时的「下载查看」走带空间头的下载，而不是裸链接", async () => {
    // The original arrives but cannot be rendered, so the compact failure card offers the download.
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Uint8Array([1, 2, 3, 4]))));
    const api = await import("@/lib/kb-api");
    renderDrawer(file({
      parse_status: "done",
      filename: "contract.docx",
      mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }));
    fireEvent.click(screen.getByTestId("kb-preview-button"));
    const label = messagesZh.knowledge_base.preview.download_to_view;
    const btn = await screen.findByRole("button", { name: (name) => name.startsWith(label) }, { timeout: 5000 });
    expect(btn.tagName).toBe("BUTTON");
    fireEvent.click(btn);
    expect(vi.mocked(api.downloadKbFileBlob)).toHaveBeenCalledWith("kbf_adf0788de802", "contract.docx");
  });
});


describe("KbFileDrawer · 表格截断", () => {
  it("超出索引上限的表格在详情里写明只收录了前多少行", () => {
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbFileDrawer
          file={file({ parse_status: "done", chunks_count: 12, truncated_rows: { indexed: 5000, total: 12345 } })}
          onClose={() => undefined}
          onDeleted={() => undefined}
        />
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId("kb-truncated-rows").textContent).toBe("表格共 12,345 行（含表头），只有前 5,000 行进入了知识库，对话检索不到其余行。");
  });

  it("未截断不显示", () => {
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbFileDrawer file={file({ parse_status: "done", chunks_count: 3 })} onClose={() => undefined} onDeleted={() => undefined} />
      </NextIntlClientProvider>,
    );
    expect(screen.queryByTestId("kb-truncated-rows")).toBeNull();
  });
});
