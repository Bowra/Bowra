import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import messages from "@/messages/zh-CN.json";
import { chooseRowAction } from "@/__tests__/row-actions";
import {
  getAgentRolePreferences,
  getAgentUnhireImpact,
  getAgentReleaseInboxCount,
  hireAgent,
  listAgentReleaseInbox,
  listManagedAgents,
  previewAgentTeamDefault,
  setAgentRolePreference,
  setAgentTeamDefault,
  setAgentPersonalStatus,
  unhireAgent,
  type AgentCatalogItem,
  type AgentManagementView,
  type AgentRolePreferences,
} from "@/lib/agents-api";
import { AgentManagement } from "./agent-management";
import { listManualSubagentDrafts, listSubagentDrafts, SubagentDraftApiError } from "@/lib/subagent-drafts-api";

const push = vi.fn();
const directUse = vi.hoisted(() => ({ enabled: false, drafting: false, manualDrafting: false, handoff: vi.fn() }));
const spaceState = vi.hoisted(() => ({ activeId: "ws_current" as string | null, loading: false, error: null as string | null }));
vi.mock("@/lib/use-space", () => ({ useSpaceSelection: () => spaceState }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => ({ me: { team_id: "team_current", capabilities: { composer: { agent_start_use: directUse.enabled }, subagent_authoring: { authoring: directUse.drafting, unified_draft: directUse.manualDrafting } } } }) }));
vi.mock("@/lib/subagent-drafts-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/subagent-drafts-api")>();
  return { SubagentDraftApiError: actual.SubagentDraftApiError, listSubagentDrafts: vi.fn(), listManualSubagentDrafts: vi.fn() };
});
vi.mock("@/lib/composer-handoff", () => ({ handoffAgentToNewChat: (...args: unknown[]) => directUse.handoff(...args) }));
const appRouter = { push, replace: vi.fn() };
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => appRouter,
}));
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

const ITEM: AgentCatalogItem = {
  kind: "agent_catalog_item" as const,
  schema_version: 1 as const,
  agent_id: "osa_1",
  display_name: "Finance Partner",
  summary: "Explains monthly performance.",
  source: { kind: "team" as const, owner_label: "Avery", owner_user_id: "usr_1" },
  facets: { domains: ["finance"], jobs: ["analysis"], outputs: [] },
  capabilities: [{ id: "cap_data_query", label_key: "cap_data_query", label: "Query data", category: "data" }],
  risk: { level: "low", reasons: [] },
  safety: { status: "pass", checked_subject: "legacy_row", reasons: [] },
  assignment: { id: "asa_1", status: "active" as const, source: "user_hired", row_version: 1 },
  team_default: { enabled: false, policy_version: null, row_version: null },
  availability: { discoverable: true, hireable: false, runnable: true, reasons: [] },
  actions: {
    view_detail: true,
    hire: false,
    enable: false,
    disable: true,
    unhire: true,
    manage: true,
    manage_team_default: true,
    create: true,
  },
  action_reasons: {},
  relationship: "created",
  updated_label: { kind: "saved_at", at: "2026-08-30T00:00:00Z" },
  manager_url: "/agents/manage/?agent_id=osa_1",
};

function collection(
  view: AgentManagementView,
  items = [ITEM],
  teamVisible = true,
  nextCursor: string | null = null,
) {
  return {
    kind: "agent_management_collection" as const,
    schema_version: 1 as const,
    view,
    items,
    total_count: items.length,
    next_cursor: nextCursor,
    views: { hired: true, created: true, shared: true, archived: true, team: teamVisible },
    actions: { create: true, manage_team_defaults: true },
  };
}

const ROLE_PREFERENCES: AgentRolePreferences = {
  kind: "agent_role_preferences" as const,
  schema_version: 1 as const,
  candidates_truncated: false,
  roles: (["data", "compute", "deck", "knowledge", "canvas_edit", "image"] as const).map((roleId) => ({
    role_id: roleId,
    label_key: `role_${roleId}`,
    description_key: `role_${roleId}_description`,
    row_version: 0,
    saved_assignment_id: null,
    preference: null,
    invalid_reason: null,
    availability: "behavior_unavailable" as const,
    candidates: [],
    actions: { set: false, clear: false },
  })),
};

