import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import {
  getAgentReleaseInboxCount,
  getAgentRolePreferences,
  getAgentUnhireImpact,
  hireAgent,
  listAgentReleaseInbox,
  listManagedAgents,
  setAgentPersonalStatus,
  type AgentCatalogItem,
  type AgentManagementItem,
  type AgentManagementView,
} from "@/lib/agents-api";
import {
  discardSubagentDraft,
  getManualSubagentDraft,
  getSubagentDraft,
  listManualSubagentDrafts,
  listSubagentDrafts,
} from "@/lib/subagent-drafts-api";
import { setAgentReleaseBarCapabilityForTest } from "@/lib/use-agent-release-bar-capability";
import { AgentManagement } from "./agent-management";

const session = vi.hoisted(() => ({ directUse: true, drafting: true, manualDrafting: true, handoff: vi.fn() }));
vi.mock("@/lib/use-space", () => ({ useSpaceSelection: () => ({ activeId: "ws_current", loading: false, error: null }) }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { team_id: "team_current", capabilities: { composer: { agent_start_use: session.directUse }, subagent_authoring: { authoring: session.drafting, unified_draft: session.manualDrafting } } } }),
}));
vi.mock("@/lib/subagent-drafts-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/subagent-drafts-api")>();
  return {
    SubagentDraftApiError: actual.SubagentDraftApiError,
    listSubagentDrafts: vi.fn(),
    listManualSubagentDrafts: vi.fn(),
    getSubagentDraft: vi.fn(),
    getManualSubagentDraft: vi.fn(),
    discardSubagentDraft: vi.fn(),
  };
});
vi.mock("@/lib/composer-handoff", () => ({ handoffAgentToNewChat: (...args: unknown[]) => session.handoff(...args) }));
const appRouter = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => appRouter }));
vi.mock("@/lib/agents-api", () => ({
  clearAgentRolePreference: vi.fn(),
  getAgentRolePreferences: vi.fn(),
  getAgentUnhireImpact: vi.fn(),
  getAgentReleaseInboxCount: vi.fn(),
  hireAgent: vi.fn(),
  listAgentReleaseInbox: vi.fn(),
  listManagedAgents: vi.fn(),
  previewAgentTeamDefault: vi.fn(),
  removeAgentTeamDefault: vi.fn(),
  setAgentTeamDefault: vi.fn(),
  setAgentPersonalStatus: vi.fn(),
  setAgentRolePreference: vi.fn(),
  unhireAgent: vi.fn(),
}));

function agent(agentId: string, name: string, overrides: Partial<AgentCatalogItem> = {}): AgentCatalogItem {
  return {
    kind: "agent_catalog_item",
    schema_version: 1,
    agent_id: agentId,
    display_name: name,
    summary: `${name} 的介绍`,
    lifecycle_state: "active",
    source: { kind: "team", owner_label: "Avery", owner_user_id: "usr_1" },
    facets: { domains: [], jobs: [], outputs: [] },
    capabilities: [],
    risk: { level: "low", reasons: [] },
    safety: { status: "pass", checked_subject: "legacy_row", reasons: [] },
    assignment: { id: `asa_${agentId}`, status: "active", source: "user_hired", row_version: 1 },
    team_default: { enabled: false, policy_version: null, row_version: null },
    availability: { discoverable: true, hireable: false, runnable: true, reasons: [], public_reason: null },
    actions: { view_detail: true, hire: false, enable: false, disable: true, unhire: true, manage: true, manage_team_default: false, create: true },
    action_reasons: {},
    relationship: "owned",
    updated_label: { kind: "saved_at", at: "2026-11-01T00:00:00Z" },
    manager_url: `/agents/manage/?agent_id=${agentId}`,
    ...overrides,
  };
}

function collection(view: AgentManagementView, items: AgentManagementItem[], nextCursor: string | null = null, total = items.length) {
  return {
    kind: "agent_management_collection" as const,
    schema_version: 1 as const,
    view,
    items,
    total_count: total,
    next_cursor: nextCursor,
    views: { hired: true, created: true, shared: true, archived: true, team: true },
    actions: { create: true, manage_team_defaults: true },
  };
}

const PAUSED = { status: "personal_disabled" as const, source: "user_hired", row_version: 3 };

