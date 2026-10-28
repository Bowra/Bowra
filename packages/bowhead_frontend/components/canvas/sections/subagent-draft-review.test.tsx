import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { NextIntlClientProvider } from "next-intl";
import zhMessages from "@/messages/zh-CN.json";
import { SubagentDraftApiError, type SubagentDraftDetail } from "@/lib/subagent-drafts-api";

const getMock = vi.fn();
const clarifyMock = vi.fn();
const updateMock = vi.fn();
const createMock = vi.fn();
const discardMock = vi.fn();
const convertMock = vi.fn();
const catalogMock = vi.fn();
const pushMock = vi.fn();
const directUse = vi.hoisted(() => ({ enabled: false, handoff: vi.fn() }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => ({ me: { capabilities: { composer: { agent_start_use: directUse.enabled } } } }) }));
vi.mock("@/lib/composer-handoff", () => ({ handoffAgentToNewChat: (...args: unknown[]) => directUse.handoff(...args) }));
vi.mock("@/lib/agents-api", () => ({ getAgentAuthoringCatalog: (...args: unknown[]) => catalogMock(...args) }));
vi.mock("@/lib/use-app-router", () => ({ BEFORE_APP_NAVIGATION_EVENT: "bowhead:before-app-navigation", useAppRouter: () => ({ push: (...args: unknown[]) => pushMock(...args) }) }));
vi.mock("@/lib/subagent-drafts-api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/subagent-drafts-api")>();
  return {
    ...original,
    getSubagentDraft: (...args: unknown[]) => getMock(...args),
    clarifySubagentDraft: (...args: unknown[]) => clarifyMock(...args),
    updateSubagentDraft: (...args: unknown[]) => updateMock(...args),
    createSubagentFromDraft: (...args: unknown[]) => createMock(...args),
    discardSubagentDraft: (...args: unknown[]) => discardMock(...args),
    convertSubagentDraftToManual: (...args: unknown[]) => convertMock(...args),
  };
});

import { SubagentDraftReview } from "./subagent-draft-review";

function draft(overrides: Partial<SubagentDraftDetail> = {}): SubagentDraftDetail {
  return {
    schema_version: 1,
    kind: "team_subagent_draft_detail",
    draft_id: "sad_review",
    draft_status: "ready",
    authoring_state: "reviewing",
    source_mode: "intent",
    entry_mode: "conversational",
    purpose: "每周分析门店销售异常并生成趋势图",
    goal_summary: "识别门店销售下降并生成趋势图",
    definition: {
      name: "store-sales-anomaly",
      display_name: "门店销售异常分析",
      description: "分析门店周度销售异常",
      system_prompt: "Analyze weekly store sales with evidence.",
      model: "analyst",
      max_iterations: 10,
      output_schema: null,
      visibility: "creator_only",
      tool_refs: ["bi_query"],
      skills: [],
    },
    tools: [
      {
        tool_ref: "bi_query",
        display_name: "查询业务数据",
        tier: "required",
        selected: true,
        risk: "read",
        source: { kind: "native", display_name: "Bowhead" },
        reason: "读取每周销售数据",
        evidence: ["deterministic_native_mapping"],
        confidence: "high",
        locked_reason: null,
        approval_required: false,
      },
      {
        tool_ref: "teammcp.crm.update",
        display_name: "更新 CRM",
        tier: "recommended",
        selected: false,
        risk: "external_write",
        source: { kind: "team", display_name: "CRM" },
        reason: "将结果写回 CRM",
        evidence: ["catalog_capability_match"],
        confidence: "medium",
        locked_reason: null,
        approval_required: false,
      },
    ],
    skills: [],
    assumptions: ["主要使用团队 BI 数据。"],
    warnings: [],
    clarification: null,
    validation: {
      status: "valid",
      issues: [],
      catalog_fingerprint: "sha256:test",
    },
    mutation_id: "11111111-1111-4111-8111-111111111111",
    version: 1,
    etag: 'W/"sad_review:1"',
    expires_at: "2026-08-01T00:00:00Z",
    result: null,
    actions: {
      clarify: false,
      edit: true,
      validate: true,
      create: true,
      discard: true,
      regenerate: false,
    },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  directUse.enabled = false;
});