describe("AgentManagement", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/agents/manage/governance/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [], false));
    vi.mocked(getAgentReleaseInboxCount).mockResolvedValue({
      kind: "agent_release_inbox_count",
      schema_version: 1,
      count: 0,
    });
    vi.mocked(getAgentRolePreferences).mockResolvedValue(ROLE_PREFERENCES);
    vi.mocked(listAgentReleaseInbox).mockResolvedValue({
      kind: "agent_release_inbox",
      schema_version: 1,
      items: [],
      count: 0,
    });
    vi.mocked(previewAgentTeamDefault).mockResolvedValue({
      kind: "agent_team_default_preview",
      schema_version: 1,
      agent_id: "osa_1",
      action: "set",
      active_member_count: 2,
      assignment_count: 1,
      opted_out_count: 0,
      affected_count: 1,
      policy: null,
    });
    vi.mocked(getAgentUnhireImpact).mockResolvedValue({
      kind: "agent_unhire_impact",
      schema_version: 1,
      assignment_id: "asa_1",
      assignment_row_version: 1,
      impact_token: "token",
      expires_in_seconds: 300,
      active_count: 0,
      paused_count: 0,
      running_count: 0,
      jobs: [],
    });
    vi.mocked(hireAgent).mockResolvedValue({
      kind: "agent_assignment_mutation",
      schema_version: 1,
      assignment: { status: "active", source: "user_hired", row_version: 2 },
      runnable: true,
      public_reason: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    directUse.enabled = false;
    directUse.drafting = false;
    directUse.manualDrafting = false;
    Object.assign(spaceState, { activeId: "ws_current", loading: false, error: null });
  });

  it("shows Continue creating only after resumable drafts have loaded", async () => {
    directUse.drafting = true;
    directUse.manualDrafting = true;
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", []));
    let finishDrafts!: (drafts: Awaited<ReturnType<typeof listManualSubagentDrafts>>) => void;
    vi.mocked(listSubagentDrafts).mockResolvedValue([]);
    vi.mocked(listManualSubagentDrafts).mockReturnValue(new Promise((resolve) => {
      finishDrafts = resolve;
    }));
    renderIntl(<AgentManagement />);

    await screen.findByRole("heading", { name: "维护你创建的智能体" });
    expect(screen.queryByRole("region", { name: "继续创建" })).toBeNull();
    await act(async () => { finishDrafts([]); });
    expect(screen.queryByRole("region", { name: "继续创建" })).toBeNull();
  });

  it("offers an owner's active AI draft in the created view", async () => {
    directUse.drafting = true;
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", []));
    vi.mocked(listSubagentDrafts).mockResolvedValue([{
      draft_id: "sad_my_draft",
      draft_status: "ready",
      source_mode: "intent",
      title: "核对网页内容",
      updated_at: "2026-09-26T00:00:00Z",
      expires_at: "2026-09-27T00:00:00Z",
    }]);
    renderIntl(<AgentManagement />);

    const link = await screen.findByRole("link", { name: "核对网页内容 · 继续编辑" });
    expect(link.getAttribute("href")).toContain("agent_draft=sad_my_draft");
    expect(listSubagentDrafts).toHaveBeenCalledTimes(1);
  });

  it("explains temporarily unavailable drafts without an alert while keeping the created view", async () => {
    directUse.drafting = true;
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", []));
    vi.mocked(listSubagentDrafts).mockRejectedValue(
      new SubagentDraftApiError(503, "subagent_draft_store_unavailable", "unavailable"),
    );
    renderIntl(<AgentManagement />);

    expect(await screen.findByText(/草稿功能暂时不可用/)).toBeTruthy();
    expect(screen.queryByText("暂时无法读取草稿，请稍后刷新重试。")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("alerts when drafts fail to load for another reason", async () => {
    directUse.drafting = true;
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", []));
    vi.mocked(listSubagentDrafts).mockRejectedValue(new SubagentDraftApiError(500, "internal_error", "boom"));
    renderIntl(<AgentManagement />);

    expect((await screen.findByRole("alert")).textContent).toBe("暂时无法读取草稿，请稍后刷新重试。");
  });

  it("lists a space-bound manual draft beside the owner's AI drafts", async () => {
    directUse.drafting = true;
    directUse.manualDrafting = true;
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", []));
    vi.mocked(listSubagentDrafts).mockResolvedValue([]);
    vi.mocked(listManualSubagentDrafts).mockResolvedValue([{
      draft_id: "sad_manual", draft_status: "editing", source_mode: "manual",
      title: "网页核对", updated_at: "2026-09-27T00:00:00Z",
      expires_at: "2026-09-28T00:00:00Z",
    }]);
    renderIntl(<AgentManagement />);
    const link = await screen.findByRole("link", { name: "网页核对 · 继续编辑" });
    expect(link.getAttribute("href")).toContain("agent_draft=sad_manual");
    expect(screen.getByText(/手动草稿 · 编辑中 · 最近保存/)).toBeTruthy();
    expect(screen.getByText("继续编辑你保存的 AI 或手动草稿。")).toBeTruthy();
    expect(listManualSubagentDrafts).toHaveBeenCalledTimes(1);
    expect(listManualSubagentDrafts).toHaveBeenCalledWith({ teamId: "team_current", workspaceId: "ws_current" });
  });

  it("waits for the resolved Space before reading Space-bound manual drafts", async () => {
    directUse.drafting = true;
    directUse.manualDrafting = true;
    Object.assign(spaceState, { activeId: null, loading: true });
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", []));
    vi.mocked(listSubagentDrafts).mockResolvedValue([]);
    vi.mocked(listManualSubagentDrafts).mockResolvedValue([]);
    const view = renderIntl(<AgentManagement />);
    await screen.findByRole("heading", { name: "维护你创建的智能体" });
    expect(listManualSubagentDrafts).not.toHaveBeenCalled();

    Object.assign(spaceState, { activeId: "ws_resolved", loading: false });
    view.rerender(<NextIntlClientProvider locale="zh-CN" messages={messages}><AgentManagement /></NextIntlClientProvider>);
    await waitFor(() => expect(listManualSubagentDrafts).toHaveBeenCalledWith({ teamId: "team_current", workspaceId: "ws_resolved" }));
  });

  it("does not ask for manual drafts when the account has no Space", async () => {
    directUse.drafting = true;
    directUse.manualDrafting = true;
    Object.assign(spaceState, { activeId: null, loading: false });
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", []));
    vi.mocked(listSubagentDrafts).mockResolvedValue([]);
    renderIntl(<AgentManagement />);
    await waitFor(() => expect(listSubagentDrafts).toHaveBeenCalledTimes(1));
    expect(listManualSubagentDrafts).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("follows the server-normalized view for a member deep link", async () => {
    renderIntl(<AgentManagement />);

    await waitFor(() => expect(listManagedAgents).toHaveBeenCalledWith("team", {
      cursor: undefined,
      limit: 48,
    }));
    await waitFor(() => {
      expect(screen.getByRole("link", { name: "我聘用的" }).getAttribute("aria-current")).toBe("page");
      expect(window.location.search).toBe("");
    });
    expect(screen.queryByRole("link", { name: "团队治理" })).toBeNull();
  });

  it("uses one accessible destructive dialog and restores focus on Escape", async () => {
    vi.mocked(listManagedAgents).mockResolvedValue(collection("team"));
    renderIntl(<AgentManagement />);

    const trigger = await screen.findByRole("button", { name: "设为团队默认" });
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: /设为团队默认/ });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    expect(document.querySelector(".am-page-content")?.hasAttribute("inert")).toBe(true);

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it.each([
    ["created", "配置与分享", ["停用", "解聘"]],
    ["team", "设为团队默认", ["停用", "解聘"]],
  ] as const)("projects only %s relationship actions", async (requestedView, expectedAction, hiddenActions) => {
    window.history.replaceState({}, "", `/agents/manage/${requestedView === "team" ? "governance" : requestedView}/`);
    vi.mocked(listManagedAgents).mockResolvedValue(collection(requestedView));
    renderIntl(<AgentManagement />);

    expect(await screen.findByRole(requestedView === "team" ? "button" : "link", { name: expectedAction })).toBeTruthy();
    hiddenActions.forEach((name) => expect(screen.queryByRole("button", { name })).toBeNull());
  });

  it("marks archived Agents in Created by me", async () => {
    window.history.replaceState({}, "", "/agents/manage/created/");
    const archived = { ...ITEM, agent_id: "osa_2", display_name: "Old Partner", lifecycle_state: "archived" as const };
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", [{ ...ITEM, lifecycle_state: "active" as const }, archived]));
    renderIntl(<AgentManagement />);

    const row = (await screen.findByRole("heading", { name: "Old Partner" })).closest("article") as HTMLElement;
    expect(within(row).getByText("已归档").getAttribute("data-status")).toBe("relationship-archived");
    const active = screen.getByRole("heading", { name: "Finance Partner" }).closest("article") as HTMLElement;
    expect(within(active).queryByText("已归档")).toBeNull();
  });

  it("lets a user hire from Shared with me without exposing owner configuration", async () => {
    window.history.replaceState({}, "", "/agents/manage/shared/");
    const shared = {
      ...ITEM,
      assignment: { status: "not_hired" as const, source: null, row_version: null },
      actions: { ...ITEM.actions, hire: true, disable: false, unhire: false },
      relationship: "shared",
    };
    vi.mocked(listManagedAgents).mockResolvedValue(collection("shared", [shared]));
    renderIntl(<AgentManagement />);

    const button = await screen.findByRole("button", { name: "聘用" });
    expect(screen.queryByRole("link", { name: "配置与分享" })).toBeNull();
    fireEvent.click(button);
    await waitFor(() => expect(hireAgent).toHaveBeenCalledWith("osa_1"));
  });

  it("lets an owner rehire an enabled Agent from Created by me", async () => {
    window.history.replaceState({}, "", "/agents/manage/created/");
    const removed = {
      ...ITEM,
      assignment: { status: "removed" as const, source: "user_hired", row_version: 2 },
      availability: { discoverable: false, hireable: true, runnable: false, reasons: ["not_hired"] },
      actions: { ...ITEM.actions, view_detail: false, hire: true, disable: false, unhire: false },
      relationship: "owned",
    };
    vi.mocked(listManagedAgents).mockResolvedValue(collection("created", [removed]));
    renderIntl(<AgentManagement />);

    expect(screen.queryByRole("link", { name: "Finance Partner" })).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "聘用" }));
    await waitFor(() => expect(hireAgent).toHaveBeenCalledWith("osa_1"));
  });

  it("says why a hired Agent cannot run and who can fix it, and offers a retry only when the state can change", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    const broken = {
      ...ITEM,
      source: { ...ITEM.source, owner_label: "liuyixin" },
      availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "agent_needs_owner_fix" },
    };
    const checking = {
      ...ITEM,
      agent_id: "osa_2",
      display_name: "M4",
      availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "temporarily_unavailable" },
    };
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [broken, checking] as never));
    renderIntl(<AgentManagement />);

    expect(await screen.findByText("需要负责人处理")).toBeTruthy();
    expect(screen.getByText("请联系负责人 liuyixin 或团队管理员检查")).toBeTruthy();
    expect(screen.getAllByText("暂时无法确认")).toHaveLength(2);
    const calls = vi.mocked(listManagedAgents).mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(vi.mocked(listManagedAgents).mock.calls.length).toBeGreaterThan(calls));
  });

  it("marks a running Agent with a missing tool as limited, and points a manager at their own fix", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    const limited = {
      ...ITEM,
      availability: { discoverable: true, hireable: false, runnable: true, reasons: [], public_reason: null, limited: { name: "图表插图", name_en: "Chart image", more: 2 } },
    };
    const mine = {
      ...ITEM,
      agent_id: "osa_mine",
      display_name: "Eddy",
      relationship: "owned",
      availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "agent_needs_owner_fix" },
    };
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [limited, mine] as never));
    renderIntl(<AgentManagement />);

    expect(await screen.findByText("部分能力受限")).toBeTruthy();
    // The count sits next to the name it counts, not after the whole sentence.
    expect(screen.getByText("图表插图 及另外 2 项目前不可用")).toBeTruthy();
    expect(screen.getByText("你可以在管理配置中处理")).toBeTruthy();
    expect(screen.getAllByText("当前不可用")).toHaveLength(1);
  });

  it("keeps owner configuration out of the personal Hired view", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired"));
    renderIntl(<AgentManagement />);

    expect(await screen.findByRole("button", { name: "停用" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "配置与分享" })).toBeNull();
  });

  it.each([
    new Error("RAW_PROGRAM_DIAGNOSTIC"),
    new ApiError(500, "RAW_STORAGE_DIAGNOSTIC", { code: "future_agent_code" }),
  ])("explains a failed personal Agent change without displaying arbitrary prose", async (failure) => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired"));
    vi.mocked(setAgentPersonalStatus).mockRejectedValueOnce(failure);
    renderIntl(<AgentManagement />);
    fireEvent.click(await screen.findByRole("button", { name: "停用" }));
    expect((await screen.findByRole("alert")).textContent).toContain(messages.agents_marketplace.action_failed);
    expect(screen.queryByText(failure.message)).toBeNull();
  });

  it("hides role preferences until a role has two verified candidates", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired"));
    renderIntl(<AgentManagement />);

    await waitFor(() => expect(getAgentRolePreferences).toHaveBeenCalled());
    expect(screen.queryByRole("heading", { name: "岗位首选" })).toBeNull();
    expect(screen.queryByText("待验证")).toBeNull();
  });

  it("lists only roles with two verified candidates and saves an explicit personal preference", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired"));
    const candidate = {
      assignment_id: "asa_1",
      agent_id: "osa_1",
      revision_ref: "arv_1",
      release_id: "arl_1",
      behavior_fingerprint: `sha256:${"a".repeat(64)}`,
      display_name: "Finance Partner",
      description: "Explains revenue changes.",
      is_primary: true,
    };
    const second = { ...candidate, assignment_id: "asa_2", agent_id: "osa_2", display_name: "Sales Helper", is_primary: false };
    const available = {
      ...ROLE_PREFERENCES,
      roles: ROLE_PREFERENCES.roles.map((role) => role.role_id === "data" ? {
        ...role,
        availability: "ready" as const,
        candidates: [candidate, second],
        actions: { set: true, clear: false },
      } : role),
    };
    const saved = {
      ...available,
      roles: available.roles.map((role) => role.role_id === "data" ? {
        ...role,
        row_version: 1,
        saved_assignment_id: candidate.assignment_id,
        preference: candidate,
        actions: { set: true, clear: true },
      } : role),
    };
    vi.mocked(getAgentRolePreferences).mockResolvedValue(available);
    vi.mocked(setAgentRolePreference).mockResolvedValue(saved);
    renderIntl(<AgentManagement />);

    expect(await screen.findByRole("heading", { name: "岗位首选" })).toBeTruthy();
    expect(screen.getByText("有两位智能体都能做同一类工作时，选一位让主控智能体优先找它。")).toBeTruthy();
    // Roles still awaiting verification are not listed.
    expect(screen.queryByText("待验证")).toBeNull();
    expect(screen.getByText("2 位可选")).toBeTruthy();
    // Picking a candidate in the menu saves it; there is no separate Save button any more.
    // The trigger's name carries the visible value too ("为数据查询选择智能体 选择智能体").
    const trigger = screen.getByRole("button", { name: "为数据查询选择智能体 选择智能体" });
    fireEvent.click(trigger);
    const menu = await screen.findByRole("menu");
    fireEvent.click(within(menu).getByRole("menuitemradio", { name: /^Finance Partner/ }));
    await waitFor(() => expect(setAgentRolePreference).toHaveBeenCalledWith("data", "asa_1", 0));
    expect(await screen.findByText("已指定")).toBeTruthy();
    expect(screen.getByRole("button", { name: "为数据查询选择智能体 Finance Partner" })).toBeTruthy();
  });

  it("keeps candidates readable but not choosable when the role cannot be set, with the reason", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired"));
    const candidate = {
      assignment_id: "asa_1", agent_id: "osa_1", revision_ref: "arv_1", release_id: "arl_1",
      behavior_fingerprint: `sha256:${"a".repeat(64)}`, display_name: "Finance Partner", description: "", is_primary: false,
    };
    vi.mocked(getAgentRolePreferences).mockResolvedValue({
      ...ROLE_PREFERENCES,
      roles: ROLE_PREFERENCES.roles.map((role) => role.role_id === "data"
        ? { ...role, availability: "ready" as const, candidates: [candidate, { ...candidate, assignment_id: "asa_2", display_name: "Sales Helper" }], actions: { set: false, clear: true } }
        : role),
    });
    renderIntl(<AgentManagement />);

    fireEvent.click(await screen.findByRole("button", { name: /^为数据查询选择智能体/ }));
    const menu = await screen.findByRole("menu");
    const item = within(menu).getByRole("menuitemradio", { name: "Finance Partner" });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    expect(document.getElementById(item.getAttribute("aria-describedby")!)?.textContent).toBe("这个岗位的首选暂时不能更改。刷新页面查看最新状态。");
    fireEvent.click(item);
    expect(setAgentRolePreference).not.toHaveBeenCalled();
  });

  it("explains the active relationship view and uses the server total", async () => {
    directUse.enabled = true;
    window.history.replaceState({}, "", "/agents/manage/created/");
    vi.mocked(listManagedAgents).mockResolvedValue({
      ...collection("created", [{ ...ITEM, relationship: "owned" }]),
      total_count: 8,
    });
    renderIntl(<AgentManagement />);

    expect(await screen.findByRole("heading", { name: "维护你创建的智能体" })).toBeTruthy();
    expect(screen.getByText("当前视角 8 个智能体")).toBeTruthy();
    expect(screen.getByText(/个人启停请回到“我聘用的”/)).toBeTruthy();
    expect(screen.getByText(/已加入我的列表/)).toBeTruthy();
    expect(screen.queryByText(/你主动聘用/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "开始使用" }));
    expect(directUse.handoff).toHaveBeenCalledWith({ agentId: "osa_1", label: "Finance Partner" });
  });

  it("describes an owner's existing assignment consistently in Hired and Created views", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [{ ...ITEM, relationship: "owned" }]));
    renderIntl(<AgentManagement />);

    expect(await screen.findByText(/已加入我的列表/)).toBeTruthy();
    expect(screen.queryByText(/你主动聘用/)).toBeNull();
  });

  it("does not present an active but unrunnable Agent as in use", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    const unavailable = {
      ...ITEM,
      availability: {
        ...ITEM.availability,
        runnable: false,
        reasons: ["missing_connection"],
      },
    };
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [unavailable]));
    renderIntl(<AgentManagement />);

    expect((await screen.findByText("已聘用，当前不可用")).getAttribute("data-status")).toBe("assignment-unavailable");
    expect(screen.queryByText("使用中")).toBeNull();
  });

  it("does not announce a false zero while the management view is loading", () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockReturnValue(new Promise(() => {}));
    renderIntl(<AgentManagement />);

    expect(screen.getByText("正在读取此视角…")).toBeTruthy();
    expect(screen.queryByText("当前视角 0 个智能体")).toBeNull();
  });

  it("keeps the successful local mutation when background refresh fails", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents)
      .mockResolvedValueOnce(collection("hired"))
      .mockRejectedValueOnce(new Error("network"));
    vi.mocked(setAgentPersonalStatus).mockResolvedValue({
      kind: "agent_assignment_mutation",
      schema_version: 1,
      assignment: { status: "personal_disabled", source: "user_hired", row_version: 2 },
      runnable: false,
      public_reason: "personally_disabled",
    });
    renderIntl(<AgentManagement />);

    fireEvent.click(await screen.findByRole("button", { name: "停用" }));
    expect(await screen.findByRole("button", { name: "启用" })).toBeTruthy();
    expect(await screen.findByText(/操作已成功，但最新状态暂时无法刷新/)).toBeTruthy();
    expect(screen.getByText("Finance Partner")).toBeTruthy();
  });

  it("shows a localized repair reason without rendering blocked listing copy", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    const blocked = {
      ...ITEM,
      display_name: "Listing needs review",
      display_name_key: "listing_safety_blocked_agent_name",
      summary: "Listing content is hidden.",
      summary_key: "listing_safety_blocked_agent_summary",
      listing_safety: {
        effective_status: "blocked",
        deterministic_status: "blocked",
        semantic_status: "not_run",
        listing_hash: "sha256:abc",
        reasons: ["credential_or_secret"],
      },
      availability: { discoverable: false, hireable: false, runnable: false, reasons: ["credential_or_secret"] },
      actions: { ...ITEM.actions, view_detail: false, enable: false },
    };
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [blocked]));
    renderIntl(<AgentManagement />);

    expect(await screen.findByText("智能体介绍待检查")).toBeTruthy();
    expect(screen.getByText("介绍需要处理")).toBeTruthy();
    expect(screen.getByText("介绍中包含密钥或凭据。请先删除，再分享此智能体。")).toBeTruthy();
    expect(screen.queryByText("Finance Partner")).toBeNull();
  });

  it("does not expose finding details to a non-owner with an existing assignment", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    const blocked = {
      ...ITEM,
      display_name_key: "listing_safety_blocked_agent_name",
      summary_key: "listing_safety_blocked_agent_summary",
      listing_safety: {
        effective_status: "blocked",
        deterministic_status: "blocked",
        semantic_status: "not_run",
        listing_hash: null,
        reasons: [],
      },
      availability: { discoverable: false, hireable: false, runnable: false, reasons: ["listing_safety_blocked"] },
      actions: { ...ITEM.actions, view_detail: false, enable: false, manage: false },
      manager_url: null,
    };
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired", [blocked]));
    renderIntl(<AgentManagement />);

    expect(await screen.findByText("此智能体暂不可用，需要由负责人检查介绍。")).toBeTruthy();
    expect(screen.queryByText(/密钥或凭据/)).toBeNull();
  });

  it("cannot cancel a destructive dialog after submission starts and locks background scroll", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired"));
    let completeUnhire: ((value: Awaited<ReturnType<typeof unhireAgent>>) => void) | undefined;
    vi.mocked(unhireAgent).mockReturnValue(new Promise((resolve) => {
      completeUnhire = resolve;
    }));
    renderIntl(<div className="ui-page-frame-scroll"><AgentManagement /></div>);

    await chooseRowAction("Finance Partner 的更多操作", /^解聘/);
    const dialog = await screen.findByRole("dialog", { name: /解聘 Finance Partner/ });
    expect((document.querySelector(".ui-page-frame-scroll") as HTMLElement).style.overflow).toBe("hidden");
    fireEvent.click(screen.getByRole("button", { name: "确认解聘" }));

    const cancel = screen.getByRole("button", { name: "取消" });
    expect(cancel.hasAttribute("disabled")).toBe(true);
    fireEvent.click(cancel);
    fireEvent.click(dialog.parentElement as HTMLElement);
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(screen.getByRole("navigation", { name: "智能体管理视图", hidden: true }).parentElement?.getAttribute("aria-disabled")).toBe("true");

    await act(async () => {
      completeUnhire?.({
        kind: "agent_assignment_mutation",
        schema_version: 1,
        assignment: { status: "removed", source: "user_hired", row_version: 2 },
        runnable: false,
        public_reason: "not_hired",
      });
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect((document.querySelector(".ui-page-frame-scroll") as HTMLElement).style.overflow).toBe("");
  });

  it("puts Unhire in the row menu, still confirming and returning focus to the menu", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired"));
    renderIntl(<AgentManagement />);

    const more = await screen.findByRole("button", { name: "Finance Partner 的更多操作" });
    expect(screen.queryByRole("button", { name: "解聘" })).toBeNull();
    await chooseRowAction("Finance Partner 的更多操作", /^解聘/, { danger: true });
    const dialog = await screen.findByRole("dialog", { name: /解聘 Finance Partner/ });
    expect(vi.mocked(unhireAgent)).not.toHaveBeenCalled();

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(more));
  });

  it("does not let an in-flight preview cross into another management view", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    vi.mocked(listManagedAgents).mockResolvedValue(collection("hired"));
    let completePreview: ((value: Awaited<ReturnType<typeof getAgentUnhireImpact>>) => void) | undefined;
    vi.mocked(getAgentUnhireImpact).mockReturnValue(new Promise((resolve) => {
      completePreview = resolve;
    }));
    renderIntl(<AgentManagement />);

    await chooseRowAction("Finance Partner 的更多操作", /^解聘/);
    expect(screen.getByRole("navigation", { name: "智能体管理视图" }).parentElement?.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(screen.getByRole("link", { name: "我创建的" }));

    await act(async () => {
      completePreview?.({
        kind: "agent_unhire_impact",
        schema_version: 1,
        assignment_id: "asa_1",
        assignment_row_version: 1,
        impact_token: "token",
        expires_in_seconds: 300,
        active_count: 0,
        paused_count: 0,
        running_count: 0,
        jobs: [],
      });
    });
    expect(screen.queryByRole("dialog")).toBeTruthy();
    expect(screen.getByRole("link", { name: "我聘用的", hidden: true }).getAttribute("aria-current")).toBe("page");
  });

  it("loads the next management cursor without replacing or duplicating earlier Agents", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    const second = { ...ITEM, agent_id: "osa_2", display_name: "Sales Partner" };
    vi.mocked(listManagedAgents)
      .mockResolvedValueOnce(collection("hired", [ITEM], true, "cursor-2"))
      .mockResolvedValueOnce(collection("hired", [ITEM, second]));
    renderIntl(<AgentManagement />);

    fireEvent.click(await screen.findByRole("button", { name: "加载更多智能体" }));
    expect(await screen.findByText("Sales Partner")).toBeTruthy();
    expect(screen.getAllByText("Finance Partner")).toHaveLength(1);
    expect(listManagedAgents).toHaveBeenLastCalledWith("hired", {
      cursor: "cursor-2",
      limit: 48,
    });
  });

  it("announces and focuses the first page after changing status on a later page", async () => {
    window.history.replaceState({}, "", "/agents/manage/");
    const second = { ...ITEM, agent_id: "osa_2", display_name: "Sales Partner" };
    vi.mocked(listManagedAgents)
      .mockResolvedValueOnce(collection("hired", [ITEM], true, "cursor-2"))
      .mockResolvedValueOnce(collection("hired", [second]))
      .mockResolvedValueOnce(collection("hired", [ITEM]));
    vi.mocked(setAgentPersonalStatus).mockResolvedValue({
      kind: "agent_assignment_mutation",
      schema_version: 1,
      assignment: { status: "personal_disabled", source: "user_hired", row_version: 2 },
      runnable: false,
      public_reason: "personally_disabled",
    });
    renderIntl(<AgentManagement />);

    fireEvent.click(await screen.findByRole("button", { name: "加载更多智能体" }));
    await screen.findByText("Sales Partner");
    fireEvent.click(screen.getAllByRole("button", { name: "停用" })[1]);

    const heading = await screen.findByRole("heading", { name: "我聘用的" });
    expect(await screen.findByText("已保存，列表已回到首屏。")).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(screen.queryByText("Sales Partner")).toBeNull();
  });

  it("announces and focuses the first page after changing a later team default", async () => {
    const second = { ...ITEM, agent_id: "osa_2", display_name: "Sales Partner" };
    vi.mocked(listManagedAgents)
      .mockResolvedValueOnce(collection("team", [ITEM], true, "cursor-2"))
      .mockResolvedValueOnce(collection("team", [second]))
      .mockResolvedValueOnce(collection("team", [ITEM]));
    vi.mocked(setAgentTeamDefault).mockResolvedValue({
      kind: "agent_team_default_preview",
      schema_version: 1,
      agent_id: "osa_2",
      action: "set",
      active_member_count: 2,
      assignment_count: 1,
      opted_out_count: 0,
      affected_count: 1,
      policy: null,
    });
    renderIntl(<AgentManagement />);

    fireEvent.click(await screen.findByRole("button", { name: "加载更多智能体" }));
    await screen.findByText("Sales Partner");
    fireEvent.click(screen.getAllByRole("button", { name: "设为团队默认" })[1]);
    fireEvent.click(await screen.findByRole("button", { name: "确认设为默认" }));

    const heading = await screen.findByRole("heading", { name: "团队治理" });
    expect(await screen.findByText("已保存，列表已回到首屏。")).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(screen.queryByText("Sales Partner")).toBeNull();
  });
});
