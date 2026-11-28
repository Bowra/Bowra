// Knowledge bases (FEATURE_KNOWLEDGE_BASES): the directory on the Knowledge page (PRD §4.1 ①, §4.12 ⑪).
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import type { KnowledgeBaseDirectory } from "@/lib/knowledge-bases-api";

import { SPACES, directoryFixture, kbFileFixture } from "./kb-directory.fixtures";

const mocks = vi.hoisted(() => ({
  dir: { directory: null as KnowledgeBaseDirectory | null, loading: false, error: null as Error | null, refresh: vi.fn() },
  phone: false,
  push: vi.fn(),
  fetchKbFiles: vi.fn(),
  searchKb: vi.fn(),
}));

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: SPACES, activeId: "ws_1", loading: false, error: null, refresh: vi.fn() }),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ status: "authenticated", me: { id: "u_1", team_id: "team_1", capabilities: { knowledge_bases: true } } }),
}));
vi.mock("./use-knowledge-bases", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./use-knowledge-bases")>();
  return { ...actual, useKnowledgeBaseDirectory: () => mocks.dir };
});
vi.mock("@/components/ui/list-rows", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/ui/list-rows")>();
  return { ...actual, useListRowsLayout: () => mocks.phone };
});
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: mocks.push, replace: vi.fn() }) }));
vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return {
    ...actual,
    fetchKbFiles: (...args: unknown[]) => mocks.fetchKbFiles(...args),
    searchKb: (...args: unknown[]) => mocks.searchKb(...args),
  };
});
vi.mock("./kb-share-dialog", () => ({
  KbShareDialog: ({ kb, onClose }: { kb: { id: string }; onClose: () => void }) => (
    <div role="dialog" aria-label="share-stub" data-kb={kb.id}>
      <button type="button" onClick={onClose}>close-share</button>
    </div>
  ),
}));
vi.mock("./kb-delete-dialog", () => ({
  KbDeleteDialog: ({ kb }: { kb: { id: string } }) => <div role="dialog" aria-label="delete-stub" data-kb={kb.id} />,
}));

import { KbDirectory } from "./kb-directory";

beforeEach(() => {
  mocks.dir = { directory: directoryFixture(), loading: false, error: null, refresh: vi.fn() };
  mocks.phone = false;
  mocks.push.mockReset();
  mocks.fetchKbFiles.mockReset();
  mocks.searchKb.mockReset();
  window.localStorage.clear();
});
afterEach(cleanup);

function section(home: "personal" | "space" | "team") {
  return screen.getByTestId(`kb-directory-section-${home}`);
}

