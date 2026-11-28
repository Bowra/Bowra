// Access control › Knowledge base grants (knowledge base organization slice 6): read-only, names instead of IDs,
// enforcement labelling, member mapping suggestions, empty and error states, pages.
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import type { GrantReport, GrantReportItem, GrantReportKnowledgeBase } from "@/lib/knowledge-grant-report-api";

const api = vi.hoisted(() => ({ fetchKnowledgeGrantReport: vi.fn() }));
vi.mock("@/lib/knowledge-grant-report-api", async (orig) => ({ ...(await orig<Record<string, unknown>>()), ...api }));
vi.mock("@/lib/use-error-diagnostics-capability", () => ({ useErrorDiagnosticsCapability: () => false }));

import { KnowledgeGrantReport } from "./knowledge-grant-report";

const SPACE_DEFAULT: GrantReportKnowledgeBase = {
  id: "kbase_space", is_default: true, home: "space", home_id: "ws_fin", name: null, home_name: "财务部", audience: "space", audience_level: "edit",
};
const CONTRACTS: GrantReportKnowledgeBase = {
  id: "kbase_contracts", is_default: false, home: "space", home_id: "ws_fin", name: "合同库", home_name: "财务部", audience: "members", audience_level: "use",
};

function item(over: Partial<GrantReportItem> & Pick<GrantReportItem, "grant_id">): GrantReportItem {
  return {
    effect: "allow",
    permission: "read",
    expires_at: null,
    expired: false,
    created_at: "2026-11-20T00:00:00+00:00",
    applies_to: { type: "user", id: "usr_li", label: "李娜", state: "active", space_name: null, member_count: null },
    target: { kind: "kb_scope", home: "space", space_name: "财务部", file_name: null, state: "active", knowledge_base: null },
    restriction: { kind: "allow_list", actions: ["read"], reason: null },
    mapping: {
      action: "needs_level", level: "use", knowledge_bases: [SPACE_DEFAULT, CONTRACTS], more: 0, whole_home: true, open_audience: true, reason: null,
    },
    ...over,
  };
}

function report(over: Partial<GrantReport> = {}): GrantReport {
  return {
    enforced: false,
    total: 4,
    next_cursor: null,
    items: [
      item({ grant_id: "grt_scope" }),
      item({
        grant_id: "grt_file",
        effect: "deny",
        permission: "write",
        applies_to: { type: "group", id: "grp_audit", label: "审计组", state: "active", space_name: null, member_count: 3 },
        target: { kind: "kb_file", home: "space", space_name: "财务部", file_name: "合同.pdf", state: "active", knowledge_base: CONTRACTS },
        restriction: { kind: "deny", actions: ["read", "write"], reason: null },
        mapping: { action: "keep_out", level: null, knowledge_bases: [CONTRACTS], more: 0, whole_home: false, open_audience: false, reason: null },
      }),
      item({
        grant_id: "grt_hidden",
        target: { kind: "kb_file", home: "space", space_name: "法务部", file_name: null, state: "hidden", knowledge_base: null },
        mapping: { action: "needs_level", level: "use", knowledge_bases: [], more: 0, whole_home: false, open_audience: false, reason: null },
      }),
      item({
        grant_id: "grt_gone",
        expires_at: "2020-01-01T00:00:00+00:00",
        expired: true,
        applies_to: { type: "group", id: "grp_old", label: "旧项目组", state: "group_removed", space_name: null, member_count: 0 },
        target: { kind: "kb_file", home: null, space_name: null, file_name: null, state: "not_found", knowledge_base: null },
        restriction: { kind: "none", actions: [], reason: "expired" },
        mapping: { action: "none", level: null, knowledge_bases: [], more: 0, whole_home: false, open_audience: false, reason: "expired" },
      }),
    ],
    ...over,
  };
}

const rows = () => screen.getAllByTestId("kb-grant-report-item");

