import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { hireAgent, listAgentCatalog } from "@/lib/agents-api";
import { AgentMarketplace } from "./agent-marketplace";

const push = vi.fn();
const replace = vi.fn();
const appRouter = { push, replace };
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => appRouter,
}));
vi.mock("@/lib/agents-api", () => ({
  listAgentCatalog: vi.fn(),
  hireAgent: vi.fn(),
}));

const ITEM = {
  kind: "agent_catalog_item" as const,
  schema_version: 1 as const,
  agent_id: "osa_1",
  display_name: "Finance Partner",
  summary: "Explains monthly performance and highlights exceptions.",
  source: { kind: "team" as const, owner_label: "Avery", owner_user_id: null },
  facets: { domains: ["general"], jobs: ["analysis"], outputs: [] },
  capabilities: [{ id: "cap_data_query", label_key: "cap_data_query", label: "查询与分析数据", category: "data", verification_status: "derived_unverified" as const }],
  risk: { level: "low", reasons: [] },
  safety: { status: "pass", checked_subject: "legacy_row", reasons: [] },
  assignment: { status: "not_hired" as const, source: null, row_version: null },
  team_default: { enabled: false, policy_version: null, row_version: null },
  availability: { discoverable: true, hireable: true, runnable: false, reasons: ["not_hired"] },
  actions: {
    view_detail: true,
    hire: true,
    enable: false,
    disable: false,
    unhire: false,
    manage: false,
    manage_team_default: false,
    create: true,
  },
  action_reasons: {},
  relationship: "team",
  updated_label: { kind: "saved_at", at: "2026-08-30T00:00:00Z" },
  manager_url: null,
};

function result(
  items = [ITEM],
  nextCursor: string | null = null,
  options?: {
    totalCount?: number;
    featured?: Array<typeof ITEM>;
    facets?: Array<(typeof ITEM.capabilities)[number] & { count: number }>;
    domains?: Array<{ id: string; count: number }>;
    jobs?: Array<{ id: string; count: number }>;
    teamTags?: Array<{ id: string; label: string; count: number }>;
  },
) {
  return {
    kind: "agent_catalog_collection" as const,
    schema_version: 1 as const,
    items,
    collections: [{
      id: "recently_updated",
      label_key: "collection_recently_updated",
      agent_ids: (options?.featured ?? items).map((item) => item.agent_id),
      items: options?.featured ?? items,
    }],
    total_count: options?.totalCount ?? items.length,
    facets: {
      capabilities: options?.facets ?? ITEM.capabilities.map((entry) => ({ ...entry, count: items.length })),
      domains: options?.domains,
      jobs: options?.jobs,
      team_tags: options?.teamTags,
    },
    actions: { create: true },
    next_cursor: nextCursor,
  };
}