describe("KbDirectory · sections and rows", () => {
  it("groups My, the current Space (titled with its name) and the Team, with their one-line explanations", () => {
    renderIntl(<KbDirectory />);
    expect(within(section("personal")).getByRole("heading", { name: "我的" })).toBeInTheDocument();
    expect(section("personal")).toHaveTextContent("只有你能看到，在你所有的空间里都能用");
    expect(within(section("space")).getByRole("heading", { name: "商超零售场景" })).toBeInTheDocument();
    expect(section("space")).toHaveTextContent("当前空间 · 只在这个空间的对话、智能体和文档矩阵里使用");
    expect(within(section("team")).getByRole("heading", { name: "团队" })).toBeInTheDocument();
    expect(section("team")).toHaveTextContent("团队里的所有空间都能使用");
    expect(screen.getByTestId("kb-directory-total")).toHaveTextContent("共 8 个知识库 · 126 个文件");
  });

  it("shows each row's name (defaults named from their home), Default badge, description, audience, level, files and failures", () => {
    renderIntl(<KbDirectory />);
    const myDefault = screen.getByTestId("kb-directory-row-kb_my");
    expect(within(myDefault).getByRole("link", { name: "我的知识" })).toHaveAttribute("href", "/knowledge/base/?id=kb_my");
    expect(myDefault).toHaveTextContent("默认");
    expect(myDefault).toHaveTextContent("你上传的个人资料");
    expect(myDefault).toHaveTextContent("仅自己");
    // The owner sees the accent Owner badge.
    expect(within(myDefault).getByText("所有者")).toHaveClass("ui-badge--accent");

    const spaceDefault = screen.getByTestId("kb-directory-row-kb_space");
    expect(within(spaceDefault).getByRole("link", { name: "商超零售场景" })).toBeInTheDocument();
    expect(spaceDefault).toHaveTextContent("空间成员可编辑");
    expect(within(spaceDefault).getByText("可编辑")).not.toHaveClass("ui-badge");
    expect(within(spaceDefault).getByText("1 个失败")).toHaveClass("ui-badge--danger");
    expect(spaceDefault).toHaveTextContent("38");

    const contracts = screen.getByTestId("kb-directory-row-kb_contracts");
    expect(contracts).toHaveTextContent("仅成员");
    expect(contracts).not.toHaveTextContent("默认");
    const teamDefault = screen.getByTestId("kb-directory-row-kb_team");
    expect(within(teamDefault).getByRole("link", { name: "团队知识库" })).toBeInTheDocument();
    expect(teamDefault).toHaveTextContent("团队所有人可使用");
    expect(teamDefault).toHaveTextContent("可使用");
  });

  it("opens a knowledge base from its name with in-app navigation", () => {
    renderIntl(<KbDirectory />);
    fireEvent.click(screen.getByRole("link", { name: "供应商合同" }));
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/base/?id=kb_contracts");
  });

  it("offers Share, Settings and Delete by permission; My knowledge bases are never shared, defaults never deleted", async () => {
    renderIntl(<KbDirectory />);
    const names = async (label: string) => {
      const menu = await openRowMenu(label);
      const items = menuItemNames(menu);
      fireEvent.keyDown(menu, { key: "Escape" });
      return items;
    };
    expect(await names("我的知识的更多操作")).toEqual(["设置"]);
    expect(await names("竞品资料的更多操作")).toEqual(["设置", "删除知识库…"]);
    // Use or edit only: the read-only "Who has access".
    expect(await names("商超零售场景的更多操作")).toEqual(["谁能访问"]);
    expect(await names("供应商合同的更多操作")).toEqual(["谁能访问"]);
    expect(await names("门店运营手册的更多操作")).toEqual(["分享", "设置", "删除知识库…"]);
    expect(await names("团队知识库的更多操作")).toEqual(["谁能访问"]);
  });

  it("Share opens the share dialog, Settings goes to the settings tab, Delete opens the confirmation", async () => {
    renderIntl(<KbDirectory />);
    fireEvent.click(within(await openRowMenu("门店运营手册的更多操作")).getByRole("menuitem", { name: "分享" }));
    expect(screen.getByRole("dialog", { name: "share-stub" })).toHaveAttribute("data-kb", "kb_ops");
    fireEvent.click(screen.getByRole("button", { name: "close-share" }));

    const settings = within(await openRowMenu("门店运营手册的更多操作")).getByRole("menuitem", { name: "设置" });
    expect(settings).toHaveAttribute("href", "/knowledge/base/?id=kb_ops&tab=settings");
    fireEvent.click(settings);
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/base/?id=kb_ops&tab=settings");

    fireEvent.click(within(await openRowMenu("竞品资料的更多操作")).getByRole("menuitem", { name: "删除知识库…" }));
    expect(screen.getByRole("dialog", { name: "delete-stub" })).toHaveAttribute("data-kb", "kb_comp");
  });

  it("says when a section has nothing the viewer can use", () => {
    mocks.dir.directory = directoryFixture({ sections: { ...directoryFixture().sections, team: [] } });
    renderIntl(<KbDirectory />);
    expect(section("team")).toHaveTextContent("团队里还没有你能使用的知识库。");
  });

  it("shows a loading skeleton, then an actionable load error with Try again", () => {
    mocks.dir = { directory: null, loading: true, error: null, refresh: vi.fn() };
    const { unmount } = renderIntl(<KbDirectory />);
    expect(screen.getByRole("status", { name: "正在加载知识库…" })).toBeInTheDocument();
    expect(screen.queryByTestId("kb-directory-section-personal")).not.toBeInTheDocument();
    unmount();

    const refresh = vi.fn();
    mocks.dir = { directory: null, loading: false, error: new Error("boom"), refresh };
    renderIntl(<KbDirectory />);
    expect(screen.getByTestId("kb-directory-error")).toHaveTextContent("知识库列表没有加载出来。请点「重试」");
    expect(screen.getByTestId("kb-directory-error")).not.toHaveTextContent("boom");
    fireEvent.click(screen.getByTestId("kb-directory-retry"));
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});

describe("KbDirectory · search", () => {
  it("filters knowledge bases by name or description and lists matching files with their knowledge base", async () => {
    mocks.fetchKbFiles.mockResolvedValue({
      files: [
        kbFileFixture({ file_id: "f_named", filename: "采购合同 2026.pdf", knowledge_base_id: "kb_contracts" } as never),
        kbFileFixture({ file_id: "f_default", filename: "合同模板.docx", knowledge_base_id: null } as never),
      ],
      total: 2,
      has_more: false,
    });
    renderIntl(<KbDirectory />);
    const input = screen.getByRole("searchbox", { name: "搜索知识库或文件名" });
    fireEvent.change(input, { target: { value: "合同" } });
    // Knowledge bases filter as you type (name or description); sections without a match disappear.
    expect(screen.getByTestId("kb-directory-row-kb_contracts")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-directory-row-kb_ops")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-directory-section-personal")).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: "陈列" } });
    expect(screen.getByTestId("kb-directory-row-kb_ops")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "合同" } });

    const files = await screen.findByTestId("kb-directory-files");
    expect(mocks.fetchKbFiles).toHaveBeenLastCalledWith({ scope: "all", q: "合同", limit: 20, order_by: "uploaded_at", order_dir: "desc" });
    const named = await within(files).findByTestId("kb-directory-file-f_named");
    expect(within(named).getByTestId("kb-directory-file-kb")).toHaveTextContent("供应商合同");
    // A file without a named knowledge base is in its scope's default.
    expect(within(within(files).getByTestId("kb-directory-file-f_default")).getByTestId("kb-directory-file-kb")).toHaveTextContent("商超零售场景");
    fireEvent.click(within(named).getByRole("link", { name: "在知识库中打开「采购合同 2026.pdf」" }));
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/base/?id=kb_contracts&file_id=f_named");
  });

  it("says when neither knowledge bases nor files match", async () => {
    mocks.fetchKbFiles.mockResolvedValue({ files: [], total: 0, has_more: false });
    renderIntl(<KbDirectory />);
    const input = screen.getByRole("searchbox", { name: "搜索知识库或文件名" });
    fireEvent.change(input, { target: { value: "zzz" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByTestId("kb-directory-no-match")).toHaveTextContent("没有名称或说明包含「zzz」的知识库。");
    expect(await screen.findByTestId("kb-directory-files-empty")).toHaveTextContent("没有文件名包含「zzz」的文件");
  });

  it("deep search covers every knowledge base the viewer can use and opens the hit in its knowledge base", async () => {
    mocks.searchKb.mockResolvedValue({
      chunks: [
        { chunk_id: "c_1", content: "冷链商品的保质期需要每日核对。", file_id: "f_1", file_name: "生鲜陈列标准.docx", page: 3, section_title: null, role: "body", score: 0.9, scope: "space" },
        { chunk_id: "c_2", content: "保质期第二处", file_id: "f_1", file_name: "生鲜陈列标准.docx", page: 4, section_title: null, role: "body", score: 0.8, scope: "space" },
        { chunk_id: "c_3", content: "差旅报销的保质期说明", file_id: "f_2", file_name: "差旅标准.pdf", page: null, section_title: "附录", role: "body", score: 0.5, scope: "team" },
      ],
      total_candidates: 3,
      rerank_used: false,
      latency_ms: 10,
      partial: true,
    });
    mocks.fetchKbFiles.mockResolvedValue({
      files: [
        kbFileFixture({ file_id: "f_1", filename: "生鲜陈列标准.docx", knowledge_base_id: "kb_ops" } as never),
        kbFileFixture({ file_id: "f_2", filename: "差旅标准.pdf", scope: "team", scope_id: "team_1", knowledge_base_id: "kb_policy" } as never),
      ],
      total: 2,
    });
    renderIntl(<KbDirectory />);
    fireEvent.click(screen.getByTestId("kb-directory-deep"));
    expect(screen.getByTestId("kb-directory-deep")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("kb-deep-hint")).toBeInTheDocument();
    const input = screen.getByRole("searchbox", { name: "搜索知识库或文件名" });
    fireEvent.change(input, { target: { value: "保质期" } });
    expect(mocks.searchKb).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });

    const results = await screen.findByTestId("kb-directory-deep-results");
    await within(results).findByTestId("kb-directory-file-f_1");
    expect(mocks.searchKb).toHaveBeenCalledWith("保质期", { scope_filter: ["personal", "space", "team"] });
    expect(mocks.fetchKbFiles).toHaveBeenCalledWith({ scope: "all", file_ids: ["f_1", "f_2"], limit: 100 });
    // The directory sections give way to the results.
    expect(screen.queryByTestId("kb-directory-section-personal")).not.toBeInTheDocument();
    expect(results).toHaveTextContent("找到 2 个文件");
    expect(results).toHaveTextContent("部分知识库暂时无法搜索");
    const first = within(results).getByTestId("kb-directory-file-f_1");
    expect(within(first).getByTestId("kb-directory-file-kb")).toHaveTextContent("门店运营手册");
    expect(within(first).getByTestId("kb-directory-hit")).toHaveTextContent("第 3 页");
    expect(within(first).getByTestId("kb-directory-hit").querySelector("mark")).toHaveTextContent("保质期");
    const second = within(results).getByTestId("kb-directory-file-f_2");
    expect(within(second).getByTestId("kb-directory-file-kb")).toHaveTextContent("公司制度");
    expect(within(second).getByTestId("kb-directory-hit")).toHaveTextContent("章节「附录」");

    fireEvent.click(within(first).getByRole("link", { name: "在知识库中打开「生鲜陈列标准.docx」" }));
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/base/?id=kb_ops&file_id=f_1&chunk_id=c_1");
  });

  it("deep search with no hit says so and offers searching by name", async () => {
    mocks.searchKb.mockResolvedValue({ chunks: [], total_candidates: 0, rerank_used: false, latency_ms: 1 });
    renderIntl(<KbDirectory />);
    const input = screen.getByRole("searchbox", { name: "搜索知识库或文件名" });
    fireEvent.change(input, { target: { value: "火星" } });
    fireEvent.click(screen.getByTestId("kb-directory-deep"));
    expect(await screen.findByTestId("kb-directory-deep-empty")).toHaveTextContent("没有文件内容匹配「火星」");
    expect(mocks.fetchKbFiles).not.toHaveBeenCalled();
  });
});