describe("SubagentDraftReview", () => {
  it("names each blocked Skill when creating from the draft is refused", async () => {
    catalogMock.mockResolvedValue({ tools: [], plugins: undefined, skills: [] });
    createMock.mockRejectedValue(new SubagentDraftApiError(422, "agent_dependency_blocked", "Blocked", {
      reason_codes: ["skill_disabled"],
      blocked_items: [{ kind: "skill", name: "Sales report", identity: "team:sk_1", reason_codes: ["skill_disabled"] }],
    }));
    renderIntl(<SubagentDraftReview draftId="sad_review" initialDraft={draft()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));
    expect(await screen.findByText(/技能「Sales report」：已被团队停用。/)).toBeTruthy();
  });

  it("hides Skill operation Tools from the draft and its catalog", async () => {
    const skillTool = (ref: string) => ({
      stable_ref: ref, display_name: ref, description: ref, source_kind: "native", source_display_name: "Bowhead",
      capability_classes: [], risk: "read", selectable: true, locked_reason: null, approval_required: false, confirmation_summary: null,
    });
    catalogMock.mockResolvedValue({ tools: [skillTool("skill_read"), skillTool("skill_invoke"), skillTool("web_fetch")], skills: [] });
    const source = draft();
    const withSkillTool = draft({
      definition: { ...source.definition!, tool_refs: ["bi_query", "skill_list"] },
      tools: [...source.tools, { ...source.tools[0], tool_ref: "skill_list", display_name: "skill_list", tier: "recommended" }],
    });
    renderIntl(<SubagentDraftReview draftId="sad_review" initialDraft={withSkillTool} onClose={vi.fn()} />);
    expect(screen.queryByText("skill_list")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "从工具目录补选或替换" }));
    expect((await screen.findAllByText("web_fetch")).length).toBeGreaterThan(0);
    expect(screen.queryByText("skill_read")).toBeNull();
    expect(screen.queryByText("skill_invoke")).toBeNull();
  });

  it("replaces a recommendation with an authorized catalog tool in the same draft", async () => {
    const initial = draft();
    catalogMock.mockResolvedValue({ tools: [
      {
        stable_ref: "web_fetch", display_name: "读取网页", description: "读取用户给定网址",
        source_kind: "native", source_display_name: "Bowhead", capability_classes: ["web_fetch"],
        risk: "read", selectable: true, locked_reason: null, approval_required: false, confirmation_summary: null,
      },
      {
        stable_ref: "teammcp.private.read", display_name: "受限工具", description: "未开通",
        source_kind: "team_mcp", source_display_name: "Private", capability_classes: [],
        risk: "read", selectable: false, locked_reason: "not_entitled", approval_required: false, confirmation_summary: null,
      },
    ] });
    updateMock.mockResolvedValue(draft({
      version: 2, etag: 'W/"sad_review:2"',
      definition: { ...initial.definition!, tool_refs: ["web_fetch"] },
      tools: [{ ...initial.tools[0], tool_ref: "web_fetch", display_name: "读取网页", reason: "由你选择。" }],
    }));
    renderIntl(<SubagentDraftReview draftId="sad_review" initialDraft={initial} onClose={vi.fn()} />);
    fireEvent.click(within(screen.getByText("查询业务数据").closest("label")!).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "从工具目录补选或替换" }));
    await screen.findByText("读取网页");
    expect((within(screen.getByText("受限工具").closest("label")!).getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(within(screen.getByText("读取网页").closest("label")!).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "保存并检查" }));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("sad_review", expect.objectContaining({ selected_tool_refs: ["web_fetch"] }), initial.etag));
    expect(screen.getByText("由你选择。")).toBeTruthy();
  });

  it("turns a missing-tool validation code into a repair action", async () => {
    const initial = draft();
    catalogMock.mockResolvedValue({ tools: [] });
    renderIntl(<SubagentDraftReview draftId="sad_review" initialDraft={draft({
      draft_status: "blocked",
      definition: { ...initial.definition!, tool_refs: [] },
      tools: [],
      validation: { status: "invalid", catalog_fingerprint: "sha256:test", issues: [{
        code: "subagent_tool_required", severity: "error", field: "tool_refs", tool_ref: null,
        message_key: "subagent.validation.subagent_tool_required", details: {},
      }] },
    })} onClose={vi.fn()} />);
    expect(screen.getByText("当前团队需要至少选择一个工具。请从工具目录补选。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开工具目录修正" }));
    // One Plugin/Skill prefetch on open, then the on-demand Tool catalog.
    await waitFor(() => expect(catalogMock).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("searchbox", { name: "搜索可用工具" })).toBeTruthy();
  });

  it("keeps a selected tool removable when it disappears from the current catalog", async () => {
    const initial = draft();
    catalogMock.mockResolvedValue({ tools: [] });
    renderIntl(<SubagentDraftReview draftId="sad_review" initialDraft={draft({
      definition: { ...initial.definition!, tool_refs: ["teammcp.old.private"] },
      tools: [],
      validation: { status: "invalid", catalog_fingerprint: "sha256:test", issues: [{
        code: "subagent_tool_no_longer_available", severity: "error", field: "tool_refs",
        tool_ref: "teammcp.old.private", message_key: "subagent.validation.subagent_tool_no_longer_available", details: {},
      }] },
    })} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "打开工具目录修正" }));
    const row = (await screen.findByText("已选工具现在不可用，请取消选择或联系管理员处理。")).closest("label")!;
    fireEvent.click(within(row).getByRole("checkbox"));
    expect(screen.queryByText("已选工具现在不可用，请取消选择或联系管理员处理。")).toBeNull();
    expect(screen.getByRole("button", { name: "保存并检查" })).toHaveProperty("disabled", false);
  });

  it("keeps the draft selection when the catalog request fails and can retry", async () => {
    // The first request is the Plugin/Skill prefetch on open; the Tool catalog loads on demand after it.
    catalogMock.mockResolvedValueOnce({ tools: [] }).mockRejectedValueOnce(new Error("network down")).mockResolvedValueOnce({ tools: [] });
    renderIntl(<SubagentDraftReview draftId="sad_review" initialDraft={draft()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "从工具目录补选或替换" }));
    await screen.findByText("暂时无法读取工具目录。已选工具和草稿内容仍保留，请重试。");
    expect((within(screen.getByText("查询业务数据").closest("label")!).getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "加载最新版本" }));
    await waitFor(() => expect(catalogMock).toHaveBeenCalledTimes(3));
    expect(screen.queryByText("暂时无法读取工具目录。已选工具和草稿内容仍保留，请重试。")).toBeNull();
  });

  it("binds the saved version to Chat and protects unsaved right-side edits", () => {
    const onDraftChange = vi.fn();
    renderIntl(<SubagentDraftReview draftId="sad_review" initialDraft={draft()}
      onDraftChange={onDraftChange} onClose={vi.fn()} />);
    expect(onDraftChange).toHaveBeenCalledWith({ draftId: "sad_review", version: 1, dirty: false });
    expect(screen.getByText(/当前左侧对话已连接这份草稿/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "编辑完整配置" }));
    fireEvent.change(screen.getByLabelText("完整系统提示词"), { target: { value: "新的工作说明" } });
    expect(onDraftChange).toHaveBeenCalledWith({ draftId: "sad_review", version: 1, dirty: true });
    expect(screen.getByText(/右侧有未保存修改/)).toBeTruthy();
  });

  it("moves local AI edits into the same manual draft before opening its editor", async () => {
    const initial = draft();
    const converted = { draft_id: initial.draft_id, entry_mode: "canvas", source_mode: "intent" };
    const onManual = vi.fn();
    const onDraftChange = vi.fn();
    convertMock.mockResolvedValue(converted);
    renderIntl(<SubagentDraftReview draftId={initial.draft_id} initialDraft={initial}
      unifiedDraftEnabled onManual={onManual} onDraftChange={onDraftChange} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "编辑完整配置" }));
    fireEvent.change(screen.getByLabelText("完整系统提示词"), { target: { value: "保留我改过的指令" } });
    fireEvent.click(screen.getByRole("button", { name: "手动配置" }));
    await waitFor(() => expect(convertMock).toHaveBeenCalledWith(
      initial,
      initial.purpose,
      expect.objectContaining({ system_prompt: "保留我改过的指令", tool_refs: ["bi_query"] }),
      false,
    ));
    expect(onManual).toHaveBeenCalledWith(initial.purpose, converted);
    expect(onDraftChange).toHaveBeenCalledWith(null);
  });

  it("refreshes a chat-saved version without overwriting a later manual edit", async () => {
    const initial = draft();
    const onDraftChange = vi.fn();
    getMock.mockResolvedValue(draft({ version: 2, etag: 'W/"sad_review:2"',
      definition: { ...initial.definition!, description: "对话更新后的用途" } }));
    const renderReview = (refreshVersion: number | null) => (
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <SubagentDraftReview draftId="sad_review" initialDraft={initial}
          refreshVersion={refreshVersion} onDraftChange={onDraftChange} onClose={vi.fn()} />
      </NextIntlClientProvider>
    );
    const view = renderIntl(<SubagentDraftReview draftId="sad_review" initialDraft={initial}
      onDraftChange={onDraftChange} onClose={vi.fn()} />);
    view.rerender(renderReview(2));
    await waitFor(() => expect(onDraftChange).toHaveBeenCalledWith({ draftId: "sad_review", version: 2, dirty: false }));
    fireEvent.click(screen.getByRole("button", { name: "编辑完整配置" }));
    const prompt = screen.getByLabelText("完整系统提示词") as HTMLTextAreaElement;
    fireEvent.change(prompt, { target: { value: "保留我的本地修改" } });
    view.rerender(renderReview(3));
    expect(screen.getByLabelText("完整系统提示词")).toHaveProperty("value", "保留我的本地修改");
    expect(screen.getByText(/草稿已在其他窗口更新/)).toBeTruthy();
  });

  it.each(["zh-CN", "en-US"] as const)("explains why an unselected approval tool is locked in %s", (locale) => {
    const locked = draft();
    locked.tools[1] = { ...locked.tools[1], locked_reason: "approval_executor_unavailable" };
    renderIntl(<SubagentDraftReview draftId={locked.draft_id} initialDraft={locked} onClose={vi.fn()} />, { locale });
    const row = screen.getByText("更新 CRM").closest("label")!;
    expect((within(row).getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
    expect(row.textContent).toContain(locale === "zh-CN"
      ? "当前部署无法执行此工具所需的审批流程"
      : "This deployment cannot execute this tool's approval workflow.");
  });

  it("shows the server explanation for an unmet required capability", () => {
    const message = "必需能力「查询数据」（读取每周销售数据）没有选中任何工具：请在工具列表中确认并勾选。";
    const blocked = draft({
      draft_status: "blocked",
      validation: {
        status: "invalid",
        issues: [
          {
            code: "subagent_required_capability_unmet",
            severity: "error",
            field: "tool_refs",
            tool_ref: null,
            message_key: "subagent.validation.subagent_required_capability_unmet",
            message,
            details: { capability_class: "data_query" },
          },
        ],
        catalog_fingerprint: "sha256:test",
      },
    });
    renderIntl(<SubagentDraftReview draftId={blocked.draft_id} initialDraft={blocked} onClose={vi.fn()} />);
    expect(screen.getByText(message).tagName).toBe("LI");
  });

  it("explains approval lock and lets a previously selected tool be removed before creation", async () => {
    const locked = draft();
    locked.definition = { ...locked.definition!, tool_refs: ["bi_query", "teammcp.crm.update"], skills: [] };
    locked.tools[1] = { ...locked.tools[1], selected: true, locked_reason: "approval_executor_unavailable", approval_required: true };
    updateMock.mockResolvedValue(locked);
    renderIntl(<SubagentDraftReview draftId={locked.draft_id} initialDraft={locked} onClose={vi.fn()} />);
    const row = screen.getByText("更新 CRM").closest("label")!;
    const checkbox = within(row).getByRole("checkbox") as HTMLInputElement;
    expect(within(row).getByText(/当前部署无法执行此工具所需的审批流程/)).toBeTruthy();
    expect(checkbox.disabled).toBe(false);
    fireEvent.click(checkbox);
    expect(checkbox.checked).toBe(false);
    expect(checkbox.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "保存并检查" }));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith(locked.draft_id, expect.objectContaining({ selected_tool_refs: ["bi_query"] }), locked.etag));
    const saved = JSON.parse(JSON.stringify(updateMock.mock.calls[0][1]));
    const { tool_refs: _tools, skills: _skills, ...editable } = locked.definition;
    // Skills and Plugins travel with the definition (the server stores the same empty defaults).
    expect(saved.definition).toEqual({ ...editable, skill_grants: [], capability_ids: [] });
  });

  it("shows the generated summary, private audience, and explainable tools", () => {
    renderIntl(
      <SubagentDraftReview
        draftId="sad_review"
        initialDraft={draft()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("门店销售异常分析")).toBeTruthy();
    expect(screen.getByText("仅自己")).toBeTruthy();
    expect(screen.getByText("主要使用团队 BI 数据。")).toBeTruthy();
    expect(screen.getByText("查询业务数据")).toBeTruthy();
    expect(screen.getByText("读取每周销售数据")).toBeTruthy();
  });

  it("saves selected tools before final creation", async () => {
    directUse.enabled = true;
    const updated = draft({
      version: 2,
      etag: 'W/"sad_review:2"',
      tools: draft().tools.map((tool) => ({
        ...tool,
        selected: true,
      })),
    });
    updateMock.mockResolvedValue(updated);
    createMock.mockResolvedValue({
      schema_version: 1,
      kind: "team_subagent_created_from_draft",
      replayed: false,
      draft_id: "sad_review",
      definition_id: "osa_1",
      visibility: "creator_only",
      status: "enabled",
      lifecycle_state: "active",
    });
    renderIntl(
      <SubagentDraftReview
        draftId="sad_review"
        initialDraft={draft()}
        onClose={vi.fn()}
      />,
    );
    const crmRow = screen.getByText("更新 CRM").closest("label");
    if (!crmRow) throw new Error("CRM row missing");
    fireEvent.click(within(crmRow).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    expect(updateMock.mock.calls[0][1].selected_tool_refs).toEqual([
      "bi_query",
      "teammcp.crm.update",
    ]);
    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    expect(createMock.mock.calls[0][0].etag).toBe('W/"sad_review:2"');
    expect(await screen.findByText("智能体已创建")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "开始使用" }));
    expect(directUse.handoff).toHaveBeenCalledWith({ agentId: "osa_1", label: "门店销售异常分析" });
    fireEvent.click(screen.getByRole("button", { name: "管理智能体" }));
    expect(pushMock).toHaveBeenCalledWith("/agents/manage/?agent_id=osa_1");
  });

  it("saves Plugins and Skills picked in the review with the draft", async () => {
    const grant = {
      distribution: "reference", permissions: ["read"], revision: "sha256:" + "a".repeat(64),
      skill_ref: { id: "skill_store_kpi", kind: "team", name: "store-kpi" },
    };
    catalogMock.mockResolvedValue({
      tools: [],
      skills: [{ name: "store-kpi", display_name: "门店 KPI 方法", description: "门店指标口径", source_kind: "team", linked_tools: [], allowed_audiences: ["private"], grant }],
      plugins: {
        builtin: [{
          id: "builtin:designs", name: "designs", version: 1, family: "general",
          listing: { "zh-CN": { name: "设计稿", description: "创建设计稿" } }, tools: ["canvas_create_design"], prompt_modules: [],
          ready: true, unavailable_tools: [], selectable: true, locked_reason: null, team_disabled: false, my_disabled: false,
        }],
        team: [],
      },
    });
    updateMock.mockResolvedValue(draft({ version: 2, etag: 'W/"sad_review:2"' }));
    createMock.mockResolvedValue({
      schema_version: 1, kind: "team_subagent_created_from_draft", replayed: false, draft_id: "sad_review",
      definition_id: "osa_1", visibility: "creator_only", status: "enabled", lifecycle_state: "active",
    });
    renderIntl(<SubagentDraftReview draftId="sad_review" initialDraft={draft()} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("checkbox", { name: /设计稿/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /门店 KPI 方法/ }));
    fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    const saved = updateMock.mock.calls[0][1].definition;
    expect(saved.builtin_plugin_ids).toEqual(["builtin:designs"]);
    expect(saved.capability_ids).toEqual([]);
    expect(saved.skill_grants).toEqual([grant]);
  });

  it("asks one server-provided clarification and keeps the same draft", async () => {
    const clarifying = draft({
      draft_status: "needs_clarification",
      authoring_state: "needs_clarification",
      definition: null,
      tools: [],
      clarification: {
        schema_version: 1,
        question: "主要使用哪类销售数据？",
        answered: false,
      },
      validation: {
        status: "unknown",
        issues: [],
        catalog_fingerprint: null,
      },
      actions: {
        clarify: true,
        edit: false,
        validate: false,
        create: false,
        discard: true,
        regenerate: false,
      },
    });
    clarifyMock.mockResolvedValue(draft({ version: 2 }));
    renderIntl(
      <SubagentDraftReview
        draftId="sad_review"
        initialDraft={clarifying}
        onClose={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("你的补充"), {
      target: { value: "使用团队 BI 销售数据，只读取" },
    });
    fireEvent.click(screen.getByRole("button", { name: "继续生成草稿" }));

    await waitFor(() => expect(clarifyMock).toHaveBeenCalledWith(
      "sad_review",
      "使用团队 BI 销售数据，只读取",
      'W/"sad_review:1"',
    ));
    expect(await screen.findByText("门店销售异常分析")).toBeTruthy();
  });

  it("loads the terminal draft after clarification fails and preserves the answer for manual setup", async () => {
    const source = draft({
      draft_status: "needs_clarification",
      definition: null,
      tools: [],
      clarification: { schema_version: 1, question: "要核对什么？", answered: false },
      actions: { clarify: true, edit: false, validate: false, create: false, discard: true, regenerate: false },
    });
    const failed = draft({
      ...source,
      draft_status: "blocked",
      version: 3,
      etag: 'W/"sad_review:3"',
      clarification: { schema_version: 1, question: "要核对什么？", answered: true, answer: "核对用户给出的公开公告" },
      actions: { ...source.actions, clarify: false, edit: true },
    });
    clarifyMock.mockRejectedValue(new SubagentDraftApiError(503, "subagent_draft_secret_pattern_rejected", "操作失败"));
    getMock.mockResolvedValue(failed);
    const onManual = vi.fn();
    renderIntl(<SubagentDraftReview draftId={source.draft_id} initialDraft={source} onClose={vi.fn()} onManual={onManual} />);

    fireEvent.change(screen.getByLabelText("你的补充"), { target: { value: "核对用户给出的公开公告" } });
    fireEvent.click(screen.getByRole("button", { name: "继续生成草稿" }));

    await waitFor(() => expect(getMock).toHaveBeenCalledWith(source.draft_id));
    expect(await screen.findByText(/草稿已更新/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "继续生成草稿" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "手动配置" }));
    expect(onManual).toHaveBeenCalledWith(`${source.purpose}\n核对用户给出的公开公告`);
  });

  it("keeps the typed answer when a failed request leaves the draft clarifiable", async () => {
    const source = draft({
      draft_status: "needs_clarification",
      definition: null,
      tools: [],
      clarification: { schema_version: 1, question: "要核对什么？", answered: false },
      actions: { clarify: true, edit: false, validate: false, create: false, discard: true, regenerate: false },
    });
    clarifyMock.mockRejectedValue(new Error("暂时无法连接"));
    getMock.mockResolvedValue(source);
    const onManual = vi.fn();
    renderIntl(<SubagentDraftReview draftId={source.draft_id} initialDraft={source} onClose={vi.fn()} onManual={onManual} />);

    fireEvent.change(screen.getByLabelText("你的补充"), { target: { value: "只核对公开公告" } });
    fireEvent.click(screen.getByRole("button", { name: "继续生成草稿" }));

    await waitFor(() => expect(getMock).toHaveBeenCalledWith(source.draft_id));
    expect(screen.getByLabelText("你的补充")).toHaveProperty("value", "只核对公开公告");
    fireEvent.click(screen.getByRole("button", { name: "手动配置" }));
    expect(onManual).toHaveBeenCalledWith(`${source.purpose}\n只核对公开公告`);
  });
});


