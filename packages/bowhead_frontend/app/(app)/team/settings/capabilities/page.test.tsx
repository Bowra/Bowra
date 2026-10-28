import type { ReactNode } from "react";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { getAgentAuthoringCatalog, type AgentAuthoringCatalog } from "@/lib/agents-api";
import { createTeamPlugin, deleteTeamPlugin, listTeamPlugins, type TeamPlugin } from "@/lib/team-plugins-api";

import TeamPluginsPage from "./page";

vi.mock("@/components/settings/settings-shell", () => ({
  SettingsShell: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/lib/agents-api", () => ({ getAgentAuthoringCatalog: vi.fn() }));
vi.mock("@/lib/team-plugins-api", () => ({
  listTeamPlugins: vi.fn(),
  createTeamPlugin: vi.fn(),
  updateTeamPlugin: vi.fn(),
  setTeamPluginEnabled: vi.fn(),
  deleteTeamPlugin: vi.fn(),
}));

const catalog: AgentAuthoringCatalog = {
  kind: "agent_authoring_catalog", schema_version: 2, fingerprint: "test",
  behavior_catalog_fingerprint: "test", risk_registry_version: "test",
  model_policy: { allowed_roles: [], effective_role: "", locked: false, policy_version: "test" },
  primary_capabilities: [], tools: [], skills: [],
};

function makePlugin(id: string): TeamPlugin {
  return {
    id, team_id: "team_1", name: "资料研究", description: "常用研究资源",
    status: "active", revision: 1, row_version: 1, content_hash: "test",
    tool_refs: ["web_search"], skill_grants: [], agent_count: 2,
  };
}

describe("TeamPluginsPage", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("shows an empty state before opening the creation form", async () => {
    vi.mocked(listTeamPlugins).mockResolvedValue([]);
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue(catalog);

    renderIntl(<TeamPluginsPage />);

    expect(await screen.findByText("当前没有插件")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "新建插件" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "新建插件" }));
    expect(screen.getByRole("heading", { name: "新建插件" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByText("当前没有插件")).toBeTruthy();
  });

  it("shows existing plugins without opening the editor", async () => {
    vi.mocked(listTeamPlugins).mockResolvedValue([makePlugin("tcap_1")]);
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue(catalog);

    renderIntl(<TeamPluginsPage />);

    const plugin = await screen.findByRole("button", { name: "资料研究" });
    expect(screen.queryByRole("heading", { name: "编辑插件" })).toBeNull();
    fireEvent.click(plugin);
    expect(screen.getByRole("heading", { name: "编辑插件" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByText("常用研究资源")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "编辑插件" })).toBeNull();
  });

  it("returns to the list after creating a plugin", async () => {
    const thirdPartyRef = "teammcp.ibt_sales.datarc_query";
    const created = { ...makePlugin("tcap_2"), tool_refs: [thirdPartyRef] };
    vi.mocked(listTeamPlugins).mockResolvedValueOnce([]).mockResolvedValueOnce([created]);
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({
      ...catalog,
      tools: [{
        stable_ref: "web_search", display_name: "网页搜索", description: "搜索网页",
        source_kind: "native", source_display_name: null, capability_classes: [],
        risk: "read", selectable: true, locked_reason: null, approval_required: false,
        confirmation_summary: null,
      }, {
        stable_ref: thirdPartyRef, display_name: "北极九章数据查询", description: "查询业务数据",
        source_kind: "team_mcp", source_display_name: "北极九章 - 销售数据", connection_id: "ibt_sales",
        capability_classes: [], risk: "read", selectable: true, locked_reason: null,
        approval_required: false, confirmation_summary: null,
      }],
    });
    vi.mocked(createTeamPlugin).mockResolvedValue(created);

    renderIntl(<TeamPluginsPage />);

    expect(await screen.findByText("当前没有插件")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "新建插件" }));
    fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: "资料研究" } });
    const addedTools = screen.getByRole("group", { name: "已添加的工具" });
    expect(within(addedTools).getByText("北极九章 - 销售数据")).toBeTruthy();
    fireEvent.click(within(addedTools).getByRole("checkbox", { name: /北极九章数据查询/ }));
    expect(within(screen.getByRole("group", { name: "系统工具" })).getByRole("checkbox", { name: /网页搜索/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "保存插件" }));

    expect(await screen.findByRole("button", { name: "资料研究" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "新建插件" })).toBeNull();
    expect(createTeamPlugin).toHaveBeenCalledWith({
      name: "资料研究", description: "", tool_refs: [thirdPartyRef], skill_grants: [],
    });
  });

  it("confirms permanent deletion and reloads the list", async () => {
    vi.mocked(listTeamPlugins).mockResolvedValueOnce([makePlugin("tcap_1")]).mockResolvedValueOnce([]);
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue(catalog);
    vi.mocked(deleteTeamPlugin).mockResolvedValue();
    renderIntl(<TeamPluginsPage />);
    expect(await screen.findByText("资料研究")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(screen.getByText(/2 个智能体将立即失去/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(deleteTeamPlugin).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("当前没有插件")).toBeTruthy();
    expect(deleteTeamPlugin).toHaveBeenCalledWith("tcap_1", 1);
  });
});
