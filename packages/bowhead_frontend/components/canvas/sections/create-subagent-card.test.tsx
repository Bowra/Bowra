import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import zhMessages from "@/messages/zh-CN.json";
import type { TeamSubagentOwnerManagerDetail } from "@/lib/team-subagents-api";
import type { ConfigDraftResult } from "@/lib/config-drafts-api";
import type { CreateSubagentCardProps } from "./create-subagent-card";

// cca P3 — CreateSubagentCard edit mode: prefill an existing subagent + PATCH (not create),
// with name + visibility read-only. The create path must stay unchanged.

vi.mock("@/lib/team-plugins-api", () => ({ listTeamPlugins: vi.fn(async () => []) }));
const getToolsCatalogMock = vi.fn();
const getSkillsCatalogMock = vi.fn();
const createSubagentMock = vi.fn();
const patchSubagentMock = vi.fn();
const createManualDraftMock = vi.fn();
const saveManualDraftMock = vi.fn();
const getManualDraftMock = vi.fn();
const getSubagentManagerMock = vi.fn();
const saveAgentWorkingCopyMock = vi.fn();
const getAgentAuthoringCatalogMock = vi.fn(() => Promise.resolve({ plugins: undefined as unknown }));
const getAgentWorkingCopyMock = vi.fn((..._args: unknown[]): Promise<unknown> => Promise.reject(new Error("not read in this test")));
vi.mock("@/lib/agents-api", () => ({
  saveAgentWorkingCopy: (...a: unknown[]) => saveAgentWorkingCopyMock(...a),
  getAgentAuthoringCatalog: () => getAgentAuthoringCatalogMock(),
  getAgentWorkingCopy: (...a: unknown[]) => getAgentWorkingCopyMock(...a),
}));
const pushMock = vi.fn();
const authMock = vi.fn();
const handoffMock = vi.fn();
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => authMock() }));
vi.mock("@/lib/composer-handoff", () => ({ handoffAgentToNewChat: (...args: unknown[]) => handoffMock(...args) }));
vi.mock("@/lib/use-app-router", () => ({
  BEFORE_APP_NAVIGATION_EVENT: "bowhead:before-app-navigation",
  useAppRouter: () => ({ push: (...args: unknown[]) => pushMock(...args) }),
}));
vi.mock("@/lib/team-subagents-api", () => ({
  getSubagentManager: (...a: unknown[]) => getSubagentManagerMock(...a),
  getToolsCatalog: (...a: unknown[]) => getToolsCatalogMock(...a),
  getSkillsCatalog: (...a: unknown[]) => getSkillsCatalogMock(...a),
  createSubagent: (...a: unknown[]) => createSubagentMock(...a),
  patchSubagent: (...a: unknown[]) => patchSubagentMock(...a),
}));
vi.mock("@/lib/subagent-drafts-api", () => ({
  SubagentDraftApiError: class extends Error {
    constructor(public status: number, public code: string) { super(code); }
  },
  createManualSubagentDraft: (...a: unknown[]) => createManualDraftMock(...a),
  saveManualSubagentDraft: (...a: unknown[]) => saveManualDraftMock(...a),
  getManualSubagentDraft: (...a: unknown[]) => getManualDraftMock(...a),
  validateManualSubagentDraft: vi.fn(),
  createSubagentFromManualDraft: vi.fn(),
}));

import { CreateSubagentCard } from "./create-subagent-card";
import { toolDisplay } from "@/lib/tool-display";
import { validateManualSubagentDraft, createSubagentFromManualDraft, SubagentDraftApiError } from "@/lib/subagent-drafts-api";
import { confirmCanvasNavigation } from "@/lib/canvas-navigation-guard";

const EXISTING = {
  schema_version: 1,
  kind: "team_owner_manager_detail",
  id: "sa_1",
  team_id: "o1",
  name: "sales-bot",
  display_name: "销售助手",
  description: "分析销售数据",
  system_prompt: "你是销售分析师",
  tool_refs: ["bi_query"],
  tool_count: 1,
  max_iterations: 12,
  skills: [] as string[],
  model: null,
  output_schema: null,
  visibility: "team_wide",
  status: "enabled",
  created_by: "u1",
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-02T00:00:00Z",
  source: "team",
  owner: { id: "u1", display_name: "Alice", status: "active" },
  audience: { mode: "team_wide", roster: [] },
  lifecycle_state: "active",
  version: 1,
  etag: 'W/"sa_1:1"',
  actions: {
    view_consumer_detail: false,
    view_manager_detail: true,
    edit: true,
    change_status: true,
    manage_audience: false,
    transfer_owner: false,
    delete: true,
    validate: false,
    bring_to_web_chat: false,
    govern: false,
  },
  action_reasons: {},
} satisfies TeamSubagentOwnerManagerDetail;