beforeEach(() => {
  api.fetchKnowledgeGrantReport.mockResolvedValue(report());
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("知识库授权对照", () => {
  it("explains that grants only narrow, labels log-only enforcement and stays read-only", async () => {
    renderIntl(<KnowledgeGrantReport />);
    const section = await screen.findByTestId("kb-grant-report");
    await waitFor(() => expect(rows()).toHaveLength(4));
    expect(within(section).getByRole("heading", { name: "知识库授权对照" })).toBeInTheDocument();
    expect(section).toHaveTextContent("只会拦下访问，从不授予访问");
    expect(section).toHaveTextContent("只是建议，不会做任何改动");
    expect(screen.getByTestId("kb-grant-report-enforcement")).toHaveTextContent("仅记录日志");
    expect(screen.getByTestId("kb-grant-report-enforcement")).toHaveTextContent("BOWHEAD_RESOURCE_GRANTS_ENFORCE 不是 1");
    expect(within(section).queryAllByRole("button")).toHaveLength(0);
    expect(within(section).queryAllByRole("textbox")).toHaveLength(0);
    // Names, not raw IDs.
    expect(section.textContent).not.toMatch(/usr_|grp_|ws_fin|kbase_/);
  });

  it("shows each grant's target, recipient, restriction and member mapping", async () => {
    renderIntl(<KnowledgeGrantReport />);
    await waitFor(() => expect(rows()).toHaveLength(4));
    const [scope, file, hidden, gone] = rows();
    expect(scope).toHaveTextContent("空间「财务部」的知识库文件");
    expect(scope).toHaveTextContent("允许名单：只有在这个对象上有「允许」授权的人能读取或写入，其他人都会被拦下。李娜可以读取。");
    expect(scope).toHaveTextContent("（目前只写入日志。）");
    expect(scope).toHaveTextContent(/李娜需要在「财务部」.*「合同库」上有「可使用」。/);
    expect(scope).toHaveTextContent("这条授权覆盖这里的所有知识库，包括你看不到的。");
    expect(scope).toHaveTextContent("需要改用「仅成员」知识库");

    expect(file).toHaveTextContent("文件「合同.pdf」");
    expect(file).toHaveTextContent("在知识库「合同库」中");
    expect(file).toHaveTextContent("组「审计组」 · 3 名成员");
    expect(file).toHaveTextContent("组「审计组」不能读取和写入这个对象。");
    expect(file).toHaveTextContent("要继续把组「审计组」挡在「合同库」之外");

    expect(hidden).toHaveTextContent("空间「法务部」中你无法打开的文件");
    expect(hidden).toHaveTextContent("李娜需要在这个文件所在的知识库上有「可使用」");

    expect(gone).toHaveTextContent("已不存在的知识库文件");
    expect(gone).toHaveTextContent("组「旧项目组」（已移除）");
    expect(gone).toHaveTextContent("已过期");
    expect(gone).toHaveTextContent("已于 2020-01-01 过期，不再起作用。");
    expect(gone).toHaveTextContent("已过期，不需要对应。");
  });

  it("labels enforcement when it is on", async () => {
    api.fetchKnowledgeGrantReport.mockResolvedValue(report({ enforced: true }));
    renderIntl(<KnowledgeGrantReport />, { locale: "en-US" });
    const enforcement = await screen.findByTestId("kb-grant-report-enforcement");
    expect(enforcement).toHaveTextContent("Enforced");
    expect(enforcement).toHaveTextContent("the restrictions below block access now");
    expect(screen.getByTestId("kb-grant-report")).not.toHaveTextContent("Today this is only logged.");
    expect(rows()[0]).toHaveTextContent(/李娜 would need Can use on “财务部” and “合同库”\./);
  });

  it("has an empty state", async () => {
    api.fetchKnowledgeGrantReport.mockResolvedValue({ enforced: false, items: [], total: 0, next_cursor: null });
    renderIntl(<KnowledgeGrantReport />);
    expect(await screen.findByText("团队里没有作用于知识库范围或知识库文件的授权，不需要对照。")).toBeInTheDocument();
    expect(screen.queryAllByTestId("kb-grant-report-item")).toHaveLength(0);
  });

  it("shows an actionable error and retries", async () => {
    api.fetchKnowledgeGrantReport.mockRejectedValueOnce(new ApiError(500, "Internal Server Error", { code: "internal_error" }));
    renderIntl(<KnowledgeGrantReport />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("知识库授权对照没有加载出来。请点「重试」；如果一直失败，请把诊断信息发给部署管理员。");
    expect(alert).not.toHaveTextContent(/500|internal/i);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(rows()).toHaveLength(4));
    expect(api.fetchKnowledgeGrantReport).toHaveBeenCalledTimes(2);
  });

  it("explains a role refusal", async () => {
    api.fetchKnowledgeGrantReport.mockRejectedValue(new ApiError(403, "Forbidden"));
    renderIntl(<KnowledgeGrantReport />);
    expect(await screen.findByRole("alert")).toHaveTextContent("只有团队负责人和团队管理员可以查看知识库授权对照");
  });

  it("loads further pages with the cursor", async () => {
    const first = report({ total: 5, next_cursor: "cur_1" });
    api.fetchKnowledgeGrantReport
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce({ enforced: false, total: 5, next_cursor: null, items: [item({ grant_id: "grt_last" })] });
    renderIntl(<KnowledgeGrantReport />);
    await waitFor(() => expect(rows()).toHaveLength(4));
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    await waitFor(() => expect(rows()).toHaveLength(5));
    expect(api.fetchKnowledgeGrantReport).toHaveBeenLastCalledWith({ cursor: "cur_1" });
    expect(screen.queryByRole("button", { name: "加载更多" })).toBeNull();
  });
});