describe("text-only draft review", () => {
  const warning = { code: "subagent_text_only_confirmation", message: "我确认只处理提供的文字。" };
  function textDraft() {
    const source = draft();
    return draft({ definition: { ...source.definition!, tool_refs: [] }, tools: [], warnings: [warning],
      validation: { ...source.validation, status: "warning" } });
  }

  it("requires explicit confirmation and submits the reviewed snapshot", async () => {
    const source = textDraft();
    createMock.mockResolvedValue({ kind: "team_subagent_created_from_draft", definition_id: "osa_text", draft_id: source.draft_id, replayed: false });
    renderIntl(<SubagentDraftReview draftId={source.draft_id} initialDraft={source} onClose={vi.fn()} />);
    expect(screen.getByText(/此草稿只处理你提供的文字/)).toBeTruthy();
    const create = screen.getByRole("button", { name: "创建智能体" });
    expect(create).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByLabelText(warning.message));
    expect(create).toHaveProperty("disabled", false);
    fireEvent.click(create);
    await waitFor(() => expect(createMock).toHaveBeenCalledWith(source, [warning.code]));
  });

  it("requires a fresh confirmation after saving edited content", async () => {
    const source = textDraft();
    const updated = { ...source, version: 2, etag: 'W/"sad_review:2"' };
    updateMock.mockResolvedValue(updated);
    createMock.mockResolvedValue({ kind: "team_subagent_created_from_draft", definition_id: "osa_text", draft_id: source.draft_id, replayed: false });
    renderIntl(<SubagentDraftReview draftId={source.draft_id} initialDraft={source} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText(warning.message));
    fireEvent.click(screen.getByRole("button", { name: "编辑完整配置" }));
    fireEvent.change(screen.getByLabelText("完整系统提示词"), { target: { value: "只整理提供的文字并说明变化。" } });
    fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));
    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    expect(createMock).not.toHaveBeenCalled();
    expect(screen.getByLabelText(warning.message)).toHaveProperty("checked", false);
    fireEvent.click(screen.getByLabelText(warning.message));
    fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));
    await waitFor(() => expect(createMock).toHaveBeenCalledWith(updated, [warning.code]));
  });
});