beforeEach(() => {
  getSubagentManagerMock.mockResolvedValue(EXISTING);
  getAgentWorkingCopyMock.mockImplementation(() => Promise.reject(new Error("not read in this test")));
  authMock.mockReturnValue(null);
  // every mount fetches the skills catalog; default to empty so existing tests are unaffected.
  getSkillsCatalogMock.mockResolvedValue({ builtin: [], team_skills: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CreateSubagentCard edit mode", () => {
  it("only describes Chat as connected when this editor receives its Chat draft", () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: false });
    renderIntl(<CreateSubagentCard onClose={() => {}} />);
    expect(screen.getByText(/当前对话未连接这份创建配置/)).toBeTruthy();
    cleanup();

    const chatDraft = {
      status: "ok", form_id: "team_subagent", fields: { description: "只分析月度销售" },
      field_meta: { description: { origin: "generated", needs_confirm: false, note: null } },
      issues: [], notes: {}, clarification: null, secret_redacted: false,
    } as ConfigDraftResult;
    renderIntl(<CreateSubagentCard editing={EXISTING} initialDraft={chatDraft}
      draftSubscribe={() => () => {}} onClose={() => {}} />);
    expect(screen.getByText(/当前对话已连接此智能体/)).toBeTruthy();
  });

  it("saves a revisioned agent as a working copy while preserving unrelated definition, listing and audience", async () => {
    saveAgentWorkingCopyMock.mockResolvedValue({ id: "awc_1" });
    const audience = { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] };
    const definition = { system_prompt: "你是销售分析师", tool_refs: ["bi_query"], max_iterations: 12,
      model: "analyst", output_schema: null, skill_grants: [], primary_capability_ref: "cap_visualize@1" };
    const revisioned = {
      detail: {
        working_copy: null,
        live: { definition, listing: { display_name: "销售助手", description: "分析销售数据", source_locale: "zh-CN", taxonomy: { domain: "sales" } }, audience },
      },
      catalog: {
        tools: [{ stable_ref: "bi_query", source_kind: "native", selectable: true, risk: "read", description: "查询", locked_reason: null }],
        skills: [], model_policy: { allowed_roles: ["analyst"], effective_role: "analyst" },
      },
    } as unknown as NonNullable<CreateSubagentCardProps["revisioned"]>;
    renderIntl(<CreateSubagentCard editing={EXISTING} revisioned={revisioned} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    await waitFor(() => expect(screen.getByText("保存修改")).not.toHaveProperty("disabled", true));
    fireEvent.click(screen.getByText("保存修改"));
    await waitFor(() => expect(saveAgentWorkingCopyMock).toHaveBeenCalledWith("sa_1", expect.objectContaining({
      definition: expect.objectContaining({ primary_capability_ref: "cap_visualize@1", tool_refs: ["bi_query"] }),
      listing: expect.objectContaining({ taxonomy: { domain: "sales" } }),
      audience,
      expected_row_version: null,
    })));
    expect(patchSubagentMock).not.toHaveBeenCalled();
  });
  it("offers Plugins when editing a revisioned agent and writes only changed selections", async () => {
    saveAgentWorkingCopyMock.mockResolvedValue({ id: "awc_1" });
    const audience = { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] };
    const definition = { system_prompt: "你是销售分析师", tool_refs: ["bi_query"], max_iterations: 12,
      model: "analyst", output_schema: null, skill_grants: [], builtin_plugin_ids: ["builtin:designs"] };
    const revisioned = {
      detail: { working_copy: null, live: { definition, listing: { display_name: "销售助手", description: "分析销售数据" }, audience } },
      catalog: {
        tools: [{ stable_ref: "bi_query", source_kind: "native", selectable: true, risk: "read", description: "查询", locked_reason: null }],
        skills: [], model_policy: { allowed_roles: ["analyst"], effective_role: "analyst" },
        plugins: {
      builtin: [
        {
          id: "builtin:designs", name: "designs", version: 1, family: "general",
          listing: { "zh-CN": { name: "设计稿", description: "创建设计稿" } }, tools: ["canvas_create_design"], prompt_modules: [],
          ready: true, unavailable_tools: [], selectable: true, locked_reason: null, team_disabled: false, my_disabled: false,
        },
        {
          id: "builtin:web-research", name: "web-research", version: 1, family: "general",
          listing: { "zh-CN": { name: "联网研究", description: "搜索网页" } }, tools: ["web_search"], prompt_modules: [],
          ready: true, unavailable_tools: [], selectable: true, locked_reason: null, team_disabled: false, my_disabled: false,
        },
      ],
      team: [],
    },
      },
    } as unknown as NonNullable<CreateSubagentCardProps["revisioned"]>;
    renderIntl(<CreateSubagentCard editing={EXISTING} revisioned={revisioned} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    expect(screen.getByRole("list", { name: "已选插件" }).textContent).toContain("设计稿");
    fireEvent.click(screen.getByRole("button", { name: "+ 联网研究" }));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    await waitFor(() => expect(screen.getByText("保存修改")).not.toHaveProperty("disabled", true));
    fireEvent.click(screen.getByText("保存修改"));
    await waitFor(() => expect(saveAgentWorkingCopyMock).toHaveBeenCalledWith("sa_1", expect.objectContaining({
      definition: expect.objectContaining({ builtin_plugin_ids: ["builtin:designs", "builtin:web-research"] }),
    })));
    expect(saveAgentWorkingCopyMock.mock.calls[0][1].definition).not.toHaveProperty("capability_ids");
  });
  it("explains skill audience and dependency restrictions in the revisioned editor", async () => {
    const audience = { schema_version: 1, effective_mode: "teamwide", user_ids: [], group_ids: [] };
    const grant = { source_kind: "team", stable_ref: "skill@1", version: 1 };
    const revisioned = {
      detail: {
        working_copy: null,
        live: {
          definition: { system_prompt: "你是销售分析师", tool_refs: ["bi_query"], max_iterations: 12, model: "analyst", skill_grants: [] },
          listing: { display_name: "销售助手", description: "分析销售数据" }, audience,
        },
      },
      catalog: {
        tools: [{ stable_ref: "bi_query", source_kind: "native", selectable: true, risk: "read", description: "查询", locked_reason: null }],
        skills: [
          { name: "private-sop", description: "个人流程", source_kind: "user", allowed_audiences: ["private"], grant },
          { name: "sales-sop", description: "销售流程", source_kind: "team", allowed_audiences: ["teamwide"], required_skills: ["base-sop"], grant },
          { name: "base-sop", description: "基础流程", source_kind: "team", allowed_audiences: ["teamwide"], grant },
        ],
        model_policy: { allowed_roles: ["analyst"], effective_role: "analyst" },
      },
    } as unknown as NonNullable<CreateSubagentCardProps["revisioned"]>;
    renderIntl(<CreateSubagentCard editing={EXISTING} revisioned={revisioned} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByText("自动读取的技能"));
    expect(screen.getByText(/此技能的可用范围未覆盖智能体的所有使用者/)).toBeTruthy();
    expect(within(screen.getByText("private-sop").closest("label")!).getByRole("checkbox")).toHaveProperty("disabled", true);
    expect(screen.getByText(/还需选择依赖技能：base-sop/)).toBeTruthy();
  });
  it("only overlays suggested fields and protects a manual edit from later chat updates", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
    let apply: ((draft: ConfigDraftResult) => { filled: number }) | undefined;
    const draft = (fields: Record<string, unknown>): ConfigDraftResult => ({
      status: "ok", form_id: "team_subagent", fields,
      field_meta: Object.fromEntries(Object.keys(fields).map((key) => [key, { origin: "generated", needs_confirm: false, note: null }])),
      issues: [], notes: {}, clarification: null, secret_redacted: false,
    } as ConfigDraftResult);
    renderIntl(<CreateSubagentCard editing={EXISTING} initialDraft={draft({ description: "只分析月度销售" })}
      draftSubscribe={(callback) => { apply = callback; return () => { apply = undefined; }; }} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByDisplayValue("只分析月度销售")).toBeTruthy());
    expect(screen.getByDisplayValue("你是销售分析师")).toBeTruthy();
    fireEvent.change(screen.getByDisplayValue("只分析月度销售"), { target: { value: "我手动写的说明" } });
    act(() => { apply?.(draft({ description: "AI 再次改写", system_prompt: "请先校验口径" })); });
    expect(screen.getByDisplayValue("我手动写的说明")).toBeTruthy();
    expect(screen.getByDisplayValue("请先校验口径")).toBeTruthy();
  });
  it("clears an existing output schema when chat explicitly proposes null", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
    const draft = {
      status: "ok", form_id: "team_subagent", fields: { output_schema: null },
      field_meta: { output_schema: { origin: "generated", needs_confirm: false, note: null } },
      issues: [], notes: {}, clarification: null, secret_redacted: false,
    } as ConfigDraftResult;
    renderIntl(<CreateSubagentCard editing={{ ...EXISTING, output_schema: { type: "object" } }} initialDraft={draft}
      draftSubscribe={() => () => {}} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "输出格式" })).toHaveProperty("value", ""));
    await waitFor(() => expect(screen.getByText("保存修改")).not.toHaveProperty("disabled", true));
    fireEvent.click(screen.getByText("保存修改"));
    await waitFor(() => expect(patchSubagentMock).toHaveBeenCalledWith("sa_1", expect.objectContaining({ output_schema: null }), EXISTING.etag));
  });
  it("requires work instructions before leaving the first step", () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    renderIntl(<CreateSubagentCard onClose={() => {}} />);

    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "sales-bot" } });
    fireEvent.change(screen.getByPlaceholderText(/分析销售数据并生成图表/), { target: { value: "分析销售" } });

    const next = screen.getByText(/下一步 · 选择工具/).closest("button") as HTMLButtonElement;
    expect(screen.getByText("工作说明")).toBeTruthy();
    expect(next.getAttribute("aria-disabled")).toBe("true");
    // an attempt to continue stays on this step and names the missing field
    fireEvent.click(next);
    expect(screen.getByText("请填写工作说明。")).toBeTruthy();
    expect(screen.queryByText(/下一步 · 配置/)).toBeNull();

    fireEvent.change(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/), { target: { value: "分析销售数据并说明结论" } });
    expect(next.getAttribute("aria-disabled")).toBe("false");
    expect(screen.queryByText("请填写工作说明。")).toBeNull();
  });

  it("names each field by its label, not by its placeholder example", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    renderIntl(<CreateSubagentCard onClose={() => {}} />);

    const cases: [string, string | RegExp][] = [
      ["标识名", "sales-analysis"],
      ["名称", "例如：销售数据分析师"],
      ["用途说明", /分析销售数据并生成图表/],
      ["工作说明", /你是一名严谨的销售数据分析师/],
    ];
    for (const [label, placeholder] of cases) {
      expect(screen.getByRole("textbox", { name: label })).toBe(screen.getByPlaceholderText(placeholder));
    }
  });

  it("does not treat a stored Skill operation Tool as a stale selection", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    renderIntl(<CreateSubagentCard editing={{ ...EXISTING, tool_refs: ["bi_query", "skill_invoke"] }} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    await screen.findByText(toolDisplay("bi_query").label);
    expect(screen.queryByText(/skill_invoke/)).toBeNull();
    expect((screen.getByText(/下一步 · 配置/).closest("button") as HTMLButtonElement).disabled).toBe(false);
  });

  it("names the configure-step controls by their labels", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    renderIntl(<CreateSubagentCard editing={EXISTING} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    await screen.findByText(toolDisplay("bi_query").label);
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    expect(screen.getByRole("combobox", { name: "模型" })).toBeTruthy();
  });

  it("prefills the name and display name the user stated in chat", () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    renderIntl(
      <CreateSubagentCard purposeHint="只回答 GitHub 开源仓库的文档问题" nameHint="dogfood-wiki-helper"
        displayNameHint="Wiki 助手" onClose={() => {}} />,
    );
    expect((screen.getByPlaceholderText("sales-analysis") as HTMLInputElement).value).toBe("dogfood-wiki-helper");
    expect((screen.getByPlaceholderText("例如：销售数据分析师") as HTMLInputElement).value).toBe("Wiki 助手");
    expect((screen.getByPlaceholderText(/分析销售数据并生成图表/) as HTMLInputElement).value).toBe("只回答 GitHub 开源仓库的文档问题");
    expect((screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/) as HTMLTextAreaElement).value).toBe("只回答 GitHub 开源仓库的文档问题");
  });

  it("keeps the full goal in work instructions when manual configuration follows AI drafting", () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    const goal = `只处理用户提供的门店记录。${"逐条检查异常并说明依据。".repeat(20)}`;
    renderIntl(<CreateSubagentCard purposeHint={goal} onClose={() => {}} />);

    expect((screen.getByRole("textbox", { name: /工作说明/ }) as HTMLTextAreaElement).value).toBe(goal);
    expect((screen.getByRole("textbox", { name: /用途说明/ }) as HTMLInputElement).value).toBe(goal.slice(0, 200));
  });

  it("explains an invalid name in words the user can act on", () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    renderIntl(<CreateSubagentCard onClose={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "9bad" } });
    expect(screen.getByText("以小写字母开头，只用小写字母、数字、- 或 _，共 2–32 位；且不能是系统保留名。")).toBeTruthy();
  });

  it("does not show required-field errors before the user interacts", () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    renderIntl(<CreateSubagentCard onClose={() => {}} />);

    expect(screen.queryByText("请填写工作说明。")).toBeNull();
    expect(screen.queryByText("请填写用途说明。")).toBeNull();

    // leaving the empty work-instructions field reports it
    fireEvent.blur(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/));
    expect(screen.getByText("请填写工作说明。")).toBeTruthy();
  });

  it("names every missing required field when the user tries to continue", () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    renderIntl(<CreateSubagentCard onClose={() => {}} />);

    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    expect(screen.getByText(/以小写字母开头，只用小写字母、数字/)).toBeTruthy();
    expect(screen.getByText("请填写用途说明。")).toBeTruthy();
    expect(screen.getByText("请填写工作说明。")).toBeTruthy();
    expect(screen.queryByText(/下一步 · 配置/)).toBeNull();
  });

  it("locks unavailable approval tools but lets the Owner retain or remove an existing selection", async () => {
    const ref = "teammcp.crm.update";
    getToolsCatalogMock.mockResolvedValue({
      native_tools: ["bi_query"],
      team_tools: [{ tool_name: ref, operation_id: "update", description: "Update CRM", method: "POST", path: "/crm", is_write: true,
        selectable: false, locked_reason: "approval_executor_unavailable" }],
    });
    patchSubagentMock.mockResolvedValue({ ...EXISTING, tool_count: 2 });
    renderIntl(<CreateSubagentCard editing={{ ...EXISTING, tool_refs: ["bi_query", ref] }} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    const explanation = await screen.findByText(/当前部署无法执行此工具所需的审批流程/);
    const checkbox = explanation.closest("label")!.querySelector("button")!;
    expect(checkbox.disabled).toBe(false);
    expect(checkbox.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByText("保存修改"));
    await waitFor(() => expect(patchSubagentMock).toHaveBeenCalledWith("sa_1", expect.objectContaining({ tool_refs: ["bi_query", ref] }), EXISTING.etag));
    cleanup();
    renderIntl(<CreateSubagentCard editing={{ ...EXISTING, tool_refs: ["bi_query", ref] }} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    const row = (await screen.findByText(/当前部署无法执行此工具所需的审批流程/)).closest("label")!;
    fireEvent.click(row.querySelector("button")!);
    expect(row.querySelector("button")!.disabled).toBe(true);
    expect(row.querySelector("button")!.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(row.querySelector("button")!);
    expect(row.querySelector("button")!.getAttribute("aria-checked")).toBe("false");
  });

  it("prefills from the existing subagent and locks the name (slug)", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query", "python_sandbox"] });
    renderIntl(<CreateSubagentCard editing={EXISTING} onClose={() => {}} />);

    // header reflects edit mode
    expect(screen.getByText("编辑智能体")).toBeTruthy();
    // slug input is prefilled + disabled (name immutable)
    const slug = screen.getByPlaceholderText("sales-analysis") as HTMLInputElement;
    expect(slug.value).toBe("sales-bot");
    expect(slug).toHaveProperty("disabled", true);
    // description prefilled
    expect((screen.getByPlaceholderText(/分析销售数据并生成图表/) as HTMLInputElement).value).toBe("分析销售数据");
    // display_name prefilled (cca P3 follow-up #3)
    expect((screen.getByPlaceholderText("例如：销售数据分析师") as HTMLInputElement).value).toBe("销售助手");
  });

  it("submits via PATCH (not create) with the edited fields", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query", "python_sandbox"] });
    patchSubagentMock.mockResolvedValue({ ...EXISTING, tool_count: 1 });
    const onSaved = vi.fn();
    renderIntl(<CreateSubagentCard editing={EXISTING} onClose={() => {}} onSaved={onSaved} />);

    // edit the display_name (cca P3 follow-up #3) before advancing
    fireEvent.change(screen.getByPlaceholderText("例如：销售数据分析师"), { target: { value: "销售分析助手" } });
    // describe → tools → configure (sel is prefilled from tool_refs, so we can advance)
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    // submit button reads 保存修改 in edit mode
    fireEvent.click(screen.getByText("保存修改"));

    await waitFor(() => expect(patchSubagentMock).toHaveBeenCalledTimes(1));
    expect(createSubagentMock).not.toHaveBeenCalled();
    const [id, body, etag] = patchSubagentMock.mock.calls[0];
    expect(id).toBe("sa_1");
    expect(etag).toBe('W/"sa_1:1"');
    expect(body).toMatchObject({
      display_name: "销售分析助手",
      description: "分析销售数据",
      system_prompt: "你是销售分析师",
      tool_refs: ["bi_query"],
      output_schema: null,
    });
    // PATCH must NOT carry name or visibility (immutable / not patchable)
    expect(body).not.toHaveProperty("name");
    expect(body).not.toHaveProperty("visibility");
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("create mode defaults to creator-only when it submits", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    createSubagentMock.mockResolvedValue({ ...EXISTING, visibility: "creator_only" });
    renderIntl(<CreateSubagentCard onClose={() => {}} onCreated={() => {}} />);

    expect(screen.getByText("新建智能体")).toBeTruthy();
    const slug = screen.getByPlaceholderText("sales-analysis") as HTMLInputElement;
    expect(slug).toHaveProperty("disabled", false);

    // fill the required routing description and work instructions, then advance
    fireEvent.change(slug, { target: { value: "sales-bot" } });
    fireEvent.change(screen.getByPlaceholderText(/分析销售数据并生成图表/), { target: { value: "分析销售" } });
    fireEvent.change(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/), { target: { value: "分析销售数据并说明结论" } });
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByText(/下一步 · 配置/));

    // create-mode submit button label differs from edit ("保存修改")
    expect(screen.getByText("创建智能体")).toBeTruthy();
    expect(patchSubagentMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("创建智能体"));
    await waitFor(() => expect(createSubagentMock).toHaveBeenCalledTimes(1));
    expect(createSubagentMock.mock.calls[0][0].visibility).toBe("creator_only");
    expect(screen.getByText(/直接输入名称不会保证由它执行/)).toBeTruthy();
    expect(screen.queryByText(/@sales-bot/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "管理智能体" }));
    expect(pushMock).toHaveBeenCalledWith("/agents/manage/?agent_id=sa_1");
  });

  it("resets visibility to creator-only when creating another subagent", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    createSubagentMock.mockResolvedValue({ name: "sales-bot", tool_count: 1 });
    renderIntl(<CreateSubagentCard onClose={() => {}} onCreated={() => {}} />);

    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "sales-bot" } });
    fireEvent.change(screen.getByPlaceholderText(/分析销售数据并生成图表/), { target: { value: "分析销售" } });
    fireEvent.change(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/), { target: { value: "分析销售数据并说明结论" } });
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByText("创建智能体"));
    await waitFor(() => expect(createSubagentMock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByText("再建一个"));
    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "sales-bot-2" } });
    fireEvent.change(screen.getByPlaceholderText(/分析销售数据并生成图表/), { target: { value: "继续分析销售" } });
    fireEvent.change(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/), { target: { value: "继续分析销售数据并说明结论" } });
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByText("创建智能体"));

    await waitFor(() => expect(createSubagentMock).toHaveBeenCalledTimes(2));
    expect(createSubagentMock.mock.calls[1][0].visibility).toBe("creator_only");
  });

  it("blocks advancing when an edited subagent references a tool no longer in the catalog", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    renderIntl(<CreateSubagentCard editing={{ ...EXISTING, tool_refs: ["bi_query", "teammcp.crm.gone"] }} onClose={() => {}} />);

    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    expect(await screen.findByText("已失效的工具")).toBeTruthy();
    expect(screen.getByText("teammcp.crm.gone")).toBeTruthy();
    const nextBtn = screen.getByText(/下一步 · 配置/).closest("button") as HTMLButtonElement;
    expect(nextBtn).toHaveProperty("disabled", true);

    // removing the stale ref unblocks the flow
    const staleRow = screen.getByText("teammcp.crm.gone").closest("label") as HTMLElement;
    fireEvent.click(within(staleRow).getByRole("checkbox"));
    await waitFor(() =>
      expect(screen.getByText(/下一步 · 配置/).closest("button") as HTMLButtonElement).toHaveProperty("disabled", false),
    );
  });

  it("renders the skills picker and submits the selected skills (create)", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    getSkillsCatalogMock.mockResolvedValue({
      builtin: [{ name: "analyst-sop-x", description: "builtin sop" }],
      team_skills: [{ name: "team-recipe", display_name: "团队流程", description: "team sop" }],
    });
    renderIntl(<CreateSubagentCard onClose={() => {}} onCreated={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "skilled-bot" } });
    fireEvent.change(screen.getByPlaceholderText(/分析销售数据并生成图表/), { target: { value: "x" } });
    fireEvent.change(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/), { target: { value: "分析数据并说明结论" } });
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(await screen.findByRole("checkbox"));            // pick a tool to satisfy the bundle gate
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    // skills section starts collapsed in create mode → open it, then toggle one team skill on
    fireEvent.click(screen.getByText("自动读取的技能"));
    const row = (await screen.findByText("team-recipe")).closest("label") as HTMLElement;
    fireEvent.click(within(row).getByRole("checkbox"));
    fireEvent.click(screen.getByText("创建智能体"));
    await waitFor(() => expect(createSubagentMock).toHaveBeenCalledTimes(1));
    expect(createSubagentMock.mock.calls[0][0].skills).toEqual(["team-recipe"]);
  });

  it("prefills selected skills in edit mode and round-trips them through PATCH", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    getSkillsCatalogMock.mockResolvedValue({
      builtin: [], team_skills: [{ name: "team-recipe", display_name: "团队流程", description: "team sop" }],
    });
    patchSubagentMock.mockResolvedValue({ ...EXISTING, tool_count: 1 });
    renderIntl(<CreateSubagentCard editing={{ ...EXISTING, skills: ["team-recipe"] }} onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    // editing.skills is non-empty → the section starts open; the prefilled row is checked.
    const row = (await screen.findByText("team-recipe")).closest("label") as HTMLElement;
    expect(within(row).getByRole("checkbox").getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByText("保存修改"));
    await waitFor(() => expect(patchSubagentMock).toHaveBeenCalledTimes(1));
    expect(patchSubagentMock.mock.calls[0][1].skills).toEqual(["team-recipe"]);
  });

  it("shows prefilled skills as rows while the catalog is still loading (edit)", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    getSkillsCatalogMock.mockReturnValue(new Promise(() => {}));  // never resolves → catalog stays null
    renderIntl(<CreateSubagentCard editing={{ ...EXISTING, skills: ["team-recipe"] }} onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    // the prefilled skill renders (checked, removable) even before the catalog resolves —
    // not hidden behind "加载中…". (Old guard `skillsCatalog ? … : []` would drop it here.)
    const row = (await screen.findByText("team-recipe")).closest("label") as HTMLElement;
    expect(within(row).getByRole("checkbox").getAttribute("aria-checked")).toBe("true");
  });

  // ─── Tool checkbox list: identifiers must not serve as descriptions (capability-surface-audit wrap-up) ────
  //
  // Real-device dogfood on 116, 2026-09-07: 7 of this list's 28 descriptions were identifiers themselves
  // (`pbi_execute_dax` / `web_fetch` / `python_exec` …); others used name prefixes to produce
  // one generic family description. Catalog endpoint returned only **bare tool-name arrays**, forcing component guesses.
  //
  // Mutation verification: changing `nativeMeta` `desc` back to `name` fails both tests in this group.
  it("每个内置工具都渲人话名 + 一句话说明，不渲代号", async () => {
    // Sample spans five active built-in tool families: bi / compute / kb / web / canvas. Power BI `pbi_*` moved out in e5f810a86
    // of built-in tool table (now team built-in third-party tools, absent from native_tools); replace with kb_search.
    const natives = ["bi_query", "python_sandbox", "kb_search", "web_fetch", "canvas_add_spec_block"];
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: natives });
    renderIntl(<CreateSubagentCard editing={EXISTING} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));

    for (const name of natives) {
      const code = await screen.findByText(name);
      const row = code.closest("label") as HTMLElement;
      const text = row.textContent ?? "";
      // Description must not be identifier itself; human-readable name must appear
      const display = toolDisplay(name);
      expect(display.label, `${name} 没有展示名`).not.toBe(name);
      expect(text, `${name} 行没渲展示名`).toContain(display.label);
      expect(text, `${name} 行没渲说明`).toContain(display.blurb);
    }
  });

  // Team-tool row names must omit `teammcp.<source_id>.` prefix.
  //
  // ★ Same assertions for both source types — first version routed by field name (used nonempty `operation_id` directly),
  // but OpenAPI source `operation_id` **is the full string** (`openapi_tool_source.py:405`:
  // OpenAPI dispatch_name == advertised_name); after the fix, real-device text was unchanged.
  // Testing only MCP would still pass.
  it.each([
    ["OpenAPI 源（operation_id 就是整串）", "teammcp.ots_819dfef35887.addPet", "teammcp.ots_819dfef35887.addPet", "addPet"],
    ["MCP 源（operation_id 是对端原名）", "teammcp.ots_819dfef35887.search_docs", "search_docs", "search_docs"],
    ["无 operation_id 时回落 tool_name", "teammcp.ots_x.getPetById", "", "getPetById"],
  ])("团队工具行不渲内部 source id — %s", async (_label, toolName, operationId, expected) => {
    getToolsCatalogMock.mockResolvedValue({
      team_tools: [{
        tool_name: toolName,
        operation_id: operationId,
        description: "Add a new pet to the store.",
        is_write: true,
        method: "POST",
        path: "/pet",
      }],
      native_tools: [],
    });
    renderIntl(<CreateSubagentCard editing={EXISTING} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));

    const desc = await screen.findByText("Add a new pet to the store.");
    const row = desc.closest("label") as HTMLElement;
    const text = row.textContent ?? "";
    expect(text, `行里泄漏了内部 source id：${text}`).not.toContain("teammcp.");
    expect(text, `行里泄漏了内部 source id：${text}`).not.toContain("ots_");
    expect(text, `行名不是干净的 operation：${text}`).toContain(expected);
  });

  it("搜索框认人话名（不必输工具代号）", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["python_sandbox", "bi_query"] });
    renderIntl(<CreateSubagentCard editing={EXISTING} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    await screen.findByText("python_sandbox");

    const box = screen.getByPlaceholderText(/搜索|filter|Search/i);
    fireEvent.change(box, { target: { value: "运行代码" } });
    expect(await screen.findByText("python_sandbox")).toBeTruthy();
    expect(screen.queryByText("bi_query")).toBeNull();
  });
});