describe("My Agents with the release bar switch on", () => {
  beforeEach(() => {
    setAgentReleaseBarCapabilityForTest(true);
    vi.mocked(getAgentReleaseInboxCount).mockResolvedValue({ kind: "agent_release_inbox_count", schema_version: 1, count: 2 });
    vi.mocked(listAgentReleaseInbox).mockResolvedValue({ kind: "agent_release_inbox", schema_version: 1, items: [], count: 0 } as never);
    vi.mocked(getAgentRolePreferences).mockResolvedValue({ kind: "agent_role_preferences", schema_version: 1, candidates_truncated: false, roles: [] });
    vi.mocked(listSubagentDrafts).mockResolvedValue([]);
    vi.mocked(listManualSubagentDrafts).mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    setAgentReleaseBarCapabilityForTest(null);
  });

  it("names the team tab 团队审批 on the same route with the same badge, without intro cards", async () => {
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", [agent("osa_1", "商品比价")]));
    renderIntl(<AgentManagement />);

    const tab = await screen.findByRole("link", { name: /团队审批/ });
    expect(tab.getAttribute("href")).toContain("/agents/manage/governance/");
    await waitFor(() => expect(tab.textContent).toContain("2"));
    expect(screen.queryByRole("link", { name: /团队治理/ })).toBeNull();
    expect(screen.queryByRole("heading", { name: "维护你创建的智能体" })).toBeNull();
  });

  it("lists drafts first with their expiry and deletes a manual draft in its Space", async () => {
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", [agent("osa_1", "商品比价")]));
    vi.mocked(listManualSubagentDrafts).mockResolvedValue([{
      draft_id: "sad_manual", draft_status: "editing", source_mode: "manual", title: "", updated_at: "2026-11-24T09:35:00Z", expires_at: "2026-12-01T00:00:00Z",
    }]);
    vi.mocked(getManualSubagentDraft).mockResolvedValue({ draft_id: "sad_manual", etag: "\"v3\"" } as never);
    vi.mocked(discardSubagentDraft).mockResolvedValue();
    renderIntl(<AgentManagement />);

    const drafts = await screen.findByRole("region", { name: /继续创建/ });
    expect(within(drafts).getByText(/前没创建完会过期，过期后不能再继续/)).toBeTruthy();
    expect(within(drafts).getByRole("link", { name: "继续创建 未命名草稿" }).getAttribute("href")).toContain("agent_draft=sad_manual");
    const order = [drafts, screen.getByRole("region", { name: /我创建的智能体/ })];
    expect(order[0].compareDocumentPosition(order[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(within(drafts).getByRole("button", { name: "删除 未命名草稿…" }));
    const dialog = await screen.findByRole("dialog", { name: "删除草稿「未命名草稿」？" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(discardSubagentDraft).toHaveBeenCalledWith("sad_manual", "\"v3\"", { teamId: "team_current", workspaceId: "ws_current" }));
    expect(getManualSubagentDraft).toHaveBeenCalledWith("sad_manual", { teamId: "team_current", workspaceId: "ws_current" });
    expect(getSubagentDraft).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("region", { name: /继续创建/ })).toBeNull());
  });

  it("gives owned rows one 管理 link, moves the rest into ⋯ and points archived Agents to 已归档", async () => {
    window.history.replaceState({}, "", "/agents/manage/created/");
    const archived = agent("osa_old", "旧周报", { lifecycle_state: "archived" });
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", [agent("osa_1", "商品比价"), archived]));
    renderIntl(<AgentManagement />);

    const manage = await screen.findByRole("link", { name: "管理 商品比价" });
    expect(manage.getAttribute("href")).toContain("/agents/manage/?agent_id=osa_1");
    expect(screen.queryByText(/由 Avery 创建/)).toBeNull();
    expect(screen.queryByText("旧周报")).toBeNull();
    const pointer = screen.getByText(/另有 1 个已归档的智能体在/);
    expect(within(pointer).getByRole("link", { name: "已归档" }).getAttribute("href")).toContain("/agents/manage/archived/");
    const menu = await openRowMenu("商品比价 的更多操作");
    expect(menuItemNames(menu)).toEqual(["开始使用", "停用", "解聘…"]);
  });

  it("loads the remaining pages on first search, then filters and sorts the whole list", async () => {
    window.history.replaceState({}, "", "/agents/manage/created/");
    const paused = agent("osa_2", "Beta 门店周报", { assignment: PAUSED, availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "personally_disabled" }, actions: { ...agent("x", "x").actions, enable: true, disable: false } });
    vi.mocked(listManagedAgents)
      .mockResolvedValueOnce(collection("created", [agent("osa_1", "Alpha 比价")], "cursor-2", 2))
      .mockResolvedValueOnce(collection("created", [paused], null, 2));
    renderIntl(<AgentManagement />);

    await screen.findByRole("link", { name: "管理 Alpha 比价" });
    expect(screen.queryByText("Beta 门店周报")).toBeNull();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索我创建的智能体" }), { target: { value: "周报" } });
    expect(await screen.findByText("Beta 门店周报")).toBeTruthy();
    expect(listManagedAgents).toHaveBeenLastCalledWith("created", { cursor: "cursor-2", limit: 100 });
    expect(screen.queryByText("Alpha 比价")).toBeNull();
    expect(screen.getByText("你停用了")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "加载更多智能体" })).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索我创建的智能体" }), { target: { value: "" } });
    fireEvent.change(screen.getByRole("combobox", { name: "显示" }), { target: { value: "disabled" } });
    expect(screen.getByText("Beta 门店周报")).toBeTruthy();
    expect(screen.queryByText("Alpha 比价")).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: "显示" }), { target: { value: "all" } });
    fireEvent.change(screen.getByRole("combobox", { name: "排序" }), { target: { value: "name" } });
    const names = screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
    expect(names.indexOf("Alpha 比价")).toBeLessThan(names.indexOf("Beta 门店周报"));
    expect(listManagedAgents).toHaveBeenCalledTimes(2);
  });

  it("says the search covers only the first 500 rows when there are more", async () => {
    window.history.replaceState({}, "", "/agents/manage/created/");
    const page = (start: number, count: number) => Array.from({ length: count }, (_, index) => agent(`osa_${String(start + index).padStart(4, "0")}`, `Agent ${start + index}`));
    vi.mocked(listManagedAgents)
      .mockResolvedValueOnce(collection("created", page(0, 48), "c1", 700))
      .mockImplementation(async (_view, params) => {
        const offset = Number(params?.cursor?.slice(1)) * 100 - 52;
        return collection("created", page(offset, 100), `c${Number(params?.cursor?.slice(1)) + 1}`, 700);
      });
    renderIntl(<AgentManagement />);
    await screen.findByRole("link", { name: "管理 Agent 0" });
    fireEvent.change(screen.getByRole("combobox", { name: "排序" }), { target: { value: "name" } });
    expect(await screen.findByText("智能体太多，搜索和筛选只覆盖前 500 个。")).toBeTruthy();
    expect(listManagedAgents).toHaveBeenCalledTimes(6);
  });

  it("folds hired Agents that cannot run, each with its one reason and action", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    const paused = agent("osa_off", "KB Reader", { relationship: "team", assignment: PAUSED, availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "personally_disabled" }, actions: { ...agent("x", "x").actions, enable: true, disable: false } });
    const archived = agent("osa_gone", "可用性原因", { relationship: "team", lifecycle_state: "archived", availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "agent_retired" }, actions: { ...agent("x", "x").actions, disable: false } });
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [agent("osa_1", "商品比价"), paused, archived]));
    vi.mocked(setAgentPersonalStatus).mockResolvedValue({ kind: "agent_assignment_mutation", schema_version: 1, assignment: { status: "active", source: "user_hired", row_version: 4 }, runnable: true, public_reason: null });
    vi.mocked(getAgentUnhireImpact).mockResolvedValue({ kind: "agent_unhire_impact", schema_version: 1, assignment_id: "asa_osa_gone", assignment_row_version: 1, impact_token: "t", expires_in_seconds: 300, active_count: 0, paused_count: 0, running_count: 0, jobs: [] });
    renderIntl(<AgentManagement />);

    expect(await screen.findByRole("button", { name: "开始使用 商品比价" })).toBeTruthy();
    expect(screen.getByText("我是所有者")).toBeTruthy();
    const folded = screen.getByText("现在用不了").closest("details")!;
    expect(folded.open).toBe(false);
    expect(folded.textContent).toContain("· 2");
    expect(within(folded).getByText("你停用了它，只影响你自己")).toBeTruthy();
    expect(within(folded).getByText("所有者把它归档了，取消归档前都用不了")).toBeTruthy();
    fireEvent.click(within(folded).getByRole("button", { name: "解聘 可用性原因…" }));
    expect(await screen.findByRole("dialog", { name: /解聘 可用性原因/ })).toBeTruthy();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(within(folded).getByRole("button", { name: "重新启用 KB Reader" }));
    await waitFor(() => expect(setAgentPersonalStatus).toHaveBeenCalledWith("osa_off", "active", 3));
    // The row moves between groups; the change is announced and focus stays on the row.
    expect(await screen.findByText("已重新启用「KB Reader」")).toBeTruthy();
    await waitFor(() => expect(document.activeElement?.closest("[data-agent-id]")?.getAttribute("data-agent-id")).toBe("osa_off"));
  });

  it("words the viewer's own unavailable Agents as theirs and sends them to 管理", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    const ownArchived = agent("osa_mine", "我的旧助手", { lifecycle_state: "archived", availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "agent_retired" }, actions: { ...agent("x", "x").actions, disable: false } });
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [agent("osa_1", "商品比价"), ownArchived]));
    renderIntl(<AgentManagement />);
    const folded = (await screen.findByText("现在用不了")).closest("details")!;
    expect(within(folded).getByText("你把它归档了，取消归档前都用不了")).toBeTruthy();
    expect(within(folded).getByRole("link", { name: "管理 我的旧助手" })).toBeTruthy();
  });

  it("offers 查看详情 before 聘用 on shared rows", async () => {
    window.history.replaceState({}, "", "/agents/manage/shared/");
    const shared = agent("osa_s", "财务分析", {
      relationship: "team",
      assignment: { status: "not_hired", source: null, row_version: null },
      availability: { discoverable: true, hireable: true, runnable: false, reasons: [], public_reason: "not_hired" },
      actions: { ...agent("x", "x").actions, hire: true, disable: false, unhire: false, manage: false },
    });
    vi.mocked(listManagedAgents).mockResolvedValue(collection("shared", [shared]));
    vi.mocked(hireAgent).mockResolvedValue({ kind: "agent_assignment_mutation", schema_version: 1, assignment: { status: "active", source: "user_hired", row_version: 1 }, runnable: true, public_reason: null });
    renderIntl(<AgentManagement />);

    const detail = await screen.findByRole("link", { name: "查看 财务分析 的详情" });
    const hire = screen.getByRole("button", { name: "聘用 财务分析" });
    expect(detail.compareDocumentPosition(hire) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("由 Avery 创建")).toBeTruthy();
    fireEvent.click(hire);
    await waitFor(() => expect(hireAgent).toHaveBeenCalledWith("osa_s"));
  });

  it("keeps the approval queue on 团队审批 with 开始审批 as a secondary row action", async () => {
    window.history.replaceState({}, "", "/agents/manage/governance/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("team", [agent("osa_1", "商品比价")]));
    vi.mocked(listAgentReleaseInbox).mockResolvedValue({
      kind: "agent_release_inbox", schema_version: 1, count: 1,
      items: [{
        kind: "agent_release_inbox_item", schema_version: 1, candidate_id: "arc_1", candidate_hash: "h", candidate_row_version: 1,
        agent_id: "osa_1", display_name: "商品比价", owner_user_id: "usr_1", change_kind: "authored",
        audience: { mode: "custom", user_count: 1, group_count: 0 }, risk: { level: "low" }, safety: {},
        submitted_at: "2026-11-24T09:47:00Z", detail_url: "/agents/manage/?release_candidate=arc_1",
      }],
    } as never);
    renderIntl(<AgentManagement />);

    const row = await screen.findByRole("link", { name: /商品比价.*开始审批/ });
    expect(row.closest(".am-release-inbox-list")?.getAttribute("data-layout")).toBe("v2");
    expect(screen.getByRole("link", { name: /团队审批/ }).getAttribute("aria-current")).toBe("page");
  });

  it("keeps unhire confirmation in the hired row menu", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [agent("osa_1", "商品比价")]));
    vi.mocked(getAgentUnhireImpact).mockResolvedValue({ kind: "agent_unhire_impact", schema_version: 1, assignment_id: "asa_osa_1", assignment_row_version: 1, impact_token: "t", expires_in_seconds: 300, active_count: 0, paused_count: 0, running_count: 0, jobs: [] });
    renderIntl(<AgentManagement />);

    await chooseRowAction("商品比价 的更多操作", /^解聘/, { danger: true });
    expect(await screen.findByRole("dialog", { name: /解聘 商品比价/ })).toBeTruthy();
  });
});
