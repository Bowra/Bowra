import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FileDTO, FileKbStatus } from "@/lib/api";
import messagesZh from "@/messages/zh-CN.json";

import { FilesDialogs, FilesToasts } from "./file-dialogs";
import { KbColumn } from "./kb-column";

const readiness = vi.hoisted(() => ({ state: "ready" as "ready" | "missing" | "unavailable" | "checking" }));
vi.mock("@/lib/kb-ingestion-readiness", () => ({
  useKbIngestionReadiness: () => ({ state: readiness.state, retry: vi.fn() }),
}));

afterEach(() => { readiness.state = "ready"; cleanup(); });

function file(over: Partial<FileDTO> = {}): FileDTO {
  return {
    file_id: "art_1",
    filename: "供应商合同-华东区.pdf",
    mime: "application/pdf",
    mime_family: "pdf",
    size: 1024,
    source: "upload",
    origin: "upload",
    kind: "user_upload",
    trust: "trusted",
    created_at: "2026-09-20T10:00:00Z",
    last_used_at: null,
    deleted_at: null,
    category: null,
    category_source: null,
    usage_count: 0,
    in_kb: false,
    kb: null,
    kb_promotable: true,
    session_id: null,
    session_title: null,
    has_canvas_ref: false,
    retention_hint: null,
    url: "/api/v1/files/art_1",
    ...over,
  };
}

function kb(over: Partial<FileKbStatus> = {}): FileKbStatus {
  return {
    file_id: "kbf_1",
    scope: "space",
    parse_status: "done",
    parse_progress_pct: null,
    parse_stuck: false,
    parse_error_code: null,
    ...over,
  };
}

function renderZh(node: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      {node}
    </NextIntlClientProvider>,
  );
}

describe("KbColumn（文件页「知识库」列）", () => {
  it("可用：绿色徽章 + 「在知识库中查看」深链到对应范围与抽屉", () => {
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb() })} onAdd={vi.fn()} />);
    const badge = screen.getByText("已加入 · 可用");
    expect(badge).toHaveClass("kbbadge", "avail");
    expect(screen.getByRole("link", { name: "在知识库中查看" })).toHaveAttribute(
      "href",
      "/knowledge/?scope=space&file_id=kbf_1",
    );
  });

  it("空间文件的深链带上所属空间，知识库页会切到那个空间", () => {
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ workspace_id: "ws_retail" }) })} onAdd={vi.fn()} />);
    expect(screen.getByRole("link", { name: "在知识库中查看" })).toHaveAttribute(
      "href",
      "/knowledge/?scope=space&space=ws_retail&file_id=kbf_1",
    );
  });

  it("处理中：蓝色徽章带进度 + 「查看处理进度」", () => {
    renderZh(
      <KbColumn
        file={file({ in_kb: true, kb: kb({ parse_status: "embedding", parse_progress_pct: 45, scope: "personal" }) })}
        onAdd={vi.fn()}
      />,
    );
    expect(screen.getByText("已加入 · 处理中 45%")).toHaveClass("kbbadge", "proc");
    expect(screen.getByRole("link", { name: "查看处理进度" })).toHaveAttribute(
      "href",
      "/knowledge/?scope=personal&file_id=kbf_1",
    );
  });

  it("卡住与失败同一映射：显示失败 + 「去知识库重试」，而不是停在处理中", () => {
    renderZh(
      <KbColumn file={file({ in_kb: true, kb: kb({ parse_status: "parsing", parse_stuck: true }) })} onAdd={vi.fn()} />,
    );
    expect(screen.getByText("已加入 · 失败")).toHaveClass("kbbadge", "failed");
    expect(screen.getByRole("link", { name: "去知识库重试" })).toBeInTheDocument();
    // Control: the same row without a blockage is processing
    cleanup();
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ parse_status: "parsing" }) })} onAdd={vi.fn()} />);
    expect(screen.getByText("已加入 · 处理中")).toHaveClass("proc");
  });

  it("建议加入：AI 成品显示建议徽章，「加入知识库」打开弹窗", () => {
    const onAdd = vi.fn();
    renderZh(<KbColumn file={file({ kind: "deliverable" })} onAdd={onAdd} />);
    expect(screen.getByText("建议加入知识库")).toHaveClass("kbbadge", "sugg");
    fireEvent.click(screen.getByRole("button", { name: "加入知识库" }));
    expect(onAdd).toHaveBeenCalledTimes(1);
  });

  it("未加入：可加入的给入口，不可加入的只写未加入", () => {
    renderZh(<KbColumn file={file()} onAdd={vi.fn()} />);
    expect(screen.getByText("未加入")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "加入知识库" })).toBeInTheDocument();
    cleanup();
    renderZh(<KbColumn file={file({ kb_promotable: false })} onAdd={vi.fn()} />);
    expect(screen.getByText("未加入")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "加入知识库" })).toBeNull();
  });

  it("在知识库里但不在你可见的范围：只写已加入，不给状态与跳转", () => {
    renderZh(<KbColumn file={file({ in_kb: true, kb: null })} onAdd={vi.fn()} />);
    expect(screen.getByText("已加入")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByText("未加入")).toBeNull();
  });
});

