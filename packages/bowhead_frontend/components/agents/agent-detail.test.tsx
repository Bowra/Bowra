import { cleanup, render, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import { forkAgent, getAgentCapabilityReport, getAgentDetail, hireAgent, previewAgentFork } from "@/lib/agents-api";
import { AgentDetail } from "./agent-detail";

const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
const directUse = vi.hoisted(() => ({ enabled: false, handoff: vi.fn() }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => ({ me: { capabilities: { composer: { agent_start_use: directUse.enabled } } } }) }));
vi.mock("@/lib/composer-handoff", () => ({ handoffAgentToNewChat: (...args: unknown[]) => directUse.handoff(...args) }));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => navigation,
}));
vi.mock("@/lib/agents-api", () => ({
  forkAgent: vi.fn(),
  getAgentCapabilityReport: vi.fn(),
  getAgentDetail: vi.fn(),
  hireAgent: vi.fn(),
  previewAgentFork: vi.fn(),
  setAgentPersonalStatus: vi.fn(),
}));

const ITEM = {
  kind: "agent_consumer_detail" as const,
  schema_version: 1 as const,
  agent_id: "osa_1",
  display_name: "Finance Partner",
  summary: "Explains monthly performance.",
  source: { kind: "team" as const, owner_label: "Avery", owner_user_id: null },
  facets: { domains: ["finance"], jobs: ["analysis"], outputs: [] },
  capabilities: [{ id: "cap_data_query", label_key: "cap_data_query", label: "Query data", category: "data" }],
  risk: { level: "low", reasons: [] },
  safety: { status: "pass", checked_subject: "legacy_row", reasons: [] },
  listing_safety: {
    effective_status: "pass",
    deterministic_status: "pass",
    semantic_status: "not_run",
    listing_hash: "sha256:abc",
    reasons: [],
  },
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
    create: false,
  },
  action_reasons: {},
  relationship: "team",
  updated_label: { kind: "saved_at", at: "2026-08-30T00:00:00Z" },
  manager_url: null,
};

const CAPABILITY_REPORT = {
  kind: "agent_capability_report" as const,
  schema_version: 2 as const,
  compiler_version: "agent-capability-drift-v1",
  locale: "zh-CN",
  agent: { id: "osa_1", source_kind: "team" as const, source_version: "legacy-row:1", runtime_profile: "marketplace" as const },
  verification_status: "derived_unverified" as const,
  capabilities: [{
    id: "cap_data_query",
    definition_version: 1,
    compatibility_major: 1,
    definition_status: "active",
    definition: { lifecycle: "active" as const, version: 1 },
    category: "data",
    listing_revision: 1,
    name: "查询与分析数据",
    description: "查询获授权的业务数据，并返回带可追溯上下文的结构化数据集。",
    task_examples: ["比较本季度和上季度的收入。"],
    parameters_schema: { type: "object" },
    inputs: [{ contract: "dataset.request.v1", required: true }],
    outputs: [{ contract: "dataset.v1", required: true }],
    implementation_evidence: {
      status: "shadow" as const,
      reviewed_at: null,
      evidence_level: "derived_unverified" as const,
    },
    readiness: {
      state: "unavailable" as const,
      checked_at: "2026-09-07T10:00:00Z",
      reason_codes: ["not_hired"],
    },
    verification_status: "derived_unverified" as const,
    availability: "degraded" as const,
    evidence_reasons: ["legacy_dependency_match"],
  }],
  legacy_direct_dependencies: { status: "derived_unverified" as const, tool_count: 1, skill_count: 0 },
  drift: {
    status: "legacy_unverified" as const,
    listing_vs_manifest: { status: "aligned" as const, listing_without_evidence: [], dependency_without_listing: [] },
    capability_vs_dependency: { status: "aligned" as const, reason_codes: [] },
    prompt_duplication: { status: "clear" as const, reason_codes: [] },
    implicit_team_tool_overlay: { status: "clear" as const, reason_codes: [] },
    unverified_outputs: { status: "clear" as const, formats: [] },
  },
};