// These assert user-visible copy with the real locale catalogs. The legacy
// is_write bit intentionally includes unknown risk; it is not a display label
// or a promise that an existing selected tool can execute.
const PICKER_COPY = [
  {
    locale: "zh-CN", team: "团队工具", native: "内置分析工具",
    count: (n: number) => `共 ${n} 项`,
    unknown: "风险未知", read: "只读", write: "修改外部系统", destructive: "破坏性操作",
    retained: "已选工具中包含当前无法执行审批流程的工具。已有绑定可保留或移除；保留不代表可以执行。",
    guarded: "修改外部系统中的内容。",
  },
  {
    locale: "en-US", team: "Team tools", native: "Built-in analysis tools",
    count: (n: number) => `${n} ${n === 1 ? "tool" : "tools"}`,
    unknown: "Unknown risk", read: "Read only", write: "Modifies an external system", destructive: "Destructive operation",
    retained: "Some selected tools cannot currently complete their approval flow. Existing bindings may be kept or removed; keeping them does not make them executable.",
    guarded: "change content in another system",
  },
] as const;

const PICKER_ROWS = [
  { tool_name: "teammcp.crm.lookup", operation_id: "lookup", description: "Read records", method: "GET", path: "/crm", is_write: false,
    risk: "read", selectable: true, locked_reason: null },
  { tool_name: "teammcp.crm.update", operation_id: "update", description: "Update records", method: "POST", path: "/crm", is_write: true,
    risk: "external_write", selectable: false, locked_reason: "approval_executor_unavailable" },
  { tool_name: "teammcp.crm.mystery", operation_id: "mystery", description: "Risk unverified", method: null, path: null, is_write: true,
    risk: "unknown", selectable: false, locked_reason: "risk_metadata_unknown" },
];