describe("KbDirectory · phone", () => {
  it("renders list rows (name, description, audience · files · updated, level, ⋯) instead of tables", async () => {
    mocks.phone = true;
    renderIntl(<KbDirectory />);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    const spaceList = within(section("space")).getByRole("list", { name: "商超零售场景" });
    const rows = within(spaceList).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("商超零售场景默认");
    expect(rows[0]).toHaveTextContent("空间成员共用的资料");
    expect(rows[0]).toHaveTextContent("空间成员可编辑 · 38 个文件 · 1 个失败");
    expect(rows[0]).toHaveTextContent("可编辑");
    expect(within(rows[1]).getByText("所有者")).toHaveClass("ui-badge--accent");
    fireEvent.click(within(rows[2]).getByRole("button", { name: "供应商合同" }));
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/base/?id=kb_contracts");
    expect(menuItemNames(await openRowMenu("门店运营手册的更多操作"))).toEqual(["分享", "设置", "删除知识库…"]);
  });

  it("lists file name matches as rows too", async () => {
    mocks.phone = true;
    mocks.fetchKbFiles.mockResolvedValue({
      files: [kbFileFixture({ file_id: "f_named", filename: "采购合同 2026.pdf", knowledge_base_id: "kb_contracts" } as never)],
      total: 1,
    });
    renderIntl(<KbDirectory />);
    const input = screen.getByRole("searchbox", { name: "搜索知识库或文件名" });
    fireEvent.change(input, { target: { value: "合同" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const files = await screen.findByTestId("kb-directory-files");
    await waitFor(() => expect(within(files).getByRole("list")).toBeInTheDocument());
    const row = within(files).getByRole("listitem");
    expect(row).toHaveTextContent("供应商合同");
    fireEvent.click(within(row).getByRole("button", { name: "在知识库中打开「采购合同 2026.pdf」" }));
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/base/?id=kb_contracts&file_id=f_named");
  });
});
