import { PRODUCT_NAME } from "@/lib/product-identity";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, expectConfirmGate, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import {
  deleteSource,
  listSources,
  listSourceTools,
  patchSource,
  previewSourceImpact,
  type ResourceChangeImpact,
  TeamToolApiError,
  type TeamToolSourcePublic,
  validateSource,
} from "@/lib/team-tool-sources-api";
import { configureTavily, createDatarcTool, listBuiltinTools, refreshDatarcScope, type TeamBuiltinTool } from "@/lib/team-builtin-tools-api";
import { applyToolSourceRefresh, previewToolSourceRefresh } from "@/lib/team-tool-source-refresh-api";

import { ToolsManager } from "./tools-manager";

vi.mock("@/lib/team-tool-sources-api", () => ({
  authorizeSource: vi.fn(),
  deleteSource: vi.fn(),
  getSourceUsage: vi.fn(),
  listSources: vi.fn(),
  listSourceTools: vi.fn(),
  patchSource: vi.fn(),
  previewSourceImpact: vi.fn(),
  validateSource: vi.fn(),
  TeamToolApiError: class TeamToolApiError extends Error {
    constructor(public status: number, message: string) {
      super(message);
    }
  },
}));
vi.mock("@/lib/team-builtin-tools-api", () => ({
  configureTavily: vi.fn(),
  createDatarcTool: vi.fn(),
  deleteDatarcTool: vi.fn(),
  listBuiltinTools: vi.fn().mockResolvedValue([]),
  refreshDatarcScope: vi.fn(),
  setDatarcEnabled: vi.fn(),
  setTavilyEnabled: vi.fn(),
  updateDatarcTool: vi.fn(),
}));
vi.mock("@/lib/team-tool-source-refresh-api", () => ({
  applyToolSourceRefresh: vi.fn(),
  previewToolSourceRefresh: vi.fn(),
}));
vi.mock("@/lib/use-connector-presets", () => ({
  useConnectorPresets: () => [],
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn() }),
}));
vi.mock("@/components/canvas/sections/create-tool-card", () => ({
  CreateToolCard: ({ onClose }: { onClose: () => void }) => (
    <div>
      <button type="button" onClick={onClose}>关闭新建工具</button>
      <input aria-label="测试字段" />
    </div>
  ),
}));

const SOURCE: TeamToolSourcePublic = {
  id: "source_1",
  team_id: "team_1",
  name: "CRM",
  base_url: "https://api.example.com",
  kind: "openapi",
  selected_op_ids: ["listCustomers", "uploadFile"],
  auth_type: "none",
  has_auth: false,
  visibility: "team_wide",
  status: "enabled",
  tools_count: 2,
  op_count: 2,
  impact_guard_enabled: true,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
};

const IMPACT: ResourceChangeImpact = {
  kind: "resource_change_impact",
  schema_version: 1,
  action: "patch",
  blocking: false,
  summary: {
    current_agent_count: 2,
    running_task_count: 1,
    draft_agent_count: 0,
    historical_agent_count: 0,
    scheduled_binding_count: 1,
    restricted_count: 0,
    completeness: "complete",
    reason_codes: [],
  },
  new_admission: "blocked_for_affected_agents",
  running_execution: "future_dispatch_blocked",
  already_sent_effect: "not_reversible",
  required_follow_up: [{ kind: "schedule_reconfirm", job_id: "job_1" }],
  impact_token: "impact-token",
  expires_in_seconds: 300,
  command: { action: "patch", proposed_change: { status: "disabled" } },
  reason_codes: [],
};