function openPicker(locale: "zh-CN" | "en-US", toolRefs = ["bi_query"]) {
  renderIntl(<CreateSubagentCard editing={{ ...EXISTING, tool_refs: toolRefs }} onClose={() => {}} />, { locale });
  fireEvent.click(screen.getByText(/下一步 · 选择工具|Next · Choose tools/));
}

describe.each(PICKER_COPY)("authoring picker user-facing facts ($locale)", (copy) => {
  it("labels filtered catalog totals, including locked rows, without promising availability", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: PICKER_ROWS, native_tools: ["bi_query", "python_sandbox"] });
    openPicker(copy.locale);
    await screen.findByText("lookup");
    const teamHeader = screen.getByText(copy.team).parentElement!;
    const nativeHeader = screen.getByText(copy.native).parentElement!;
    expect(within(teamHeader).getByText(copy.count(3))).toBeTruthy();
    expect(within(nativeHeader).getByText(copy.count(2))).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText(/搜索工具|Search tools/), { target: { value: "mystery" } });
    expect(within(teamHeader).getByText(copy.count(1))).toBeTruthy();
    expect(within(nativeHeader).getByText(copy.count(0))).toBeTruthy();
    expect(screen.getByText("mystery").closest("label")!.querySelector("button")!.disabled).toBe(true);
  });

  it("renders authoritative risk labels instead of the legacy write bit", async () => {
    getToolsCatalogMock.mockResolvedValue({
      team_tools: [...PICKER_ROWS, { ...PICKER_ROWS[1], tool_name: "teammcp.crm.delete", operation_id: "delete", risk: "destructive" }],
      native_tools: ["bi_query"],
    });
    openPicker(copy.locale);
    await screen.findByText("mystery");
    for (const [operation, label] of [["lookup", copy.read], ["update", copy.write], ["mystery", copy.unknown], ["delete", copy.destructive]]) {
      const row = screen.getByText(operation).closest("label")!;
      expect(within(row).getByText(label, { exact: true })).toBeTruthy();
    }
    const unknownRow = screen.getByText("mystery").closest("label")!;
    expect(unknownRow.querySelector("button")!.disabled).toBe(true);
    expect(within(unknownRow).queryByText(copy.write, { exact: true })).toBeNull();
  });

  it("does not infer verified risk from the write bit when risk metadata is absent", async () => {
    const { risk: _risk, ...withoutRisk } = PICKER_ROWS[2];
    getToolsCatalogMock.mockResolvedValue({ team_tools: [withoutRisk], native_tools: ["bi_query"] });
    openPicker(copy.locale);
    const row = (await screen.findByText("mystery")).closest("label")!;
    expect(within(row).getByText(copy.unknown, { exact: true })).toBeTruthy();
    expect(row.querySelector("button")!.disabled).toBe(true);
  });

  it("explains retained approval locks and removes the warning when deselected", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: PICKER_ROWS, native_tools: ["bi_query"] });
    openPicker(copy.locale, ["bi_query", "teammcp.crm.update"]);
    await screen.findByText("update");
    expect(screen.getByText(copy.retained)).toBeTruthy();
    expect(screen.queryByText(copy.guarded, { exact: false })).toBeNull();
    const checkbox = screen.getByText("update").closest("label")!.querySelector("button")!;
    expect(checkbox.disabled).toBe(false);
    expect(checkbox.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(checkbox);
    expect(screen.queryByText(copy.retained)).toBeNull();
    expect(checkbox.disabled).toBe(true);
    expect(checkbox.getAttribute("aria-checked")).toBe("false");
  });

  it("separates an approval-locked selection from an eligible external-write selection", async () => {
    getToolsCatalogMock.mockResolvedValue({
      team_tools: [...PICKER_ROWS, { ...PICKER_ROWS[1], tool_name: "teammcp.crm.ready", operation_id: "ready", selectable: true, locked_reason: null }],
      native_tools: ["bi_query"],
    });
    openPicker(copy.locale, ["bi_query", "teammcp.crm.update", "teammcp.crm.ready"]);
    await screen.findByText("ready");
    expect(screen.getByText(copy.retained)).toBeTruthy();
    expect(screen.getByText(copy.guarded, { exact: false })).toBeTruthy();
    fireEvent.click(screen.getByText("ready").closest("label")!.querySelector("button")!);
    expect(screen.queryByText(copy.guarded, { exact: false })).toBeNull();
    expect(screen.getByText(copy.retained)).toBeTruthy();
  });
});


describe("text-only manual creation", () => {
  function fillDescription() {
    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "text-editor" } });
    fireEvent.change(screen.getByPlaceholderText(/分析销售数据并生成图表/), { target: { value: "整理提供的文字" } });
    fireEvent.change(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/), { target: { value: "仅整理用户提供的文字。" } });
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
  }

  it("creates a private agent with an explicit empty tool and skill selection", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: true });
    createSubagentMock.mockResolvedValue({ ...EXISTING, name: "text-editor", tool_count: 0 });
    renderIntl(<CreateSubagentCard onClose={() => {}} />);
    fillDescription();
    expect(screen.getByRole("searchbox", { name: "搜索工具…" })).toBeTruthy();
    await screen.findByText(/仅处理你提供的文字时/);
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));
    await waitFor(() => expect(createSubagentMock).toHaveBeenCalledWith(expect.objectContaining({
      name: "text-editor", tool_refs: [], skills: [], visibility: "creator_only",
    })));
  });

  it.each([false, undefined])("requires a tool without server capability (%s)", async (capability) => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: capability });
    renderIntl(<CreateSubagentCard onClose={() => {}} />);
    fillDescription();
    await screen.findByText(toolDisplay("bi_query").label);
    const next = screen.getByText(/下一步 · 配置/).closest("button") as HTMLButtonElement;
    expect(next).toHaveProperty("disabled", true);
    // the disabled Next says why
    const hint = screen.getByText("当前团队的智能体需要至少选择 1 个工具，选好后才能继续。");
    expect(next.getAttribute("aria-describedby")).toBe(hint.id);
    expect(createSubagentMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("checkbox"));
    expect(next).toHaveProperty("disabled", false);
    expect(screen.queryByText("当前团队的智能体需要至少选择 1 个工具，选好后才能继续。")).toBeNull();
    fireEvent.click(next);
    expect(screen.getByRole("region", { name: "创建前核对工具权限" }).textContent).toContain(toolDisplay("bi_query").label);
  });

  it("does not allow empty creation when catalog loading fails", async () => {
    getToolsCatalogMock.mockRejectedValue(new Error("unavailable"));
    renderIntl(<CreateSubagentCard onClose={() => {}} />);
    fillDescription();
    await screen.findByText(/工具目录没能加载/);
    expect(screen.getByText(/下一步 · 配置/).closest("button")).toHaveProperty("disabled", true);
    // the load-failure callout is the explanation; no second, misleading reason
    expect(screen.queryByText(/需要至少选择 1 个工具/)).toBeNull();
  });

  it("keeps an existing empty definition editable after rollout is closed", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: false });
    patchSubagentMock.mockResolvedValue({ ...EXISTING, tool_count: 0 });
    renderIntl(<CreateSubagentCard editing={{ ...EXISTING, tool_refs: [], tool_count: 0 }} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    await screen.findByText(/仅处理你提供的文字时/);
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByText("保存修改"));
    await waitFor(() => expect(patchSubagentMock).toHaveBeenCalledWith(EXISTING.id,
      expect.objectContaining({ tool_refs: [], skills: [] }), EXISTING.etag));
  });
});