describe("加入知识库弹窗", () => {
  const dlg = { kind: "kb-add" as const, name: "供应商合同-华东区.pdf", onConfirm: vi.fn() };

  it("explains a missing embedding model and blocks promotion before confirmation", () => {
    readiness.state = "missing";
    const onConfirm = vi.fn();
    renderZh(<FilesDialogs dlg={{ ...dlg, onConfirm }} onClose={vi.fn()} canPromoteToTeam spaceName="商超零售场景" />);
    expect(screen.getByTestId("kb-ingestion-readiness")).toHaveTextContent("团队尚未配置并启用嵌入模型");
    expect(screen.getByRole("link", { name: "配置嵌入模型" })).toHaveAttribute("href", "/team/settings/models/");
    expect(screen.getByTestId("kb-add-confirm")).toBeDisabled();
    fireEvent.click(screen.getByTestId("kb-add-confirm"));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("副标题写出文件名，空间卡片写出空间名", () => {
    renderZh(<FilesDialogs dlg={dlg} onClose={vi.fn()} canPromoteToTeam={false} spaceName="商超零售场景" />);
    expect(screen.getByText(/^供应商合同-华东区\.pdf · 加入后/)).toBeInTheDocument();
    expect(screen.getByTestId("kb-scope-space")).toHaveTextContent("空间 · 商超零售场景");
    // Non-admin: Team card is locked
    expect(screen.getByTestId("kb-scope-team")).toBeDisabled();
    expect(screen.getByText("仅团队管理员")).toBeInTheDocument();
  });

  it("身份加载中：团队卡片是占位，不显示锁定，提交禁用", () => {
    renderZh(<FilesDialogs dlg={dlg} onClose={vi.fn()} canPromoteToTeam={null} spaceName="商超零售场景" />);
    expect(screen.getByTestId("kb-scope-team-loading")).toBeInTheDocument();
    expect(screen.queryByText("仅团队管理员")).toBeNull();
    expect(screen.getByTestId("kb-add-confirm")).toBeDisabled();
  });

  it("团队管理员：团队可选，确认带回所选范围", () => {
    const onConfirm = vi.fn();
    renderZh(
      <FilesDialogs dlg={{ ...dlg, onConfirm }} onClose={vi.fn()} canPromoteToTeam spaceName="商超零售场景" />,
    );
    expect(screen.queryByText("仅团队管理员")).toBeNull();
    fireEvent.click(screen.getByTestId("kb-scope-team"));
    fireEvent.click(screen.getByTestId("kb-add-confirm"));
    expect(onConfirm).toHaveBeenCalledWith("team");
  });

  it("没有空间：空间卡片禁用并说明原因", () => {
    renderZh(<FilesDialogs dlg={dlg} onClose={vi.fn()} canPromoteToTeam={false} spaceName={null} />);
    expect(screen.getByTestId("kb-scope-space")).toBeDisabled();
    expect(screen.getByText("你还没有加入任何空间。")).toBeInTheDocument();
  });
});

describe("删除确认弹窗", () => {
  it("打开时焦点落在取消上，Esc 关闭", () => {
    const onClose = vi.fn();
    const onConfirm = vi.fn();
    renderZh(
      <FilesDialogs
        dlg={{ kind: "del", name: "供应商合同-华东区.pdf", onConfirm }}
        onClose={onClose}
        canPromoteToTeam={false}
        spaceName={null}
      />,
    );
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "取消" }));
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});

describe("加入后的提示", () => {
  it("「查看处理进度」是指向知识库抽屉的链接", () => {
    renderZh(
      <FilesToasts
        list={[{
          id: "t1",
          text: "已加入知识库 · 供应商合同-华东区.pdf",
          action: { label: "查看处理进度", href: "/knowledge/?scope=personal&file_id=kbf_9" },
        }]}
      />,
    );
    expect(screen.getByText("已加入知识库 · 供应商合同-华东区.pdf")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看处理进度" })).toHaveAttribute(
      "href",
      "/knowledge/?scope=personal&file_id=kbf_9",
    );
  });
});