describe("ToolsManager interaction boundaries", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/team/settings/tools/");
    vi.mocked(listBuiltinTools).mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  async function renderLoadedTools() {
    vi.mocked(listSources).mockResolvedValue([{ ...SOURCE }]);
    vi.mocked(patchSource).mockResolvedValue({ ...SOURCE, status: "disabled" });
    vi.mocked(previewSourceImpact).mockResolvedValue(IMPACT);
    renderIntl(<ToolsManager />);
    expect(await screen.findByText("CRM")).toBeTruthy();
  }

  it("reveals operation matches without forcing the full source open", async () => {
    await renderLoadedTools();

    fireEvent.change(screen.getByRole("searchbox", { name: "按源名 / 工具名过滤…" }), {
      target: { value: "upload" },
    });

    expect(screen.getByText("命中 1 个操作")).toBeTruthy();
    expect(screen.getByText("uploadFile")).toBeTruthy();
    expect(screen.queryByText("listCustomers")).toBeNull();

    const expand = screen.getByRole("button", { name: "展开工具清单" });
    expect(expand.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(expand);
    expect(screen.getByRole("button", { name: "收起工具清单" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("listCustomers")).toBeTruthy();
  });

  it("describes selected operations as imported rather than claiming they are executable", async () => {
    await renderLoadedTools();

    expect(screen.getByText("已导入 2 个操作")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开工具清单" }));
    expect(screen.getByText("已导入的操作 · 2")).toBeTruthy();
  });

  it("traps new-source focus and returns it to the trigger on Escape", async () => {
    await renderLoadedTools();
    const trigger = screen.getByRole("button", { name: /添加 MCP 工具/ });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: /添加 MCP 工具/ });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    // jsdom lacks inert; floating-ui falls back to aria-hidden + data-floating-ui-inert.
    await waitFor(() => expect(trigger.closest("header")?.hasAttribute("data-floating-ui-inert")).toBe(true));

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /添加 MCP 工具/ })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("selects a built-in third-party tool before showing its configuration", async () => {
    await renderLoadedTools();

    fireEvent.click(screen.getByRole("button", { name: "＋ 添加内置第三方工具" }));
    expect(await screen.findByRole("dialog", { name: "选择内置第三方工具" })).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索工具…" }), { target: { value: "missing" } });
    expect(screen.getByText("没有匹配的内置第三方工具。")).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索工具…" }), { target: { value: "tav" } });

    fireEvent.click(screen.getByRole("button", { name: /Tavily/ }));
    expect(await screen.findByRole("dialog", { name: "接入 Tavily" })).toBeTruthy();
    expect(screen.getByLabelText("Tavily API Key")).toBeTruthy();
  });

  it("offers Datarc with name, endpoint, and connection key fields", async () => {
    await renderLoadedTools();

    fireEvent.click(screen.getByRole("button", { name: "＋ 添加内置第三方工具" }));
    fireEvent.click(screen.getByRole("button", { name: /北极九章/ }));

    expect(await screen.findByRole("dialog", { name: "接入北极九章" })).toBeTruthy();
    expect(screen.getByLabelText("配置名称")).toBeTruthy();
    expect(screen.getByLabelText("Endpoint")).toBeTruthy();
    expect(screen.getByLabelText("连接密钥")).toBeTruthy();
  });

  it("shows separate Power BI connections with the existing Power BI mark", async () => {
    vi.mocked(listSources).mockResolvedValue([]);
    const base: TeamBuiltinTool = {
      id: "pbi_one", provider: "powerbi", name: "财务", display_name: "Power BI - 财务",
      endpoint: "", key_hint: "1234", status: "enabled", configured: true,
      created_at: "t", updated_at: "t", scope_state: "unknown", scope: {}, scope_checked_at: null,
      supported_tools: ["pbi_execute_dax"], workspace_id: "workspace_1", dataset_id: "dataset_1",
    };
    vi.mocked(listBuiltinTools).mockResolvedValue([base, { ...base, id: "pbi_two", name: "销售", display_name: "Power BI - 销售" }]);
    renderIntl(<ToolsManager />);
    const first = await screen.findByText("Power BI - 财务");
    expect(screen.getByText("Power BI - 销售")).toBeTruthy();
    expect(first.closest(".legacy-tools-pbi-row")?.querySelector("span[aria-hidden='true']")?.getAttribute("style")).toContain("rgb(242, 200, 17)");
    fireEvent.click(screen.getByRole("button", { name: "＋ 添加内置第三方工具" }));
    expect(screen.getByRole("button", { name: /Power BI/ })).toBeTruthy();
  });

  it("shows a Tavily save failure inside the dialog in plain words, not as a page load failure", async () => {
    await renderLoadedTools();
    vi.mocked(configureTavily).mockRejectedValueOnce(Object.assign(new Error("tavily_api_key_invalid"), { status: 422 }));

    fireEvent.click(screen.getByRole("button", { name: "＋ 添加内置第三方工具" }));
    fireEvent.click(screen.getByRole("button", { name: /Tavily/ }));
    const dialog = await screen.findByRole("dialog", { name: "接入 Tavily" });
    fireEvent.change(screen.getByLabelText("Tavily API Key"), { target: { value: "tvly-wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并保存" }));

    await waitFor(() => expect(dialog.textContent).toContain("Tavily 未接受这个 API Key，请检查后重新粘贴。"));
    expect(screen.getByRole("dialog", { name: "接入 Tavily" })).toBeTruthy();
    expect(document.body.textContent).not.toContain("tavily_api_key_invalid");
    expect(document.body.textContent).not.toContain("加载失败");
  });

  it("maps Datarc save failures to plain words and falls back for unknown codes", async () => {
    await renderLoadedTools();
    vi.mocked(createDatarcTool)
      .mockRejectedValueOnce(Object.assign(new Error("datarc_endpoint_rejected"), { status: 422 }))
      .mockRejectedValueOnce(Object.assign(new Error("some_new_backend_code"), { status: 500 }));

    fireEvent.click(screen.getByRole("button", { name: "＋ 添加内置第三方工具" }));
    fireEvent.click(screen.getByRole("button", { name: /北极九章/ }));
    const dialog = await screen.findByRole("dialog", { name: "接入北极九章" });
    fireEvent.change(screen.getByLabelText("配置名称"), { target: { value: "财务" } });
    fireEvent.change(screen.getByLabelText("Endpoint"), { target: { value: "http://10.0.0.1" } });
    fireEvent.change(screen.getByLabelText("连接密钥"), { target: { value: "key" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并保存" }));

    await waitFor(() => expect(dialog.textContent).toContain("无法使用这个 Endpoint"));
    expect(document.body.textContent).not.toContain("datarc_endpoint_rejected");

    fireEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    await waitFor(() => expect(dialog.textContent).toContain("保存失败，请检查填写内容后重试。"));
    expect(dialog.textContent).not.toContain("无法使用这个 Endpoint");
    expect(document.body.textContent).not.toContain("some_new_backend_code");
    expect(document.body.textContent).not.toContain("加载失败");
  });

  it("maps new-format Datarc errors by code, not by the generic message", async () => {
    // Real production shape: backend returns {detail:{code}}; readApiError replaces message with generic copy, retaining code in .code.
    // Previous case put code in message, accidentally bypassing this gap.
    await renderLoadedTools();
    vi.mocked(createDatarcTool)
      .mockRejectedValueOnce(Object.assign(new Error("操作失败，请稍后重试。"), { status: 422, code: "datarc_connection_key_invalid" }))
      .mockRejectedValueOnce(Object.assign(new Error("操作失败，请稍后重试。"), { status: 422, code: "datarc_endpoint_unsupported" }));

    fireEvent.click(screen.getByRole("button", { name: "＋ 添加内置第三方工具" }));
    fireEvent.click(screen.getByRole("button", { name: /北极九章/ }));
    const dialog = await screen.findByRole("dialog", { name: "接入北极九章" });
    fireEvent.change(screen.getByLabelText("配置名称"), { target: { value: "财务" } });
    fireEvent.change(screen.getByLabelText("Endpoint"), { target: { value: "https://demo.datarc.cn" } });
    fireEvent.change(screen.getByLabelText("连接密钥"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    await waitFor(() => expect(dialog.textContent).not.toContain("保存失败，请检查填写内容后重试。"));
    await waitFor(() => expect(dialog.textContent).toMatch(/密钥/));

    fireEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    await waitFor(() => expect(dialog.textContent).toContain("这个地址不是北极九章开放接口"));
    expect(document.body.textContent).not.toContain("datarc_endpoint_unsupported");
  });

  it("shows provider icons on configured built-in tools", async () => {
    const base: Omit<TeamBuiltinTool, "id" | "provider" | "name" | "display_name" | "endpoint"> = {
      key_hint: null,
      status: "enabled",
      configured: true,
      created_at: "2026-09-23T00:00:00Z",
      updated_at: "2026-09-23T00:00:00Z",
      scope_state: "unknown",
      scope: {},
      scope_checked_at: null,
      supported_tools: [],
    };
    vi.mocked(listSources).mockResolvedValue([]);
    vi.mocked(listBuiltinTools).mockResolvedValue([
      { ...base, id: "tavily_1", provider: "tavily", name: "Tavily", display_name: "Tavily", endpoint: "https://api.tavily.com" },
      { ...base, id: "datarc_1", provider: "datarc", name: "财务", display_name: "北极九章 - 财务", endpoint: "https://openapi.datarc.cn" },
    ]);

    const { container } = renderIntl(<ToolsManager />);
    await screen.findByText("北极九章 - 财务");

    expect(container.querySelector("svg.bh-tavily-mark")).toBeTruthy();
    expect(container.querySelector('[data-source-icon="datarc"]')).toBeTruthy();
  });

  it("presents a verified Datarc key scope as scannable groups", async () => {
    const tool: TeamBuiltinTool = {
      id: "datarc_1",
      provider: "datarc",
      name: "财务",
      display_name: "北极九章 - 财务",
      endpoint: "https://openapi.datarc.cn",
      key_hint: "1234",
      status: "enabled",
      configured: true,
      created_at: "2026-09-23T00:00:00Z",
      updated_at: "2026-09-23T00:00:00Z",
      scope_state: "verified",
      scope: {
        permissions: ["account", "workspace", "data", "task"],
        workspaces: [{ id: "1", name: "财务" }, { id: "2", name: "销售" }],
        expired_at: "2026-12-22T23:59:59+08:00",
      },
      scope_checked_at: "2026-09-23T07:10:23+08:00",
      supported_tools: ["datarc_workspaces", "datarc_schema", "datarc_query"],
    };
    vi.mocked(listSources).mockResolvedValue([]);
    vi.mocked(listBuiltinTools).mockResolvedValue([tool]);
    vi.mocked(refreshDatarcScope).mockResolvedValue(tool);
    renderIntl(<ToolsManager />);
    await screen.findByText("北极九章 - 财务");

    await chooseRowAction("北极九章 - 财务 的更多操作", "编辑");

    expect(await screen.findByText("范围已验证")).toBeTruthy();
    expect(screen.getByText("接口权限")).toBeTruthy();
    expect(screen.getByText("可提供工具")).toBeTruthy();
    expect(screen.getByText("允许的北极九章工作区 · 2 个")).toBeTruthy();
    expect(screen.getByText("北极九章工作区查看")).toBeTruthy();
    expect(screen.getByText("数据结构查看")).toBeTruthy();
    expect(screen.getByText("数据查询")).toBeTruthy();
  });

  it("previews disable impact, confirms it, and returns focus to the source switch", async () => {
    await renderLoadedTools();
    const toggle = screen.getByRole("switch", { name: "启停工具源 CRM" });
    toggle.focus();
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(previewSourceImpact).toHaveBeenCalledWith("source_1", {
        action: "patch",
        proposed_change: { status: "disabled" },
      }),
    );
    expect(await screen.findByText("2 个当前智能体")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认停用" }));

    await waitFor(() =>
      expect(patchSource).toHaveBeenCalledWith(
        "source_1",
        { status: "disabled" },
        expect.objectContaining({ impactToken: "impact-token" }),
      ),
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("switch", { name: "启停工具源 CRM" }),
      ),
    );
  });

  it("previews the impact before removing a source's credentials and patches with the token", async () => {
    // With the Impact Guard on, the server answers 412 to a credentials→none patch without a token.
    vi.mocked(listSources).mockResolvedValue([{ ...SOURCE, auth_type: "bearer", has_auth: true }]);
    vi.mocked(patchSource).mockResolvedValue({ ...SOURCE });
    vi.mocked(previewSourceImpact).mockResolvedValue({
      ...IMPACT,
      command: { action: "patch", proposed_change: { auth_type: "none" } },
    });
    renderIntl(<ToolsManager />);
    expect(await screen.findByText("CRM")).toBeTruthy();
    await chooseRowAction("CRM 的更多操作", "编辑");
    const dialog = await screen.findByRole("dialog", { name: "编辑源「CRM」" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "更换凭据" }));
    fireEvent.change(within(dialog).getByLabelText("认证方式"), { target: { value: "none" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(previewSourceImpact).toHaveBeenCalledWith("source_1", {
        action: "patch",
        proposed_change: { auth_type: "none", auth: null },
      }),
    );
    expect(patchSource).not.toHaveBeenCalled();
    expect(await screen.findByText("2 个当前智能体")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "移除凭据" }));
    await waitFor(() =>
      expect(patchSource).toHaveBeenCalledWith(
        "source_1",
        { auth_type: "none", auth: null },
        expect.objectContaining({ impactToken: "impact-token" }),
      ),
    );
  });

  it("does not delete a source until the server-computed impact is confirmed", async () => {
    await renderLoadedTools();
    vi.mocked(previewSourceImpact).mockResolvedValue({ ...IMPACT, action: "delete" });
    vi.mocked(deleteSource).mockResolvedValue(undefined);

    await chooseRowAction("CRM 的更多操作", "删除…");
    expect(await screen.findByText(/将从整个团队删除这个来源、凭据/)).toBeTruthy();
    expect(deleteSource).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await waitFor(() =>
      expect(deleteSource).toHaveBeenCalledWith(
        "source_1",
        expect.objectContaining({ impactToken: "impact-token" }),
      ),
    );
  });

  it("refreshes the impact instead of retrying a stale confirmation", async () => {
    await renderLoadedTools();
    vi.mocked(previewSourceImpact)
      .mockResolvedValueOnce(IMPACT)
      .mockResolvedValue({
        ...IMPACT,
        impact_token: "fresh-token",
        summary: { ...IMPACT.summary, current_agent_count: 3 },
      });
    vi.mocked(patchSource).mockRejectedValueOnce(
      new TeamToolApiError(409, "resource_impact_stale"),
    );

    fireEvent.click(screen.getByRole("switch", { name: "启停工具源 CRM" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认停用" }));

    await waitFor(() => expect(previewSourceImpact).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("3 个当前智能体")).toBeTruthy();
    expect(screen.getByText(/影响范围刚刚发生变化/)).toBeTruthy();
    expect(patchSource).toHaveBeenCalledTimes(1);
  });

  it("keeps the blocking reason when a stale delete becomes unsafe", async () => {
    await renderLoadedTools();
    vi.mocked(previewSourceImpact)
      .mockResolvedValueOnce({ ...IMPACT, action: "delete" })
      .mockResolvedValue({
        ...IMPACT,
        action: "delete",
        blocking: true,
        impact_token: null,
        reason_codes: ["resource_delete_has_live_dependencies"],
      });
    vi.mocked(deleteSource).mockRejectedValueOnce(
      new TeamToolApiError(409, "resource_impact_stale"),
    );

    await chooseRowAction("CRM 的更多操作", "删除…");
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));

    await waitFor(() => expect(previewSourceImpact).toHaveBeenCalledTimes(2));
    expect(screen.getByText(/仍有当前智能体、运行中任务或草稿依赖/)).toBeTruthy();
    expect(screen.queryByText(/影响范围刚刚发生变化/)).toBeNull();
  });

  it("restores legacy mutations when the server disables the impact guard", async () => {
    vi.mocked(listSources).mockResolvedValue([{ ...SOURCE, impact_guard_enabled: false }]);
    vi.mocked(patchSource).mockResolvedValue({ ...SOURCE, status: "disabled" });
    vi.mocked(deleteSource).mockResolvedValue(undefined);
    renderIntl(<ToolsManager />);
    expect(await screen.findByText("CRM")).toBeTruthy();

    fireEvent.click(screen.getByRole("switch", { name: "启停工具源 CRM" }));
    await waitFor(() =>
      expect(patchSource).toHaveBeenCalledWith("source_1", { status: "disabled" }),
    );
    expect(previewSourceImpact).not.toHaveBeenCalled();

    await chooseRowAction("CRM 的更多操作", "删除…");
    expect(await screen.findByText(/已导入操作和已保存凭据/)).toBeTruthy();
    expect(deleteSource).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await waitFor(() => expect(deleteSource).toHaveBeenCalledWith("source_1"));
    expect(previewSourceImpact).not.toHaveBeenCalled();
  });

  it("preserves Agent repair context and filters to the missing operation", async () => {
    window.history.replaceState({}, "", "/team/settings/tools/?from_agent=osa_1&dependency=uploadFile");
    await renderLoadedTools();

    expect(screen.getByText("正在筛选必要依赖“uploadFile”相关的 Tool。")).toBeTruthy();
    expect(screen.getByRole("searchbox", { name: "按源名 / 工具名过滤…" }).getAttribute("value")).toBe("uploadFile");
    expect(screen.getByText("命中 1 个操作")).toBeTruthy();
    expect(screen.getByRole("link", { name: /返回智能体详情/ }).getAttribute("href")).toContain("id=osa_1");
  });

  // ─── Built-in capabilities (capability-surface-audit step 2) ────────────────────
  it("陈列系统内置能力，并跟着搜索框一起过滤", async () => {
    await renderLoadedTools();

    // Unfiltered: heading + total count.
    expect(screen.getByTestId("builtin-capabilities")).toBeTruthy();
    expect(screen.getByText(new RegExp(`${PRODUCT_NAME} 自带的 \\d+ 项能力`))).toBeTruthy();
    expect(screen.getByText("搜索知识库")).toBeTruthy();
    expect(screen.getByText("查询数据")).toBeTruthy();

    // Filtered: matches only, count becomes Matching N of M.
    // 116 dogfood screenshot, 2026-09-09: only 4 cards remained but subtitle still said “自带的 33 项能力”, appearing miscounted.
    fireEvent.change(screen.getByRole("searchbox", { name: "按源名 / 工具名过滤…" }), {
      target: { value: "知识库" },
    });
    expect(screen.getByText(/匹配 \d+ 项，共 \d+ 项内置能力/)).toBeTruthy();
    expect(screen.queryByText(new RegExp(`${PRODUCT_NAME} 自带的 \\d+ 项能力`))).toBeNull();
    expect(screen.getByText("搜索知识库")).toBeTruthy();
    expect(screen.queryByText("查询数据")).toBeNull();
  });
});

describe("ToolsManager 卡片「⋯」（外观段第四批）", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/team/settings/tools/");
    vi.mocked(listBuiltinTools).mockResolvedValue([]);
    vi.mocked(listSources).mockResolvedValue([{ ...SOURCE, impact_guard_enabled: false }]);
    vi.mocked(deleteSource).mockResolvedValue(undefined as never);
    vi.mocked(validateSource).mockResolvedValue({ reachable: true } as never);
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("offers Validate, Edit and Delete in the menu; Validate validates and Delete confirms first", async () => {
    renderIntl(<ToolsManager />);
    expect(await screen.findByText("CRM")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "验证" })).toBeNull();
    expect(menuItemNames(await openRowMenu("CRM 的更多操作"))).toEqual(["验证", "编辑", "删除…"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "验证" }));
    await waitFor(() => expect(validateSource).toHaveBeenCalledWith("source_1"));
    await chooseRowAction("CRM 的更多操作", "删除…", { danger: true });
    await expectConfirmGate({
      action: vi.mocked(deleteSource),
      dialogName: "删除源「CRM」？",
      confirmName: "删除",
      reopen: () => chooseRowAction("CRM 的更多操作", "删除…"),
    });
    await waitFor(() => expect(deleteSource).toHaveBeenCalled());
  });
});

describe("ToolsManager add buttons", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders ＋ before both add buttons and names the dialog without it", async () => {
    vi.mocked(listSources).mockResolvedValue([{ ...SOURCE }]);
    renderIntl(<ToolsManager />);
    await screen.findByText("CRM");
    const source = catalogText("zh-CN", "settings.tools.new_source");
    const builtin = catalogText("zh-CN", "settings.tools.new_builtin");
    const sourceButton = screen.getByRole("button", { name: (name) => name.endsWith(source) });
    expectRefreshedGlyph(sourceButton, "＋", "start", source);
    expectRefreshedGlyph(screen.getByRole("button", { name: (name) => name.endsWith(builtin) }), "＋", "start", builtin);

    fireEvent.click(sourceButton);
    expect(await screen.findByRole("dialog", { name: catalogText("zh-CN", "settings.tools.new_source_dialog_label") })).toBeTruthy();
  });
});