describe("edit mode conflict", () => {
  it("a 409 while editing an existing agent explains the agent changed elsewhere, not a Chat draft update", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"] });
    patchSubagentMock.mockRejectedValue(Object.assign(new Error("etag mismatch"), { status: 409, code: "subagent_version_conflict" }));
    renderIntl(<CreateSubagentCard editing={EXISTING} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByText("保存修改"));

    expect(await screen.findByText(zhMessages.create_subagent.issue_edit_conflict)).toBeTruthy();
    expect(screen.queryByText(zhMessages.create_subagent.draft_chat_conflict)).toBeNull();
  });
});

describe("unified manual draft", () => {
  it("resolves a cross-tab version conflict per field before saving", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
    const initial = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_conflict", draft_status: "editing" as const,
      source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
      etag: 'W/"sad_conflict:1"', version: 1, purpose: "分析销售",
      definition: { name: "sales-bot", display_name: "销售助手", description: "分析销售", system_prompt: "分析证据", tool_refs: ["bi_query"], max_iterations: 12, model: "analyst", output_schema: null, output_schema_text: "" },
      warnings: [], validation: { status: "unknown", issues: [] },
      mutation_id: "11111111-1111-4111-8111-111111111111", expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    const latest = { ...initial, version: 2, etag: 'W/"sad_conflict:2"', definition: { ...initial.definition, description: "只分析本周销售" } };
    saveManualDraftMock.mockRejectedValueOnce(new SubagentDraftApiError(409, "subagent_draft_version_conflict", "version changed"));
    saveManualDraftMock.mockImplementation(async (_draft, purpose, definition) => ({
      ...latest, version: 3, etag: 'W/"sad_conflict:3"', purpose, definition,
    }));
    getManualDraftMock.mockResolvedValue(latest);
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={initial} onClose={vi.fn()} />);

    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "my-name" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    const conflict = await screen.findByRole("group", { name: "对话刚更新了这份草稿" });
    expect(saveManualDraftMock).toHaveBeenCalledTimes(1);
    const rows = within(conflict).getAllByRole("group");
    expect(rows).toHaveLength(2);
    fireEvent.click(within(rows[0]).getByRole("radio", { name: /保留我的/ }));
    fireEvent.click(within(rows[1]).getByRole("radio", { name: /用最新的/ }));
    fireEvent.click(within(conflict).getByRole("button", { name: "确认选择" }));
    await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledWith(
      expect.objectContaining({ etag: 'W/"sad_conflict:2"' }), "分析销售",
      expect.objectContaining({ name: "my-name", description: "只分析本周销售" }), false,
    ), { timeout: 2500 });
  });

  it("keeps built-in Plugins chosen elsewhere when the latest Plugin selection is taken in a conflict", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
    getAgentAuthoringCatalogMock.mockResolvedValue({ plugins: {
      builtin: [
        {
          id: "builtin:designs", name: "designs", version: 1, family: "general",
          listing: { "zh-CN": { name: "设计稿", description: "创建设计稿" } }, tools: ["canvas_create_design"], prompt_modules: [],
          ready: true, unavailable_tools: [], selectable: true, locked_reason: null, team_disabled: false, my_disabled: false,
        },
        {
          id: "builtin:web-research", name: "web-research", version: 1, family: "general",
          listing: { "zh-CN": { name: "联网研究", description: "搜索网页" } }, tools: ["web_search"], prompt_modules: [],
          ready: true, unavailable_tools: [], selectable: true, locked_reason: null, team_disabled: false, my_disabled: false,
        },
      ],
      team: [],
    } });
    const initial = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_plugins", draft_status: "editing" as const,
      source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
      etag: 'W/"sad_plugins:1"', version: 1, purpose: "分析销售",
      definition: { name: "sales-bot", display_name: "销售助手", description: "分析销售", system_prompt: "分析证据", tool_refs: ["bi_query"], max_iterations: 12, model: "analyst", output_schema: null, output_schema_text: "" },
      warnings: [], validation: { status: "unknown", issues: [] },
      mutation_id: "11111111-1111-4111-8111-111111111111", expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    // Coach or a canvas suggestion added a built-in Plugin to the server draft meanwhile.
    const latest = { ...initial, version: 2, etag: 'W/"sad_plugins:2"', definition: { ...initial.definition, builtin_plugin_ids: ["builtin:designs"] } };
    saveManualDraftMock.mockRejectedValueOnce(new SubagentDraftApiError(409, "subagent_draft_version_conflict", "version changed"));
    saveManualDraftMock.mockImplementation(async (_draft, purpose, definition) => ({
      ...latest, version: 3, etag: 'W/"sad_plugins:3"', purpose, definition,
    }));
    getManualDraftMock.mockResolvedValue(latest);
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={initial} onClose={vi.fn()} />);

    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "my-name" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    const conflict = await screen.findByRole("group", { name: "对话刚更新了这份草稿" });
    const rows = within(conflict).getAllByRole("group");
    expect(rows).toHaveLength(2);
    fireEvent.click(within(rows[0]).getByRole("radio", { name: /保留我的/ }));
    fireEvent.click(within(rows[1]).getByRole("radio", { name: /用最新的/ }));
    fireEvent.click(within(conflict).getByRole("button", { name: "确认选择" }));
    await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledWith(
      expect.objectContaining({ etag: 'W/"sad_plugins:2"' }), "分析销售",
      expect.objectContaining({ name: "my-name", builtin_plugin_ids: ["builtin:designs"] }), false,
    ), { timeout: 2500 });
  });

  it("automatically saves changed fields to the same draft after a short pause", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: true });
    const initial = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_auto", draft_status: "editing" as const,
      source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
      etag: 'W/"sad_auto:1"', version: 1, purpose: "", definition: {}, warnings: [],
      validation: { status: "unknown", issues: [] }, mutation_id: "11111111-1111-4111-8111-111111111111",
      expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    saveManualDraftMock.mockImplementation(async (_draft, purpose, definition) => ({
      ...initial, etag: 'W/"sad_auto:2"', version: 2, purpose, definition,
    }));
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={initial} onClose={vi.fn()} />);

    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "text-helper" } });
    await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledWith(
      expect.objectContaining({ ...initial, requestContext: { teamId: expect.any(String), workspaceId: "ws_a" } }), "", expect.objectContaining({ name: "text-helper" }), false,
    ), { timeout: 2500 });
    await waitFor(() => expect(screen.getByText("草稿已保存，可从我的智能体继续；当前请在右侧修改。")).toBeTruthy());
    expect(saveManualDraftMock).toHaveBeenCalledTimes(1);
  });

  it("saves a built-in Plugin picked in the compact picker as builtin_plugin_ids", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: true });
    getAgentAuthoringCatalogMock.mockResolvedValue({
      plugins: {
        builtin: [{
          id: "builtin:designs", name: "designs", version: 1, family: "general",
          listing: { "zh-CN": { name: "设计稿", description: "创建设计稿" } }, tools: ["canvas_create_design"], prompt_modules: [],
          ready: true, unavailable_tools: [], selectable: true, locked_reason: null, team_disabled: false, my_disabled: false,
        }],
        team: [],
      },
    });
    const initial = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_plugin", draft_status: "editing" as const,
      source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
      etag: 'W/"sad_plugin:1"', version: 1, purpose: "", definition: {}, warnings: [],
      validation: { status: "unknown", issues: [] }, mutation_id: "11111111-1111-4111-8111-111111111111",
      expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    saveManualDraftMock.mockImplementation(async (_draft, purpose, definition) => ({
      ...initial, etag: 'W/"sad_plugin:2"', version: 2, purpose, definition,
    }));
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={initial} onClose={vi.fn()} />);

    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "design-helper" } });
    fireEvent.change(screen.getByPlaceholderText(/分析销售数据并生成图表/), { target: { value: "做设计稿" } });
    fireEvent.change(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/), { target: { value: "按要求创建设计稿" } });
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    fireEvent.click(await screen.findByRole("button", { name: "+ 设计稿" }));
    await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledWith(
      expect.anything(), "", expect.objectContaining({ builtin_plugin_ids: ["builtin:designs"] }), false,
    ), { timeout: 2500 });
  });

  it("counts Plugin Tools in the canvas heading and drops the tool-required hint once a Plugin is picked", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: false });
    getAgentAuthoringCatalogMock.mockResolvedValue({
      plugins: {
        builtin: [
          {
            id: "builtin:data-analysis", name: "data-analysis", version: 1, family: "general",
            listing: { "zh-CN": { name: "数据分析", description: "查询数据" } }, tools: ["bi_query", "bi_schema"], prompt_modules: [],
            ready: true, unavailable_tools: [], selectable: true, locked_reason: null, team_disabled: false, my_disabled: false,
          },
          {
            id: "builtin:settings-space-metadata", name: "settings-space-metadata", version: 3, family: "settings",
            listing: {}, tools: [], prompt_modules: [],
            ready: true, unavailable_tools: [], selectable: false, locked_reason: "settings_assistant_only", team_disabled: false, my_disabled: false,
          },
        ],
        team: [],
      },
    });
    const initial = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_canvas_plugin", draft_status: "editing" as const,
      source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
      etag: 'W/"sad_canvas_plugin:1"', version: 1, purpose: "", definition: {}, warnings: [],
      validation: { status: "unknown", issues: [] }, mutation_id: "11111111-1111-4111-8111-111111111111",
      expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    saveManualDraftMock.mockImplementation(async (_draft, purpose, definition) => ({
      ...initial, etag: 'W/"sad_canvas_plugin:2"', version: 2, purpose, definition,
    }));
    renderIntl(<CreateSubagentCard unifiedDraftEnabled presentation="canvas" manualDraft={initial} onClose={vi.fn()} />);

    const plugin = await screen.findByRole("button", { name: "+ 数据分析" });
    expect(screen.getByText("只用文字回答")).toBeTruthy();
    expect(screen.getByText(/当前团队的智能体需要至少选择 1 个工具/)).toBeTruthy();
    // Settings Plugins are never offered to custom Agents.
    expect(screen.queryByRole("button", { name: /空间信息/ })).toBeNull();
    expect(screen.queryByText("settings-space-metadata")).toBeNull();
    fireEvent.click(plugin);
    // A Plugin counts as a Plugin, not as the Tools it brings, matching the chips the user picked.
    expect(await screen.findByText("1 个插件")).toBeTruthy();
    // A selected built-in Plugin appears in the selected row and can be removed there.
    expect(screen.getByRole("button", { name: /移除.*数据分析|数据分析.*移除/ })).toBeTruthy();
    expect(screen.queryByText(/当前团队的智能体需要至少选择 1 个工具/)).toBeNull();
  });

  it("does not autosave changes while the user is deciding whether to discard them", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: true });
    const initial = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_discard", draft_status: "editing" as const,
      source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
      etag: 'W/"sad_discard:1"', version: 1, purpose: "", definition: {}, warnings: [],
      validation: { status: "unknown", issues: [] }, mutation_id: "11111111-1111-4111-8111-111111111111",
      expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    const onClose = vi.fn();
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={initial} onClose={onClose} />);

    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "not-saved" } });
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.getByRole("group", { name: "离开前处理未保存的修改" })).toBeTruthy();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1200)); });
    expect(saveManualDraftMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "放弃未保存修改" }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(saveManualDraftMock).not.toHaveBeenCalled();
  });

  it("guards both app routes and session changes until an unsaved draft reaches the server", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: true });
    const initial = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_navigation", draft_status: "editing" as const,
      source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
      etag: 'W/"sad_navigation:1"', version: 1, purpose: "", definition: {}, warnings: [],
      validation: { status: "unknown", issues: [] }, mutation_id: "11111111-1111-4111-8111-111111111111",
      expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    saveManualDraftMock.mockImplementation(async (_draft, purpose, definition) => ({
      ...initial, etag: 'W/"sad_navigation:2"', version: 2, purpose, definition,
    }));
    const onClose = vi.fn();
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={initial} onClose={onClose} />);
    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "not-yet-saved" } });

    const proceed = vi.fn();
    const route = new CustomEvent("bowhead:before-app-navigation", {
      cancelable: true, detail: { href: "/reports/", proceed },
    });
    let routeAllowed = true;
    act(() => { routeAllowed = window.dispatchEvent(route); });
    expect(routeAllowed).toBe(false);
    expect(proceed).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    await act(async () => {});
    expect(proceed).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    let sessionAllowed: boolean | undefined;
    act(() => { void confirmCanvasNavigation().then((allowed) => { sessionAllowed = allowed; }); });
    expect(screen.getByRole("group", { name: "离开前处理未保存的修改" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    await waitFor(() => expect(sessionAllowed).toBe(false));

    const resumedRoute = new CustomEvent("bowhead:before-app-navigation", {
      cancelable: true, detail: { href: "/reports/", proceed },
    });
    act(() => { expect(window.dispatchEvent(resumedRoute)).toBe(false); });
    const latestProceed = vi.fn();
    const latestRoute = new CustomEvent("bowhead:before-app-navigation", {
      cancelable: true, detail: { href: "/agents/", proceed: latestProceed },
    });
    act(() => { expect(window.dispatchEvent(latestRoute)).toBe(false); });
    fireEvent.click(screen.getByRole("button", { name: "保存并离开" }));
    await waitFor(() => expect(latestProceed).toHaveBeenCalledOnce());
    expect(proceed).not.toHaveBeenCalled();
    expect(saveManualDraftMock).toHaveBeenCalledWith(expect.objectContaining({ ...initial, requestContext: { teamId: expect.any(String), workspaceId: "ws_a" } }), "", expect.objectContaining({ name: "not-yet-saved" }), false);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("saves an edit made during an in-flight save against the returned version", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: true });
    const initial = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_auto", draft_status: "editing" as const,
      source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
      etag: 'W/"sad_auto:1"', version: 1, purpose: "", definition: {}, warnings: [],
      validation: { status: "unknown", issues: [] }, mutation_id: "11111111-1111-4111-8111-111111111111",
      expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    let finishFirst!: () => void;
    saveManualDraftMock.mockImplementationOnce((_draft, purpose, definition) => new Promise((resolve) => {
      finishFirst = () => resolve({ ...initial, version: 2, etag: 'W/"sad_auto:2"', purpose, definition });
    }));
    saveManualDraftMock.mockImplementation(async (_draft, purpose, definition) => ({
      ...initial, version: 3, etag: 'W/"sad_auto:3"', purpose, definition,
    }));
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={initial} onClose={vi.fn()} />);

    const slug = screen.getByPlaceholderText("sales-analysis");
    fireEvent.change(slug, { target: { value: "first" } });
    await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledTimes(1), { timeout: 2500 });
    fireEvent.change(slug, { target: { value: "second" } });
    finishFirst();
    await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledWith(
      expect.objectContaining({ etag: 'W/"sad_auto:2"' }), "",
      expect.objectContaining({ name: "second" }), false,
    ), { timeout: 2500 });
  });

  it("binds Chat to the saved manual draft and refreshes only when local fields are clean", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
    const initial = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_manual", draft_status: "editing" as const,
      source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
      etag: 'W/"sad_manual:1"', version: 1, purpose: "分析销售",
      definition: { name: "sales-bot", display_name: "销售助手", description: "分析销售", system_prompt: "分析证据", tool_refs: ["bi_query"], max_iterations: 12, model: "analyst", output_schema: null, output_schema_text: "" },
      warnings: [], validation: { status: "unknown", issues: [] },
      mutation_id: "11111111-1111-4111-8111-111111111111", expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    const onDraftChange = vi.fn();
    getManualDraftMock.mockResolvedValue({ ...initial, version: 2, etag: 'W/"sad_manual:2"', definition: { ...initial.definition, description: "只分析本周销售" } });
    function Harness() {
      const [version, setVersion] = useState<number | null>(null);
      return <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <button onClick={() => setVersion((current) => (current ?? 1) + 1)}>刷新版本</button>
        <CreateSubagentCard unifiedDraftEnabled manualDraft={initial}
          refreshVersion={version} onDraftChange={onDraftChange} onClose={vi.fn()} />
      </NextIntlClientProvider>;
    }
    renderIntl(<Harness />);
    await waitFor(() => expect(onDraftChange).toHaveBeenCalledWith({ draftId: "sad_manual", version: 1, dirty: false }));
    fireEvent.click(screen.getByText("刷新版本"));
    await waitFor(() => expect(screen.getByRole("textbox", { name: /用途说明/ })).toHaveProperty("value", "只分析本周销售"));
    expect(onDraftChange).toHaveBeenCalledWith({ draftId: "sad_manual", version: 2, dirty: false });
    fireEvent.change(screen.getByRole("textbox", { name: /用途说明/ }), { target: { value: "保留我的未保存修改" } });
    await waitFor(() => expect(onDraftChange).toHaveBeenCalledWith({ draftId: "sad_manual", version: 2, dirty: true }));
    fireEvent.click(screen.getByText("刷新版本"));
    expect(screen.getByRole("textbox", { name: /用途说明/ })).toHaveProperty("value", "保留我的未保存修改");
    expect(getManualDraftMock).toHaveBeenCalledTimes(1);
  });

  it("saves an incomplete form before closing and keeps its resume id", async () => {
    const created = {
      draft_id: "sad_manual", etag: 'W/"sad_manual:1"', purpose: "",
      definition: {}, warnings: [], version: 1,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    createManualDraftMock.mockResolvedValue(created);
    saveManualDraftMock.mockImplementation(async (_draft, purpose, definition) => ({
      ...created, etag: 'W/"sad_manual:2"', version: 2, purpose, definition,
    }));
    const onClose = vi.fn();
    const onManualDraftSaved = vi.fn();
    renderIntl(<CreateSubagentCard unifiedDraftEnabled onClose={onClose}
      onManualDraftSaved={onManualDraftSaved} />);
    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "web-reader" } });
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "保存并离开" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(createManualDraftMock).toHaveBeenCalledTimes(1);
    expect(saveManualDraftMock).toHaveBeenCalledWith(
      created, "", expect.objectContaining({ name: "web-reader", visibility: "creator_only" }), false,
    );
    expect(onManualDraftSaved).toHaveBeenCalledWith("sad_manual");
    expect(createSubagentMock).not.toHaveBeenCalled();
  });

  it("keeps unsaved input available for retry or explicit discard after a failed save", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: true });
    createManualDraftMock.mockRejectedValue(new Error("offline"));
    const onClose = vi.fn();
    renderIntl(<CreateSubagentCard unifiedDraftEnabled onClose={onClose} />);
    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "web-reader" } });
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    fireEvent.click(screen.getByRole("button", { name: "保存并离开" }));
    await screen.findByRole("alert");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("sales-analysis")).toHaveProperty("value", "web-reader");
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.queryByRole("group", { name: "离开前处理未保存的修改" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    fireEvent.click(screen.getByRole("button", { name: "放弃未保存修改" }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});

const SAVE_DRAFT = {
  schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
  draft_id: "sad_serial", draft_status: "editing" as const,
  source_mode: "manual" as const, entry_mode: "canvas" as const, workspace_id: "ws_a",
  etag: 'W/"sad_serial:1"', version: 1, purpose: "Analyze sales",
  definition: { name: "sales-bot", display_name: "Sales helper", description: "Analyze sales", system_prompt: "Analyze evidence", tool_refs: ["bi_query"], max_iterations: 12, model: "analyst", output_schema: null, output_schema_text: "" },
  warnings: [], validation: { status: "unknown", issues: [] },
  mutation_id: "11111111-1111-4111-8111-111111111111", expires_at: "2026-10-01T00:00:00Z", result: null,
  actions: { edit: true, validate: true, create: false, discard: true },
};

describe("draft save lifecycle", () => {
  it("keeps a conversational request as purpose without treating it as confirmed instructions", () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
    const purpose = "Create an agent that compares revenue and produces an evidence-based report";
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={{ ...SAVE_DRAFT, source_mode: "turn", purpose, definition: {} }}
      purposeHint={purpose} onClose={vi.fn()} />);
    expect(screen.getByPlaceholderText(/分析销售数据并生成图表/)).toHaveProperty("value", "");
    expect(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/)).toHaveProperty("value", "");
    expect(saveManualDraftMock).not.toHaveBeenCalled();
  });

  it("saves newer input after an in-flight save without declaring the newer input clean", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
    let finishSave!: (draft: typeof SAVE_DRAFT) => void;
    saveManualDraftMock.mockReturnValueOnce(new Promise((resolve) => { finishSave = resolve; }));
    saveManualDraftMock.mockImplementation(async (current, purpose, definition) => ({
      ...current, version: 3, etag: 'W/"sad_serial:3"', purpose, definition,
    }));
    const onDraftChange = vi.fn();
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={SAVE_DRAFT}
      onDraftChange={onDraftChange} onClose={vi.fn()} />);
    const name = screen.getByPlaceholderText("sales-analysis");
    fireEvent.change(name, { target: { value: "first-edit" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledTimes(1));
    expect(onDraftChange).toHaveBeenLastCalledWith({ draftId: "sad_serial", version: 1, dirty: true });
    fireEvent.change(name, { target: { value: "latest-edit" } });
    await act(async () => { finishSave({ ...SAVE_DRAFT, version: 2, etag: 'W/"sad_serial:2"', definition: { ...SAVE_DRAFT.definition, name: "first-edit" } }); });
    expect(name).toHaveProperty("value", "latest-edit");
    expect(onDraftChange).toHaveBeenLastCalledWith({ draftId: "sad_serial", version: 2, dirty: true });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledTimes(2));
    expect(saveManualDraftMock.mock.calls[1]).toEqual([
      expect.objectContaining({ version: 2 }), "Analyze sales", expect.objectContaining({ name: "latest-edit" }), false,
    ]);
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith({ draftId: "sad_serial", version: 3, dirty: false }));
  });

  it("coalesces a save and save-before-leaving into one revision", async () => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
    let finishSave!: (draft: typeof SAVE_DRAFT) => void;
    saveManualDraftMock.mockReturnValueOnce(new Promise((resolve) => { finishSave = resolve; }));
    const onClose = vi.fn();
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={SAVE_DRAFT} onClose={onClose} />);
    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "latest-edit" } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    let leaveAllowed: boolean | undefined;
    act(() => { void confirmCanvasNavigation().then((allowed) => { leaveAllowed = allowed; }); });
    fireEvent.click(screen.getByRole("button", { name: "保存并离开" }));
    expect(saveManualDraftMock).toHaveBeenCalledTimes(1);
    await act(async () => { finishSave({ ...SAVE_DRAFT, version: 2, etag: 'W/"sad_serial:2"', definition: { ...SAVE_DRAFT.definition, name: "latest-edit" } }); });
    await waitFor(() => expect(leaveAllowed).toBe(true));
    expect(saveManualDraftMock).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });

  it.each([false, true])("does not restore a closed editor through a late save or leave callback (%s)", async (leave) => {
    getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
    let finishSave!: (draft: typeof SAVE_DRAFT) => void;
    saveManualDraftMock.mockReturnValueOnce(new Promise((resolve) => { finishSave = resolve; }));
    const onManualDraftSaved = vi.fn();
    const onClose = vi.fn();
    const view = renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={SAVE_DRAFT}
      onManualDraftSaved={onManualDraftSaved} onClose={onClose} />);
    fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "late-edit" } });
    if (leave) {
      fireEvent.click(screen.getByRole("button", { name: "关闭" }));
      fireEvent.click(screen.getByRole("button", { name: "保存并离开" }));
    } else fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledOnce());
    view.unmount();
    await act(async () => { finishSave({ ...SAVE_DRAFT, version: 2 }); });
    expect(onManualDraftSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});