describe("AgentMarketplace", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/agents/marketplace/");
    vi.mocked(listAgentCatalog).mockResolvedValue(result());
    vi.mocked(hireAgent).mockResolvedValue({
      kind: "agent_assignment_mutation",
      schema_version: 1,
      assignment: { status: "active", source: "user_hired", row_version: 1 },
      runnable: true,
      public_reason: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders capability language without leaking raw tool refs", async () => {
    renderIntl(<AgentMarketplace />);

    expect((await screen.findAllByRole("heading", { name: "Finance Partner" })).length).toBeGreaterThan(0);
    expect(screen.getAllByText("查询与分析数据").length).toBeGreaterThan(0);
    expect(screen.getAllByText("待验证").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/创建者原文/).length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toContain("bi_query");
  });

  it("does not announce a false zero while the catalog is loading", () => {
    vi.mocked(listAgentCatalog).mockReturnValue(new Promise(() => {}));
    renderIntl(<AgentMarketplace />);

    expect(screen.getByText("正在读取可发现的智能体…")).toBeTruthy();
    expect(screen.queryByText("0 个可发现的智能体")).toBeNull();
  });

  it("aborts obsolete catalog reads immediately when filters change and on unmount", async () => {
    vi.mocked(listAgentCatalog).mockReturnValue(new Promise(() => {}));
    const rendered = renderIntl(<AgentMarketplace />);
    await waitFor(() => expect(listAgentCatalog).toHaveBeenCalledTimes(1));
    const previousSignal = vi.mocked(listAgentCatalog).mock.calls[0][0]!.signal!;
    fireEvent.change(screen.getByRole("textbox", { name: /^搜索智能体/ }), { target: { value: "finance" } });
    expect(previousSignal.aborted).toBe(true);
    await waitFor(() => expect(listAgentCatalog).toHaveBeenCalledTimes(2));
    const currentSignal = vi.mocked(listAgentCatalog).mock.calls[1][0]!.signal!;
    expect(currentSignal.aborted).toBe(false);
    rendered.unmount();
    expect(currentSignal.aborted).toBe(true);
  });

  it("keeps the current catalog visible while a new filter is loading", async () => {
    let finishFilter: ((value: ReturnType<typeof result>) => void) | undefined;
    vi.mocked(listAgentCatalog)
      .mockResolvedValueOnce(result())
      .mockReturnValueOnce(new Promise((resolve) => {
        finishFilter = resolve;
      }));
    renderIntl(<AgentMarketplace />);

    expect((await screen.findAllByRole("heading", { name: "Finance Partner" })).length).toBeGreaterThan(0);
    fireEvent.change(screen.getByRole("textbox", { name: /^搜索智能体/ }), { target: { value: "finance" } });
    await waitFor(() => expect(listAgentCatalog).toHaveBeenCalledTimes(2));

    expect(screen.getAllByRole("heading", { name: "Finance Partner" }).length).toBeGreaterThan(0);
    expect(screen.getByText("当前 1 个，正在更新…")).toBeTruthy();

    await act(async () => {
      finishFilter?.(result([]));
    });
    expect(await screen.findByRole("heading", { name: "没有匹配的智能体" })).toBeTruthy();
  });

  it("tells a member why a hired Agent cannot run, and adds nothing to cards that only need hiring", async () => {
    const blocked = {
      ...ITEM,
      agent_id: "osa_blocked",
      display_name: "Eddy",
      assignment: { status: "active" as const, source: "user_hired", row_version: 1 },
      availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "agent_needs_owner_fix" },
      actions: { ...ITEM.actions, hire: false },
    };
    vi.mocked(listAgentCatalog).mockResolvedValue(result([ITEM, blocked] as never));
    renderIntl(<AgentMarketplace />);

    expect(await screen.findByText("需要负责人处理")).toBeTruthy();
    expect(screen.getByText("请联系负责人 Avery 或团队管理员检查")).toBeTruthy();
    expect(screen.getAllByText("当前不可用")).toHaveLength(1);
    expect(screen.queryByText("需要聘用")).toBeNull();
  });

  it("tells a member who cannot hire an Agent yet that its owner has to settle it", async () => {
    const pending = {
      ...ITEM,
      agent_id: "osa_pending",
      display_name: "Eddy",
      assignment: { status: "not_hired" as const, source: null, row_version: null },
      availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "agent_needs_owner_fix" },
      actions: { ...ITEM.actions, hire: false },
    };
    vi.mocked(listAgentCatalog).mockResolvedValue(result([pending] as never));
    renderIntl(<AgentMarketplace />);

    expect(await screen.findByText("需要负责人处理")).toBeTruthy();
    expect(screen.getByText("请联系负责人 Avery 或团队管理员检查")).toBeTruthy();
    expect(screen.getByText("当前不可用")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /聘用/ })).toBeNull();
  });

  it("does not put an 'unavailable' chip next to a Hire button", async () => {
    const hireable = {
      ...ITEM,
      agent_id: "osa_checking",
      availability: { discoverable: true, hireable: true, runnable: false, reasons: [], public_reason: "temporarily_unavailable" },
    };
    vi.mocked(listAgentCatalog).mockResolvedValue(result([hireable] as never));
    renderIntl(<AgentMarketplace />);

    expect(await screen.findByRole("button", { name: /聘用/ })).toBeTruthy();
    // The reason line may say it; the status chip must not sit beside the button.
    expect(document.querySelector(".am-status[data-reason]")).toBeNull();
  });

  it("keeps successful Hire state while revalidating", async () => {
    renderIntl(<AgentMarketplace />);
    fireEvent.click(await screen.findByRole("button", { name: "聘用" }));

    await waitFor(() => expect(hireAgent).toHaveBeenCalledWith("osa_1"));
    await waitFor(() => expect(listAgentCatalog).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Finance Partner 已加入“我的智能体团队”并启用")).toBeTruthy();
    expect(screen.getByText(/这不会固定每次对话都使用它/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "管理已聘用智能体" })).toBeTruthy();
  });

  it("focuses marketplace search from the displayed keyboard shortcut", async () => {
    renderIntl(<AgentMarketplace />);
    const search = await screen.findByRole("textbox", { name: /搜索智能体/ });

    fireEvent.keyDown(window, { key: "k", metaKey: true });

    expect(document.activeElement).toBe(search);
  });

  it("opens a complete filter dialog and applies its draft state", async () => {
    renderIntl(<AgentMarketplace />);
    await screen.findAllByRole("heading", { name: "Finance Partner" });
    const trigger = await screen.findByRole("button", { name: /^筛选/ });

    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "找到适合当前工作的智能体" });
    fireEvent.click(within(dialog).getByRole("radio", { name: "团队创建" }));
    fireEvent.click(within(dialog).getByRole("radio", { name: /查询与分析数据/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "查看匹配智能体" }));

    await waitFor(() => expect(listAgentCatalog).toHaveBeenLastCalledWith(expect.objectContaining({
      source: "team",
      capability: "cap_data_query",
    })));
    expect(replace).toHaveBeenLastCalledWith("/agents/marketplace/?source=team&capability=cap_data_query", { scroll: false });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("closes the filter dialog with Escape and restores focus", async () => {
    renderIntl(<AgentMarketplace />);
    const trigger = await screen.findByRole("button", { name: /^筛选/ });
    fireEvent.click(trigger);
    expect(document.body.style.overflow).toBe("hidden");

    fireEvent.keyDown(screen.getByRole("button", { name: "关闭筛选" }), { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.style.overflow).toBe("");
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("keeps keyboard focus inside the filter dialog", async () => {
    renderIntl(<AgentMarketplace />);
    fireEvent.click(await screen.findByRole("button", { name: /^筛选/ }));
    const dialog = screen.getByRole("dialog", { name: "找到适合当前工作的智能体" });
    const buttons = within(dialog).getAllByRole("button");
    const first = buttons[0];
    const last = buttons[buttons.length - 1];

    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(first);

    first.focus();
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it("restores URL filters and sends them to the server before pagination", async () => {
    window.history.replaceState({}, "", "/agents/marketplace/?q=finance&source=team&capability=cap_data_query");
    renderIntl(<AgentMarketplace />);

    await waitFor(() => expect(listAgentCatalog).toHaveBeenCalledWith(expect.objectContaining({
      q: "finance",
      source: "team",
      capability: "cap_data_query",
      domain: "all",
      job: "all",
      tag: "all",
      cursor: undefined,
      limit: 48,
    })));
    // The URL already says this; re-replacing it would re-run navigation (and move focus) for nothing.
    expect(replace).not.toHaveBeenCalled();
  });

  it("does not replace an unchanged URL after mount", async () => {
    window.history.replaceState({}, "", "/agents/marketplace/");
    renderIntl(<AgentMarketplace />);
    await waitFor(() => expect(listAgentCatalog).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(replace).not.toHaveBeenCalled();
  });

  it("filters by category from the toolbar menu button (no native select, no inline chip strip)", async () => {
    vi.mocked(listAgentCatalog).mockResolvedValue(result([ITEM], null, { domains: [{ id: "finance", count: 1 }] }));
    renderIntl(<AgentMarketplace />);

    await screen.findAllByRole("heading", { name: "Finance Partner" });
    expect(document.querySelector("select")).toBeNull();
    const category = screen.getByTestId("am-category-filter");
    expect(category.textContent).toBe("分类：全部");
    expect(category.hasAttribute("data-active")).toBe(false);
    fireEvent.click(category, { detail: 0 });
    const menu = await screen.findByTestId("am-category-filter-menu");
    expect(menu.getAttribute("role")).toBe("menu");
    expect(within(menu).getByRole("menuitemradio", { name: "全部" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(within(menu).getByRole("menuitemradio", { name: /^财务/ }));

    await waitFor(() => expect(listAgentCatalog).toHaveBeenLastCalledWith(expect.objectContaining({ domain: "finance" })));
    expect(screen.getByTestId("am-category-filter").getAttribute("data-active")).toBe("true");
  });

  it("gives each card one button: Hire is primary, a hired card's details link is secondary", async () => {
    const hired = { ...ITEM, agent_id: "osa_hired", display_name: "Hired Partner", assignment: { status: "active" as const, source: "user_hired", row_version: 1 }, actions: { ...ITEM.actions, hire: false } };
    vi.mocked(listAgentCatalog).mockResolvedValue(result([{ ...ITEM, actions: { ...ITEM.actions, hire: true } }, hired] as never));
    renderIntl(<AgentMarketplace />);

    const hire = await screen.findByRole("button", { name: "聘用" });
    expect(hire.className).toContain("settings-button--primary");
    const details = screen.getAllByRole("link", { name: "查看" });
    expect(details).toHaveLength(1);
    expect(details[0].className).toContain("settings-button--secondary");
  });

  it("filters the catalog by business area, work type, and governed team tag", async () => {
    vi.mocked(listAgentCatalog).mockResolvedValue(result([ITEM], null, {
      domains: [{ id: "finance", count: 1 }],
      jobs: [{ id: "analysis", count: 1 }],
      teamTags: [{ id: "att_1", label: "Monthly close", count: 1 }],
    }));
    renderIntl(<AgentMarketplace />);

    await screen.findAllByRole("heading", { name: "Finance Partner" });
    fireEvent.click(await screen.findByRole("button", { name: /^筛选/ }));
    const dialog = screen.getByRole("dialog", { name: "找到适合当前工作的智能体" });
    fireEvent.click(within(within(dialog).getByRole("group", { name: "业务场景" })).getByRole("radio", { name: /^财务/ }));
    fireEvent.click(within(within(dialog).getByRole("group", { name: "工作类型" })).getByRole("radio", { name: /^分析/ }));
    fireEvent.click(within(within(dialog).getByRole("group", { name: "团队标签" })).getByRole("radio", { name: /^Monthly close/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "查看匹配智能体" }));

    await waitFor(() => expect(listAgentCatalog).toHaveBeenLastCalledWith(expect.objectContaining({
      domain: "finance",
      job: "analysis",
      tag: "att_1",
    })));
    expect(replace).toHaveBeenLastCalledWith("/agents/marketplace/?domain=finance&job=analysis&tag=att_1", { scroll: false });
  });

  it("loads the next server cursor without replacing earlier Agents", async () => {
    const second = { ...ITEM, agent_id: "osa_2", display_name: "Sales Partner" };
    vi.mocked(listAgentCatalog)
      .mockResolvedValueOnce(result([ITEM], "cursor-2"))
      .mockResolvedValueOnce(result([second], null));
    renderIntl(<AgentMarketplace />);

    fireEvent.click(await screen.findByRole("button", { name: "加载更多智能体" }));
    expect((await screen.findAllByRole("heading", { name: "Sales Partner" })).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("heading", { name: "Finance Partner" }).length).toBeGreaterThan(0);
    expect(listAgentCatalog).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: "cursor-2" }));
  });

  it("uses server totals, full capability facets, and the explicit recent collection", async () => {
    const recent = { ...ITEM, agent_id: "osa_recent", display_name: "Recently Updated" };
    const laterPageCapability = {
      id: "cap_report",
      label_key: "cap_report",
      label: "撰写报告",
      category: "output",
      verification_status: "derived_unverified" as const,
      count: 1,
    };
    vi.mocked(listAgentCatalog).mockResolvedValueOnce(result([ITEM], "cursor-2", {
      totalCount: 49,
      featured: [recent],
      facets: [{ ...ITEM.capabilities[0], count: 48 }, laterPageCapability],
    }));
    renderIntl(<AgentMarketplace />);

    expect(await screen.findByText("49 个可发现的智能体")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Recently Updated" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^筛选/ }));
    expect(within(screen.getByRole("dialog", { name: "找到适合当前工作的智能体" })).getByRole("radio", { name: /撰写报告/ })).toBeTruthy();
  });

  it("loads a catalog response from an older schema-v1 gateway during a rolling deploy", async () => {
    vi.mocked(listAgentCatalog).mockResolvedValueOnce({
      kind: "agent_catalog_collection",
      schema_version: 1,
      items: [ITEM],
      collections: [],
      actions: { create: true },
      next_cursor: null,
    });
    renderIntl(<AgentMarketplace />);

    expect((await screen.findAllByRole("heading", { name: "Finance Partner" })).length).toBeGreaterThan(0);
    expect(screen.getByText("1 个可发现的智能体")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^筛选/ }));
    expect(within(screen.getByRole("dialog", { name: "找到适合当前工作的智能体" })).getByRole("radio", { name: /查询与分析数据/ })).toBeTruthy();
    expect(screen.queryByText("智能体市场加载失败")).toBeNull();
  });

  it("applies the selected source and capability to an old schema-v1 response", async () => {
    window.history.replaceState({}, "", "/agents/marketplace/?source=team&capability=cap_data_query");
    const wrongSource = {
      ...ITEM,
      agent_id: "system:wrong",
      display_name: "Wrong source",
      source: { kind: "system" as const, owner_label: "Bowhead", owner_user_id: null },
    };
    const wrongCapability = {
      ...ITEM,
      agent_id: "osa_wrong",
      display_name: "Wrong capability",
      capabilities: [{ id: "cap_report", label_key: "cap_report", label: "撰写报告", category: "output" }],
    };
    vi.mocked(listAgentCatalog).mockResolvedValueOnce({
      kind: "agent_catalog_collection",
      schema_version: 1,
      items: [ITEM, wrongSource, wrongCapability],
      collections: [],
      actions: { create: true },
      next_cursor: null,
    });
    renderIntl(<AgentMarketplace />);

    expect((await screen.findAllByRole("heading", { name: "Finance Partner" })).length).toBeGreaterThan(0);
    expect(screen.queryByRole("heading", { name: "Wrong source" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Wrong capability" })).toBeNull();
    expect(screen.getByText("1 个可发现的智能体")).toBeTruthy();
  });

  it("does not merge a legacy response into a cursor-paginated new response", async () => {
    const duplicate = { ...ITEM, agent_id: "osa_wrong", display_name: "Wrong page" };
    vi.mocked(listAgentCatalog)
      .mockResolvedValueOnce(result([ITEM], "cursor-2", { totalCount: 49 }))
      .mockResolvedValueOnce({
        kind: "agent_catalog_collection",
        schema_version: 1,
        items: [ITEM, duplicate],
        collections: [],
        actions: { create: true },
        next_cursor: null,
      });
    renderIntl(<AgentMarketplace />);

    fireEvent.click(await screen.findByRole("button", { name: "加载更多智能体" }));
    expect(await screen.findByText("智能体市场加载失败")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Wrong page" })).toBeNull();
    expect(screen.getByText("49 个可发现的智能体")).toBeTruthy();
    expect(screen.getByRole("button", { name: "加载更多智能体" })).toBeTruthy();
  });

  it("does not misreport a failed catalog request as an empty result", async () => {
    vi.mocked(listAgentCatalog).mockRejectedValueOnce(new Error("network"));
    renderIntl(<AgentMarketplace />);

    expect(await screen.findByText("智能体市场加载失败")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "没有匹配的智能体" })).toBeNull();
  });

  it("uses client navigation while preserving real link hrefs", async () => {
    renderIntl(<AgentMarketplace />);

    const link = (await screen.findAllByRole("link", { name: "查看 Finance Partner" }))[0];
    expect(link.getAttribute("href")).toContain("/agents/detail/");
    fireEvent.click(link);
    expect(push).toHaveBeenCalledWith("/agents/detail/?id=osa_1");
  });

  it("revalidates a completed Hire against the latest filters", async () => {
    const sales = { ...ITEM, agent_id: "osa_2", display_name: "Sales Partner" };
    vi.mocked(listAgentCatalog).mockImplementation(async (params) => (
      params?.q === "sales" ? result([sales]) : result([ITEM])
    ));
    let completeHire: ((value: Awaited<ReturnType<typeof hireAgent>>) => void) | undefined;
    vi.mocked(hireAgent).mockReturnValue(new Promise((resolve) => {
      completeHire = resolve;
    }));
    renderIntl(<AgentMarketplace />);

    fireEvent.click(await screen.findByRole("button", { name: "聘用" }));
    fireEvent.change(screen.getByRole("textbox", { name: /搜索智能体/ }), {
      target: { value: "sales" },
    });
    await act(async () => {
      completeHire?.({
        kind: "agent_assignment_mutation",
        schema_version: 1,
        assignment: { status: "active", source: "user_hired", row_version: 1 },
        runnable: true,
        public_reason: null,
      });
    });

    expect((await screen.findAllByRole("heading", { name: "Sales Partner" })).length).toBeGreaterThan(0);
    await waitFor(() => expect(listAgentCatalog).toHaveBeenLastCalledWith(expect.objectContaining({
      q: "sales",
    })));
    expect(screen.queryByRole("heading", { name: "Finance Partner" })).toBeNull();
  });

  it("does not retain the previous filter results when a new filter fails", async () => {
    vi.mocked(listAgentCatalog).mockImplementation(async (params) => {
      if (params?.q === "sales") throw new Error("network");
      return result([ITEM]);
    });
    renderIntl(<AgentMarketplace />);

    expect((await screen.findAllByRole("heading", { name: "Finance Partner" })).length).toBeGreaterThan(0);
    fireEvent.change(screen.getByRole("textbox", { name: /搜索智能体/ }), {
      target: { value: "sales" },
    });

    expect(await screen.findByText("智能体市场加载失败")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Finance Partner" })).toBeNull();
    expect(screen.queryByRole("button", { name: "加载更多智能体" })).toBeNull();
  });
});