describe("AgentDetail", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/agents/detail/?id=osa_1");
    vi.mocked(getAgentDetail).mockResolvedValue(ITEM);
    vi.mocked(getAgentCapabilityReport).mockResolvedValue(CAPABILITY_REPORT);
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
    directUse.enabled = false;
  });

  it.each([
    [404, "找不到这个智能体，或你已没有查看权限"],
    [403, "你当前没有查看这个智能体的权限。"],
    [500, "暂时无法加载这个智能体，请稍后重试。"],
  ])("distinguishes HTTP %s from an empty or revoked detail", async (status, expected) => {
    vi.mocked(getAgentDetail).mockRejectedValueOnce(new ApiError(status, "raw"));
    renderIntl(<AgentDetail />);

    expect(await screen.findByRole("heading", { name: expected })).toBeTruthy();
    if (status === 500) expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("keeps the successful Hire mutation if detail revalidation fails", async () => {
    vi.mocked(getAgentDetail)
      .mockResolvedValueOnce(ITEM)
      .mockRejectedValueOnce(new Error("network"));
    renderIntl(<AgentDetail />);

    fireEvent.click(await screen.findByRole("button", { name: "聘用这个智能体" }));
    await waitFor(() => expect(getAgentDetail).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "停用个人使用" })).toBeTruthy();
    expect(screen.getByText(/操作已成功，但最新状态暂时无法刷新/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Finance Partner" })).toBeTruthy();
    expect(screen.getByText("已加入“我的智能体团队”并启用")).toBeTruthy();
    expect(screen.getByText(/主控智能体会在任务与能力匹配时考虑这个智能体/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "管理已聘用智能体" })).toBeTruthy();
  });

  it("explains a known capability with concrete inputs, outputs, and limits", async () => {
    renderIntl(<AgentDetail />);

    expect(await screen.findByText(/查询获授权的业务数据/)).toBeTruthy();
    expect(screen.getByText("能力定义已登记，实现证据仍待复核")).toBeTruthy();
    expect(screen.getByText("当前对你不可用")).toBeTruthy();
    // 116 dogfood 1.5.25: contract identifiers are not part of the reader's view; they sit behind "Technical details".
    const toggle = screen.getByRole("button", { name: "技术详情" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const panel = document.getElementById(toggle.getAttribute("aria-controls") ?? "")!;
    expect(panel.hidden).toBe(true);
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(panel.hidden).toBe(false);
    expect(within(panel).getByText("dataset.request.v1").tagName).toBe("CODE");
    expect(within(panel).getByText("dataset.v1")).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "复制 dataset.request.v1" })).toBeTruthy();
    expect(within(panel).getByText("能力定义生效 · v1")).toBeTruthy();
    expect(within(panel).getByText("实现复核中")).toBeTruthy();
    expect(document.body.textContent).not.toContain("shadow");
    expect(getAgentCapabilityReport).toHaveBeenCalledWith("osa_1", "zh-CN");
    expect(screen.queryByText(/这个智能体可以查询与分析数据/)).toBeNull();
  });

  it("lists the Plugins and Tools the Agent uses with reasons the viewer cannot use them", async () => {
    vi.mocked(getAgentDetail).mockResolvedValue({
      ...ITEM,
      resources: {
        plugins: [
          {
            kind: "builtin",
            id: "builtin:data-analysis",
            name: "data-analysis",
            listing: { "zh-CN": { name: "数据分析", description: "" } },
            version: 1,
            tools: ["bi_query"],
            unavailable_reason: null,
          },
        ],
        tools: [{ ref: "web_search", unavailable_reason: "disabled_by_team" }],
      },
    });
    renderIntl(<AgentDetail />);
    expect(await screen.findByRole("heading", { name: "用到的插件和工具" })).toBeTruthy();
    expect(screen.getByText("数据分析")).toBeTruthy();
    expect(screen.getByText("插件 · v1")).toBeTruthy();
    expect(screen.getByText("你的团队已停用")).toBeTruthy();
  });

  it("does not call an owner's existing assignment a deliberate hire", async () => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      relationship: "owned",
      assignment: { status: "active", source: "user_hired", row_version: 1 },
    } as never);
    renderIntl(<AgentDetail />);

    expect(await screen.findByText("已加入我的列表")).toBeTruthy();
    expect(screen.queryByText("你主动聘用")).toBeNull();
  });

  it("starts a new chat with the selected Agent when exact references are available", async () => {
    directUse.enabled = true;
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      assignment: { status: "active", source: "user_hired", row_version: 1 },
      availability: { discoverable: true, hireable: false, runnable: true, reasons: [] },
    } as never);
    renderIntl(<AgentDetail />);

    fireEvent.click(await screen.findByRole("button", { name: "开始使用" }));
    expect(directUse.handoff).toHaveBeenCalledWith({ agentId: "osa_1", label: "Finance Partner" });
  });

  it("replaces the enabled panel with the one reason a hired Agent cannot run", async () => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      assignment: { status: "active", source: "user_hired", row_version: 1 },
      availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "blocked_by_admin" },
    } as never);
    renderIntl(<AgentDetail />);

    const panel = (await screen.findByText("管理员限制了你使用这个智能体。")).closest("section") as HTMLElement;
    expect(panel.getAttribute("data-state")).toBe("unavailable");
    expect(within(panel).getByText("当前对你不可用")).toBeTruthy();
    expect(within(panel).getByText("如有疑问，请联系管理员")).toBeTruthy();
    expect(screen.getByText("管理员已限制你使用")).toBeTruthy();
    expect(screen.queryByText(/申请/)).toBeNull();
    expect(screen.queryByText("已加入“我的智能体团队”并启用")).toBeNull();
    expect(within(panel).getByRole("link", { name: "管理已聘用智能体" })).toBeTruthy();
  });

  it("does not point at the hired list for an Agent the viewer never hired", async () => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      assignment: { status: "not_hired", source: null, row_version: null },
      availability: { discoverable: true, hireable: false, runnable: false, reasons: [], public_reason: "agent_needs_owner_fix" },
      actions: { ...ITEM.actions, hire: false },
    } as never);
    renderIntl(<AgentDetail />);

    const panel = (await screen.findByText("这个智能体的已发布版本或安全状态需要处理后才能使用。请联系负责人或团队管理员检查。")).closest("section") as HTMLElement;
    expect(within(panel).queryByRole("link")).toBeNull();
    expect(within(panel).queryByRole("button")).toBeNull();
    // The execution fact says why, not "hire to use" next to a missing hire button.
    expect(screen.queryByText("聘用后进入调度候选")).toBeNull();
    expect(screen.queryByRole("button", { name: "聘用这个智能体" })).toBeNull();
  });

  it("keeps shadow implementation evidence distinct from an otherwise runnable Agent", async () => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      assignment: { status: "active", source: "user_hired", row_version: 1 },
      availability: { discoverable: true, hireable: false, runnable: true, reasons: [] },
    });
    vi.mocked(getAgentCapabilityReport).mockResolvedValueOnce({
      ...CAPABILITY_REPORT,
      capabilities: CAPABILITY_REPORT.capabilities.map((capability) => ({
        ...capability,
        readiness: {
          state: "unknown" as const,
          checked_at: "2026-09-07T10:00:00Z",
          reason_codes: ["implementation_not_reviewed"],
        },
      })),
    });
    renderIntl(<AgentDetail />);

    expect(await screen.findByText("验证进行中")).toBeTruthy();
    expect(screen.queryByText("已验证，当前可用")).toBeNull();
  });

  it("shows exact management evidence and repair links only when the API authorizes it", async () => {
    vi.mocked(getAgentCapabilityReport).mockResolvedValueOnce({
      ...CAPABILITY_REPORT,
      capabilities: CAPABILITY_REPORT.capabilities.map((capability) => ({
        ...capability,
        management: {
          implementation_ref: "capi_data_query_native@1",
          implementation_lifecycle: "active" as const,
          missing_dependencies: [{
            kind: "tool" as const,
            ref: "bi_query",
            repair_href: "/team/settings/tools/?from_agent=osa_1&dependency=bi_query",
          }],
          unresolved_dependencies: [{
            kind: "connection" as const,
            ref: "datarc",
            reason_code: "connection_readiness_not_evaluated",
          }],
        },
      })),
    });
    renderIntl(<AgentDetail />);

    const toggle = await screen.findByRole("button", { name: "技术详情" });
    fireEvent.click(toggle);
    expect(screen.getByText("capi_data_query_native@1").closest("[hidden]")).toBeNull();
    const repair = screen.getByRole("link", { name: "检查 Tool · bi_query" });
    expect(repair.getAttribute("href")).toContain("from_agent=osa_1");
    expect(screen.getByText("另有 1 项运行时依赖会在执行前检查。")).toBeTruthy();
  });

  it("says a built-in Agent's listing is managed by the system, not that it needs attention", async () => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      source: { kind: "system", owner_label: "Bowhead", owner_user_id: null },
      listing_safety: {
        effective_status: "managed",
        deterministic_status: "managed",
        semantic_status: "managed",
        listing_hash: null,
        reasons: [],
      },
    });
    renderIntl(<AgentDetail />);

    const check = await screen.findByText("介绍内容检查");
    const value = check.parentElement!.querySelector("dd")!;
    expect(value.getAttribute("data-state")).toBe("managed");
    expect(value.textContent).not.toMatch(/需要处理|需处理|待处理/);
    // Its own copy, naming the app as the gateway reports it, not the runtime-safety line.
    expect(value.textContent).toBe("由 Bowhead 提供");
    expect(value.querySelector("small")).toBeNull();
  });

  it("keeps Agent detail usable when capability evidence is rolled back", async () => {
    vi.mocked(getAgentCapabilityReport).mockRejectedValueOnce(new ApiError(404, "not found"));
    renderIntl(<AgentDetail />);

    expect(await screen.findByRole("heading", { name: "Finance Partner" })).toBeTruthy();
    expect(screen.getByText("结果契约证据当前未开放")).toBeTruthy();
    expect(screen.getByText(/查询你有权访问的企业数据并完成汇总、比较或诊断/)).toBeTruthy();
  });

  it("separates a capability evidence outage from the Agent detail response", async () => {
    vi.mocked(getAgentCapabilityReport).mockRejectedValueOnce(new ApiError(500, "unavailable"));
    renderIntl(<AgentDetail />);

    expect(await screen.findByRole("heading", { name: "Finance Partner" })).toBeTruthy();
    expect(screen.getByText("结果契约证据暂时无法刷新")).toBeTruthy();
    expect(screen.queryByText("暂时无法加载这个智能体，请稍后重试。")).toBeNull();
  });

  it("explains how Agent, Skill, and Tool contribute without inventing dependencies", async () => {
    renderIntl(<AgentDetail />);

    expect(await screen.findByRole("heading", { name: "智能体 · 负责目标" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Skill · 提供方法" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Tool · 执行操作" })).toBeTruthy();
    expect(screen.getByText(/不会增加任何数据权限/)).toBeTruthy();
  });

  it("separates listing rules, semantic checks, runtime evidence, and Tool effect", async () => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      risk: { level: "medium", reasons: ["external_write"] },
    });
    renderIntl(<AgentDetail />);

    expect(await screen.findByText("介绍规则已检查")).toBeTruthy();
    expect(screen.getByText("语义安全检查尚未运行")).toBeTruthy();
    expect(screen.getByText("尚未生成当前版本的运行时证据").closest("dd")?.getAttribute("data-state")).toBe("unknown");
    expect(screen.getByText("可能修改外部系统中的内容")).toBeTruthy();
    expect(screen.getByText("智能体调度状态")).toBeTruthy();
    expect(screen.getByText("聘用后进入调度候选")).toBeTruthy();
    expect(screen.getByText("该状态不代表每项 Outcome 已验证。")).toBeTruthy();
    expect(screen.queryByText("已检查", { exact: true })).toBeNull();
  });

  it("labels team listing copy as the creator's original", async () => {
    renderIntl(<AgentDetail />);

    expect(await screen.findByText(/创建者原文/)).toBeTruthy();
  });

  it("previews an exact release before creating a private Fork", async () => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      actions: { ...ITEM.actions, hire: false, fork: true },
    });
    vi.mocked(previewAgentFork).mockResolvedValueOnce({
      kind: "agent_fork_preview",
      schema_version: 1,
      source: {
        agent_id: "osa_1",
        revision_ref: "arv_exact_12345678",
        release_id: "arl_exact",
        display_name: "Finance Partner",
      },
      suggested_display_name: "Finance Partner · Copy",
      copied: { prompt: true, listing: true, model: false, output_contract: false, tool_count: 1, skill_count: 0 },
      dependency_gaps: [],
      excluded: ["audience_and_grants"],
      will_start_disabled: false,
    });
    vi.mocked(forkAgent).mockResolvedValueOnce({
      kind: "agent_fork_result",
      schema_version: 1,
      agent_id: "osa_fork",
      revision_id: "arv_fork",
      release_id: "arl_fork",
      lineage_id: "afk_1",
      dependency_gaps: [],
      status: "enabled",
      audience: "private",
      replayed: false,
      manager_url: "/agents/manage/?agent_id=osa_fork",
    });
    renderIntl(<AgentDetail />);

    fireEvent.click(await screen.findByRole("button", { name: "创建我的版本" }));
    expect(await screen.findByRole("heading", { name: "复制为我的智能体" })).toBeTruthy();
    expect(document.body.style.overflow).toBe("hidden");
    fireEvent.change(screen.getByRole("textbox", { name: "给你的版本命名" }), {
      target: { value: "我的财务伙伴" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建私有智能体" }));

    await waitFor(() => expect(forkAgent).toHaveBeenCalledWith("osa_1", expect.objectContaining({
      sourceRevisionRef: "arv_exact_12345678",
      displayName: "我的财务伙伴",
    })));
    expect(navigation.push).toHaveBeenCalledWith("/agents/manage/?agent_id=osa_fork");
    await waitFor(() => expect(document.body.style.overflow).toBe(""));
  });

  it("keeps the Fork dialog open on Escape while creating, and closes it on Escape afterwards", async () => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      actions: { ...ITEM.actions, hire: false, fork: true },
    });
    vi.mocked(previewAgentFork).mockResolvedValueOnce({
      kind: "agent_fork_preview",
      schema_version: 1,
      source: { agent_id: "osa_1", revision_ref: "arv_exact_12345678", release_id: "arl_exact", display_name: "Finance Partner" },
      suggested_display_name: "Finance Partner · Copy",
      copied: { prompt: true, listing: true, model: false, output_contract: false, tool_count: 1, skill_count: 0 },
      dependency_gaps: [],
      excluded: ["audience_and_grants"],
      will_start_disabled: false,
    });
    let failFork: ((reason: unknown) => void) | undefined;
    vi.mocked(forkAgent).mockReturnValueOnce(new Promise((_, reject) => {
      failFork = reject;
    }));
    renderIntl(<AgentDetail />);

    fireEvent.click(await screen.findByRole("button", { name: "创建我的版本" }));
    const dialog = await screen.findByRole("dialog", { name: "复制为我的智能体" });
    fireEvent.click(screen.getByRole("button", { name: "创建私有智能体" }));
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.getByRole("dialog", { name: "复制为我的智能体" })).toBe(dialog);

    failFork?.(new Error("offline"));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "给你的版本命名" }).hasAttribute("disabled")).toBe(false));
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("shows version-bound quality evidence separately from safety", async () => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      quality_evidence: {
        kind: "agent_quality_evidence",
        schema_version: 1,
        status: "valid",
        suites: [{
          suite_kind: "platform_contract",
          name: "Safe execution contract",
          state: "valid",
          stale_reason: null,
          sample_size: 1,
          attempts: 1,
          passed_cases: 1,
          flaky_cases: 0,
          evaluated_at: "2026-09-05T00:00:00Z",
        }],
      },
    });
    renderIntl(<AgentDetail />);

    expect(await screen.findByRole("heading", { name: "这个版本真正测过什么" })).toBeTruthy();
    expect(screen.getAllByText("当前有效").length).toBeGreaterThan(0);
    expect(screen.getByText(/不代表今后的每个请求都一定成功/)).toBeTruthy();
  });

  it.each([
    ["not_hired", "聘用前"],
    ["active", "当前使用边界"],
    ["personal_disabled", "重新启用前"],
    ["system_required", "系统提供的工作方式"],
    ["access_revoked", "权限变化后"],
  ] as const)("uses relationship-aware trust copy for %s", async (status, expected) => {
    vi.mocked(getAgentDetail).mockResolvedValueOnce({
      ...ITEM,
      assignment: { ...ITEM.assignment, status },
    });
    renderIntl(<AgentDetail />);

    expect(await screen.findByText(expected)).toBeTruthy();
  });
  it("clears the previous Agent when a query-only detail destination is unavailable", async () => {
    const { NextIntlClientProvider } = await import("next-intl");
    const messages = (await import("@/messages/zh-CN.json")).default;
    const surface = () => <NextIntlClientProvider locale="zh-CN" messages={messages}><AgentDetail /></NextIntlClientProvider>;
    const rendered = render(surface());
    await screen.findByRole("heading", { name: ITEM.display_name });
    vi.mocked(getAgentDetail).mockRejectedValueOnce(new ApiError(404, "Unavailable"));
    window.history.pushState(null, "", "/agents/detail/?id=osa_unavailable&return_view=shared");
    rendered.rerender(surface());
    await screen.findByRole("heading", { name: "找不到这个智能体，或你已没有查看权限" });
    expect(screen.queryByRole("heading", { name: ITEM.display_name })).toBeNull();
    expect(screen.getByRole("link", { name: "我的智能体" }).getAttribute("href")).toBe("/agents/manage/shared/");
  });

  it("returns Agent overview detail visits to the administrator overview", async () => {
    window.history.replaceState(null, "", "/agents/detail/?id=osa_1&return_page=dashboard");
    renderIntl(<AgentDetail />);
    await screen.findByRole("heading", { name: ITEM.display_name });
    expect(screen.getByRole("link", { name: "智能体概览" }).getAttribute("href")).toBe("/agents/dashboard/");
  });
});