it("blocks repeat writes and disposal after an unknown save until a newer canonical draft is read", async () => {
  getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
  saveManualDraftMock.mockRejectedValueOnce(new TypeError("Connection interrupted"));
  getManualDraftMock.mockResolvedValueOnce(SAVE_DRAFT).mockResolvedValueOnce({ ...SAVE_DRAFT, version: 2, etag: 'W/"sad_serial:2"', definition: { ...SAVE_DRAFT.definition, name: "changed-name" } });
  const onClose = vi.fn();
  renderIntl(<CreateSubagentCard unifiedDraftEnabled continuous manualDraft={SAVE_DRAFT} onClose={onClose} />);
  fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "changed-name" } });
  fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
  await screen.findByText(/请先核对已保存的草稿/);
  expect(screen.getByRole("button", { name: "保存草稿" })).toHaveProperty("disabled", true);
  await expect(confirmCanvasNavigation()).resolves.toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "核对已保存草稿" }));
  await waitFor(() => expect(getManualDraftMock).toHaveBeenCalledTimes(1));
  expect(screen.getByText(/请先核对已保存的草稿/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "核对已保存草稿" }));
  await waitFor(() => expect(screen.queryByText(/请先核对已保存的草稿/)).toBeNull());
  expect(saveManualDraftMock).toHaveBeenCalledTimes(1);
  expect(screen.getByPlaceholderText("sales-analysis")).toHaveProperty("value", "changed-name");
});