describe("ToolsManager MCP tool-list refresh", () => {
  const MCP: TeamToolSourcePublic = {
    ...SOURCE,
    id: "ots_mcp",
    name: "Remote MCP",
    kind: "mcp",
    base_url: "https://mcp.example.com/mcp",
    selected_op_ids: ["keep"],
    tools_count: 1,
    op_count: 1,
  };
  const UNAUTHORIZED_OAUTH: TeamToolSourcePublic = {
    ...MCP,
    id: "ots_oauth",
    name: "OAuth MCP",
    auth_type: "oauth",
    tools_count: 0,
    op_count: 0,
    selected_op_ids: [],
  };

  beforeEach(() => {
    window.history.replaceState({}, "", "/team/settings/tools/");
    vi.mocked(listBuiltinTools).mockResolvedValue([]);
    vi.mocked(listSources).mockResolvedValue([{ ...MCP }, { ...SOURCE }, { ...UNAUTHORIZED_OAUTH }]);
    vi.mocked(previewToolSourceRefresh).mockResolvedValue({
      kind: "mcp",
      added: ["search_docs"],
      removed: [],
      selected_dropped: [],
      tools_count: 2,
      prompts_added: [],
      prompts_removed: [],
      prompts_count: 0,
      candidate_digest: `sha256:${"b".repeat(64)}`,
      impact_token: "impact-token",
      expires_in_seconds: 300,
      summary: IMPACT.summary,
      required_follow_up: [],
    });
    vi.mocked(applyToolSourceRefresh).mockResolvedValue({
      kind: "mcp",
      added: ["search_docs"],
      removed: [],
      selected_dropped: [],
      tools_count: 2,
      prompts_added: [],
      prompts_removed: [],
      prompts_count: 0,
      paused_schedule_ids: [],
    });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("offers Refresh Tool List only in the menu of an MCP source that can list tools", async () => {
    renderIntl(<ToolsManager />);
    expect(await screen.findByText("Remote MCP")).toBeTruthy();

    expect(menuItemNames(await openRowMenu("Remote MCP 的更多操作"))).toEqual(["验证", "刷新工具列表", "选择工具", "编辑", "删除…"]);
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    const openApiItems = menuItemNames(await openRowMenu("CRM 的更多操作"));
    expect(openApiItems).not.toContain("刷新工具列表");
    expect(openApiItems).not.toContain("选择工具");
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    const oauthItems = menuItemNames(await openRowMenu("OAuth MCP 的更多操作"));
    expect(oauthItems).not.toContain("刷新工具列表");
    expect(oauthItems).not.toContain("选择工具");
  });

  it("the menu item opens the preview and focus returns to the menu trigger on cancel", async () => {
    renderIntl(<ToolsManager />);
    await screen.findByText("Remote MCP");

    await chooseRowAction("Remote MCP 的更多操作", "刷新工具列表", { danger: false });
    const dialog = await screen.findByRole("dialog", { name: "刷新源「Remote MCP」的工具列表" });
    expect(await within(dialog).findByText("search_docs")).toBeTruthy();
    expect(previewToolSourceRefresh).toHaveBeenCalledWith("ots_mcp");

    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(applyToolSourceRefresh).not.toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Remote MCP 的更多操作" })));
  });
});

describe("ToolsManager Choose Tools", () => {
  const MCP: TeamToolSourcePublic = {
    ...SOURCE,
    id: "ots_mcp",
    name: "Remote MCP",
    kind: "mcp",
    base_url: "https://mcp.example.com/mcp",
    selected_op_ids: ["keep"],
    tools_count: 2,
    op_count: 1,
  };
  const CATALOG = {
    kind: "mcp",
    imports_all: false,
    updated_at: "2026-09-05T00:00:00Z",
    items: [
      { operation_id: "keep", method: "MCP", path: "", summary: "Keep me", is_write: false, approval_executable: null, tags: [], imported: true },
      { operation_id: "search_docs", method: "MCP", path: "", summary: "Search docs", is_write: false, approval_executable: null, tags: [], imported: false },
    ],
  };

  beforeEach(() => {
    window.history.replaceState({}, "", "/team/settings/tools/");
    vi.mocked(listBuiltinTools).mockResolvedValue([]);
    vi.mocked(listSources).mockResolvedValue([{ ...MCP }]);
    vi.mocked(listSourceTools).mockResolvedValue(CATALOG);
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("the menu item opens the selection with the imported tools checked and saves an added tool", async () => {
    vi.mocked(patchSource).mockResolvedValue({ ...MCP, selected_op_ids: ["keep", "search_docs"], op_count: 2 });
    renderIntl(<ToolsManager />);
    await screen.findByText("Remote MCP");

    await chooseRowAction("Remote MCP 的更多操作", "选择工具", { danger: false });
    const dialog = await screen.findByRole("dialog", { name: "选择源「Remote MCP」导入的工具" });
    expect(await within(dialog).findByRole("checkbox", { name: /keep/ })).toBeChecked();
    expect(within(dialog).getByRole("checkbox", { name: /search_docs/ })).not.toBeChecked();
    expect(listSourceTools).toHaveBeenCalledWith("ots_mcp");

    fireEvent.click(within(dialog).getByRole("checkbox", { name: /search_docs/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    expect(await screen.findByText("已保存源「Remote MCP」的工具选择：导入 2 个工具。")).toBeTruthy();
    expect(patchSource).toHaveBeenCalledWith("ots_mcp", { selected_op_ids: ["keep", "search_docs"] });
    expect(previewSourceImpact).not.toHaveBeenCalled();
    await waitFor(() => expect(listSources).toHaveBeenCalledTimes(2));
  });

  it("offers Choose Tools after a refresh adds tools to a source that imports a subset", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue({
      kind: "mcp",
      added: ["search_docs"],
      removed: [],
      changed: [],
      selected_dropped: [],
      tools_count: 2,
      prompts_added: [],
      prompts_removed: [],
      prompts_count: 0,
      candidate_digest: `sha256:${"c".repeat(64)}`,
      impact_token: "impact-token",
      expires_in_seconds: 300,
      summary: IMPACT.summary,
      required_follow_up: [],
    });
    vi.mocked(applyToolSourceRefresh).mockResolvedValue({
      kind: "mcp",
      added: ["search_docs"],
      removed: [],
      changed: [{ name: "keep", fields: ["description"], imported: true }],
      selected_dropped: [],
      tools_count: 2,
      prompts_added: [],
      prompts_removed: [],
      prompts_count: 0,
      paused_schedule_ids: [],
    });
    renderIntl(<ToolsManager />);
    await screen.findByText("Remote MCP");

    await chooseRowAction("Remote MCP 的更多操作", "刷新工具列表");
    const refreshDialog = await screen.findByRole("dialog", { name: "刷新源「Remote MCP」的工具列表" });
    await within(refreshDialog).findByText("search_docs");
    fireEvent.click(within(refreshDialog).getByRole("button", { name: "更新工具列表" }));

    const notice = await screen.findByText(/源「Remote MCP」的工具列表已更新/);
    expect(notice).toHaveTextContent("1 个工具有变化。");
    expect(notice).toHaveTextContent("新增工具尚未导入。");
    fireEvent.click(within(notice).getByRole("button", { name: "选择工具" }));

    const dialog = await screen.findByRole("dialog", { name: "选择源「Remote MCP」导入的工具" });
    const added = await within(dialog).findByRole("checkbox", { name: /search_docs/ });
    expect(added.closest("li")).toHaveTextContent("新增");
    expect(within(dialog).getByRole("checkbox", { name: /keep/ }).closest("li")).not.toHaveTextContent("新增");
  });

  it("does not offer the shortcut when the source already imports every tool", async () => {
    vi.mocked(listSources).mockResolvedValue([{ ...MCP, selected_op_ids: [] }]);
    vi.mocked(previewToolSourceRefresh).mockResolvedValue({
      kind: "mcp",
      added: ["search_docs"],
      removed: [],
      changed: [],
      selected_dropped: [],
      tools_count: 2,
      prompts_added: [],
      prompts_removed: [],
      prompts_count: 0,
      candidate_digest: `sha256:${"d".repeat(64)}`,
      impact_token: "impact-token",
      expires_in_seconds: 300,
      summary: IMPACT.summary,
      required_follow_up: [],
    });
    vi.mocked(applyToolSourceRefresh).mockResolvedValue({
      kind: "mcp",
      added: ["search_docs"],
      removed: [],
      changed: [],
      selected_dropped: [],
      tools_count: 2,
      prompts_added: [],
      prompts_removed: [],
      prompts_count: 0,
      paused_schedule_ids: [],
    });
    renderIntl(<ToolsManager />);
    await screen.findByText("Remote MCP");

    await chooseRowAction("Remote MCP 的更多操作", "刷新工具列表");
    const refreshDialog = await screen.findByRole("dialog", { name: "刷新源「Remote MCP」的工具列表" });
    await within(refreshDialog).findByText("search_docs");
    fireEvent.click(within(refreshDialog).getByRole("button", { name: "更新工具列表" }));

    const notice = await screen.findByText(/源「Remote MCP」的工具列表已更新/);
    expect(notice).not.toHaveTextContent("新增工具尚未导入。");
    expect(within(notice).queryByRole("button", { name: "选择工具" })).toBeNull();
  });
});