it("creates privately through the saved draft and hands off the exact receipt only on explicit Use", async () => {
  authMock.mockReturnValue({ me: { capabilities: { composer: { agent_start_use: true } } } });
  getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
  const reviewed = { ...SAVE_DRAFT, definition: { ...SAVE_DRAFT.definition, display_name: "Canonical Agent" }, actions: { ...SAVE_DRAFT.actions, create: true } };
  vi.mocked(validateManualSubagentDraft).mockResolvedValue(reviewed);
  let finish!: (value: { definition_id: string; replayed: boolean }) => void;
  vi.mocked(createSubagentFromManualDraft).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const close = vi.fn();
  renderIntl(<CreateSubagentCard unifiedDraftEnabled continuous presentation="canvas" manualDraft={SAVE_DRAFT} onClose={close} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "创建私有智能体" }).matches(":disabled")).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "创建私有智能体" }));
  await waitFor(() => expect(createSubagentFromManualDraft).toHaveBeenCalledOnce());
  expect(createSubagentFromManualDraft).toHaveBeenCalledWith(reviewed, []);
  expect(screen.getByRole("textbox", { name: "名称" }).matches(":disabled")).toBe(true);
  await expect(confirmCanvasNavigation()).resolves.toBe(false);
  expect(close).not.toHaveBeenCalled();
  expect(handoffMock).not.toHaveBeenCalled();
  await act(async () => finish({ definition_id: "osa_actual", replayed: false }));
  await screen.findByRole("button", { name: "开始使用" });
  expect(handoffMock).not.toHaveBeenCalled();
  // Creating released v1 for the creator only; the receipt says so and gives the two next steps.
  const receipt = screen.getByTestId("agent-creation-receipt");
  expect(within(receipt).getByText("已发布 v1 · 只有你能用")).toBeTruthy();
  const steps = within(receipt).getByRole("list", { name: "下一步" });
  expect(steps.textContent).toContain("先试一试：「开始使用」会开新对话并选中它。");
  expect(steps.textContent).toContain("团队可见可能需要管理员审批");
  fireEvent.click(within(steps).getByRole("button", { name: "「评测」" }));
  expect(pushMock).toHaveBeenLastCalledWith("/agents/manage/?agent_id=osa_actual&section=evaluate");
  fireEvent.click(within(steps).getByRole("button", { name: "「配置 › 谁能用」" }));
  expect(pushMock).toHaveBeenLastCalledWith("/agents/manage/?agent_id=osa_actual&section=config&config_section=who");
  expect(screen.queryByText("前往管理页可查看或修改已保存的配置。")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "开始使用" }));
  await waitFor(() => expect(handoffMock).toHaveBeenCalledWith({ agentId: "osa_actual", label: "Canonical Agent" }));
  expect(createSubagentMock).not.toHaveBeenCalled();
});

it("recovers an uncertain private creation from the original draft receipt without creating twice", async () => {
  getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
  const reviewed = { ...SAVE_DRAFT, actions: { ...SAVE_DRAFT.actions, create: true } };
  vi.mocked(validateManualSubagentDraft).mockResolvedValue(reviewed);
  vi.mocked(createSubagentFromManualDraft).mockRejectedValue(new Error("Response lost"));
  renderIntl(<CreateSubagentCard unifiedDraftEnabled continuous manualDraft={SAVE_DRAFT} onClose={vi.fn()} />);
  const create = screen.getByRole("button", { name: "创建私有智能体" });
  await waitFor(() => expect(create.matches(":disabled")).toBe(false));
  fireEvent.click(create);
  await screen.findByRole("button", { name: "核对已保存草稿" });
  getManualDraftMock.mockResolvedValueOnce(reviewed);
  fireEvent.click(screen.getByRole("button", { name: "核对已保存草稿" }));
  await waitFor(() => expect(getManualDraftMock).toHaveBeenCalledOnce());
  await act(async () => {});
  expect(create.matches(":disabled")).toBe(true);
  getManualDraftMock.mockResolvedValueOnce({ ...reviewed, draft_status: "consumed", result: { definition_id: "osa_recovered" }, actions: { edit: false, validate: false, create: false, discard: false } });
  fireEvent.click(screen.getByRole("button", { name: "核对已保存草稿" }));
  await screen.findByRole("button", { name: "管理智能体" });
  expect(createSubagentFromManualDraft).toHaveBeenCalledOnce();
  expect(createSubagentMock).not.toHaveBeenCalled();
});

it("preserves exact resource bindings during manual save and supports explicit removal", async () => {
  getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
  const grant = { skill_ref: { kind: "user" as const, id: "sk_personal", name: "evidence-method" }, revision: "sha256:original",
    permissions: ["read" as const], distribution: "reference" as const };
  const initial = { ...SAVE_DRAFT, definition: { ...SAVE_DRAFT.definition, skill_grants: [grant], capability_ids: ["tcap_saved"] } };
  saveManualDraftMock.mockImplementation(async (_draft, purpose, definition) => ({ ...initial, version: 2, purpose, definition }));
  renderIntl(<CreateSubagentCard unifiedDraftEnabled continuous manualDraft={initial} onClose={vi.fn()} />);
  fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "resource-reader" } });
  fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
  await waitFor(() => expect(saveManualDraftMock).toHaveBeenCalledWith(expect.objectContaining({ ...initial, requestContext: { teamId: expect.any(String), workspaceId: initial.workspace_id } }), initial.purpose,
    expect.objectContaining({ name: "resource-reader", skill_grants: [grant], capability_ids: ["tcap_saved"] }), false));
  const method = screen.getByText(/evidence-method/).parentElement!;
  fireEvent.click(within(method).getByRole("button", { name: "移除" }));
  fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
  await waitFor(() => expect(saveManualDraftMock).toHaveBeenLastCalledWith(expect.anything(), initial.purpose,
    expect.objectContaining({ skill_grants: [], capability_ids: ["tcap_saved"] }), false));
});

it("creates without a second confirmation once a Plugin that changes another system is selected", async () => {
  getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
  const initial = { ...SAVE_DRAFT, definition: { ...SAVE_DRAFT.definition, capability_ids: ["tcap_saved"] } };
  const reviewed = { ...initial, warnings: [{ code: "subagent_risk_ack:plugin", message: "Writes external data" }], actions: { ...initial.actions, create: true } };
  vi.mocked(validateManualSubagentDraft).mockResolvedValue(reviewed);
  vi.mocked(createSubagentFromManualDraft).mockResolvedValue({ definition_id: "osa_created", replayed: false });
  renderIntl(<CreateSubagentCard unifiedDraftEnabled continuous manualDraft={initial} onClose={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "创建私有智能体" }).matches(":disabled")).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "创建私有智能体" }));
  // Selecting is granting (beginner experience §7.4): the server warning is acknowledged with the selection.
  await waitFor(() => expect(createSubagentFromManualDraft).toHaveBeenCalledWith(reviewed, ["subagent_risk_ack:plugin"]));
  expect(screen.queryByRole("checkbox", { name: /可能修改外部系统/ })).toBeNull();
});


it("recovers consumed creation metadata from the authorized Agent reader after remount", async () => {
  const receiptLabel = vi.fn();
  const created = vi.fn();
  getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: false });
  getSubagentManagerMock.mockResolvedValue({ ...EXISTING, id: "osa_actual", name: "dogfood-ui-1004",
    display_name: "Dogfood UI 1004", tool_refs: ["python_sandbox"], tool_count: 1, max_iterations: 6 });
  const consumed = { ...SAVE_DRAFT, requestContext: { teamId: "team_original", workspaceId: "ws_a" }, draft_status: "consumed" as const, definition: {},
    result: { definition_id: "osa_actual", consumed_at: "2026-10-04T00:38:40Z" },
    actions: { edit: false, validate: false, create: false, discard: false } };
  renderIntl(<CreateSubagentCard unifiedDraftEnabled continuous manualDraft={consumed}
    onReceiptLabel={receiptLabel} onCreated={created} onClose={vi.fn()} />);
  await screen.findByRole("heading", { name: /Dogfood UI 1004/ });
  expect(screen.getByText("1 个工具")).toBeTruthy();
  expect(screen.queryByText("0 个工具")).toBeNull();
  expect(getSubagentManagerMock).toHaveBeenCalledWith("osa_actual", consumed.requestContext);
  expect(receiptLabel).toHaveBeenCalledWith("Dogfood UI 1004");
  expect(created).not.toHaveBeenCalled();
  expect(createSubagentFromManualDraft).not.toHaveBeenCalled();
});

it("counts Plugins and Tools by kind in the receipt reopened after a reload", async () => {
  getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: false });
  getAgentAuthoringCatalogMock.mockResolvedValue({
    plugins: {
      builtin: [{
        id: "builtin:code", name: "code", version: 1, family: "general",
        listing: { "zh-CN": { name: "代码计算", description: "运行代码完成计算" } }, tools: ["python_sandbox"], prompt_modules: [],
        ready: true, unavailable_tools: [], selectable: true, locked_reason: null, team_disabled: false, my_disabled: false,
      }],
      team: [],
    },
  });
  getSubagentManagerMock.mockResolvedValue({ ...EXISTING, id: "osa_plugin", name: "sales-summary",
    display_name: "销售汇总助手", tool_refs: [], tool_count: 0, max_iterations: 12 });
  const consumed = { ...SAVE_DRAFT, requestContext: { teamId: "team_original", workspaceId: "ws_a" }, draft_status: "consumed" as const,
    definition: { builtin_plugin_ids: ["builtin:code"] },
    result: { definition_id: "osa_plugin", consumed_at: "2026-10-20T00:00:00Z" },
    actions: { edit: false, validate: false, create: false, discard: false } };
  // The receipt reads the released definition, the same source live and after a reload.
  getAgentWorkingCopyMock.mockResolvedValue({ agent_id: "osa_plugin", live: { definition: { tool_refs: ["bi_query", "bi_schema"], builtin_plugin_ids: ["builtin:code", "builtin:email"] } } });
  renderIntl(<CreateSubagentCard unifiedDraftEnabled presentation="canvas" manualDraft={consumed} onClose={vi.fn()} />);
  await waitFor(() => expect(screen.getByTestId("agent-creation-receipt").textContent).toContain("销售汇总助手"));
  expect(await screen.findByText("2 个插件 · 2 个工具")).toBeTruthy();
  expect(getAgentWorkingCopyMock).toHaveBeenCalledWith("osa_plugin");
});

it("does not invent tool count or iteration defaults when consumed Agent access is denied", async () => {
  getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: false });
  getSubagentManagerMock.mockRejectedValue(new Error("Access denied"));
  const consumed = { ...SAVE_DRAFT, requestContext: { teamId: "team_original", workspaceId: "ws_a" }, definition: {}, result: { definition_id: "osa_original", consumed_at: "2026-10-04T00:38:40Z" },
    actions: { edit: false, validate: false, create: false, discard: false } };
  renderIntl(<CreateSubagentCard unifiedDraftEnabled continuous manualDraft={consumed} onClose={vi.fn()} />);
  await waitFor(() => expect(getSubagentManagerMock).toHaveBeenCalledWith("osa_original", consumed.requestContext));
  expect(screen.queryByText("0 个工具")).toBeNull();
  expect(screen.queryByText(/最多 12 轮/)).toBeNull();
  expect(createSubagentFromManualDraft).not.toHaveBeenCalled();
});

it("retries unreadable created configuration without repeating creation or inventing defaults", async () => {
  getToolsCatalogMock.mockResolvedValue({ team_tools: [], native_tools: [], text_only_allowed: false });
  const receiptLabel = vi.fn();
  const consumed = { ...SAVE_DRAFT, requestContext: { teamId: "team_original", workspaceId: "ws_a" }, definition: {},
    result: { definition_id: "osa_saved", consumed_at: "2026-10-03T18:00:00Z" },
    actions: { edit: false, validate: false, create: false, discard: false } };
  getSubagentManagerMock.mockRejectedValueOnce(new Error("Access denied")).mockResolvedValueOnce({
    ...EXISTING, id: "osa_saved", display_name: "Saved Analyst", description: "Summarize supplied evidence",
    tool_refs: ["bi_query", "bi_schema", "python_sandbox"], tool_count: 3, max_iterations: 6, model: "reasoner",
  });
  getAgentWorkingCopyMock.mockRejectedValueOnce(new Error("Access denied")).mockResolvedValue({
    agent_id: "osa_saved", live: { definition: { tool_refs: ["bi_query", "bi_schema", "python_sandbox"] } },
  });
  renderIntl(<CreateSubagentCard unifiedDraftEnabled presentation="canvas" manualDraft={consumed}
    onReceiptLabel={receiptLabel} onCreated={vi.fn()} onClose={vi.fn()} />);
  const retry = await screen.findByRole("button", { name: "重新读取配置" });
  expect(screen.getByTestId("agent-creation-receipt").textContent).toContain("已创建的智能体");
  // Creation succeeded, so the release line shows even while the saved configuration can't be read.
  expect(screen.getByText("已发布 v1 · 只有你能用")).toBeTruthy();
  expect(screen.getByRole("button", { name: "管理智能体" })).toBeTruthy();
  expect(screen.queryByText("0 个工具")).toBeNull();
  fireEvent.click(retry);
  await waitFor(() => expect(screen.getByTestId("agent-creation-receipt").textContent).toContain("Saved Analyst"));
  expect(await screen.findByText("3 个工具")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "重新读取配置" })).toBeNull();
  expect(getSubagentManagerMock).toHaveBeenLastCalledWith("osa_saved", consumed.requestContext);
  expect(receiptLabel).toHaveBeenLastCalledWith("Saved Analyst");
  expect(createSubagentFromManualDraft).not.toHaveBeenCalled();
  expect(saveManualDraftMock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "管理智能体" }));
  expect(pushMock).toHaveBeenLastCalledWith("/agents/manage/?agent_id=osa_saved");
});
