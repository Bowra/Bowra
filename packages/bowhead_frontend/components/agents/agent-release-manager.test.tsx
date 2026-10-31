import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect, useState } from "react";

import { renderIntl } from "@/__tests__/intl-render";
import {
  getAgentAuthoringCatalog,
  getAgentEvalManager,
  getAgentOptionalSkillCatalog,
  getAgentForkSettings,
  getAgentWorkingCopy,
  listAgentReleases,
  publishAgentWorkingCopy,
  restoreAgent,
  runAgentSafetyCheck,
  saveAgentWorkingCopy,
  setManagedAgentResourceStatus,
  updateAgentForkPolicy,
  type AgentAuthoringCatalog,
  type AgentExactSkillGrant,
  type AgentSafetyCheckReport,
  type AgentWorkingCopyDetail,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { agentManagementReturnHref } from "@/lib/agent-navigation";
import { listTeamPlugins } from "@/lib/team-plugins-api";
import { AgentReleaseManager, agentManagerLocation } from "./agent-release-manager";
import { handoffAgentToNewChat } from "@/lib/composer-handoff";

const versionWorkbenchMock = vi.hoisted(() => ({ availability: "disabled" as "enabled" | "disabled", resultAt: null as string | null }));
const personalMcpMock = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("./agent-eval-panel", () => ({ AgentEvalPanel: () => {
  const [draftCase, setDraftCase] = useState("");
  return <input aria-label="评测用例草稿" value={draftCase} onChange={(event) => setDraftCase(event.target.value)} />;
} }));
vi.mock("@/lib/feedback-candidates-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/feedback-candidates-api")>("@/lib/feedback-candidates-api");
  return { ...actual, redraftFeedbackCandidate: vi.fn() };
});
vi.mock("@/lib/composer-handoff", () => ({ handoffAgentToNewChat: vi.fn() }));
vi.mock("./agent-localization-panel", () => ({ AgentLocalizationPanel: () => null }));
vi.mock("./agent-taxonomy-panel", () => ({ AgentTaxonomyPanel: () => null }));
vi.mock("./agent-version-workbench", () => ({ AGENT_PENDING_ANCHOR: "agent-pending-changes", AgentVersionWorkbenchPanel: ({ onAvailability, onResultWaiting, view, agentName }: { onAvailability: (availability: "enabled" | "disabled") => void; onResultWaiting?: (resultAt: string | null) => void; view: string; agentName: string }) => {
  // Like the real workbench, availability is probed once when the panel mounts.
  const [availability] = useState(() => versionWorkbenchMock.availability);
  useEffect(() => onAvailability(availability), [availability, onAvailability]);
  useEffect(() => onResultWaiting?.(versionWorkbenchMock.resultAt), [onResultWaiting]);
  return <div data-testid="version-workbench-view" data-view={view} data-agent-name={agentName} />;
} }));

vi.mock("@/lib/content-share-api", () => ({
  fetchTeamDirectory: vi.fn().mockResolvedValue({ items: [] }),
  fetchTeamGroups: vi.fn().mockResolvedValue({ items: [] }),
}));

vi.mock("@/lib/agents-api", () => ({
  archiveAgent: vi.fn(),
  createAgentRollback: vi.fn(),
  getAgentAuthoringCatalog: vi.fn(),
  getAgentEvalManager: vi.fn(),
  getAgentKnowledgeSources: vi.fn().mockResolvedValue({ enabled: false, available: true, items: [], selected_items: [], total: 0, max_selected: 64 }),
  getAgentOptionalSkillCatalog: vi.fn().mockResolvedValue({ kind: "agent_optional_skill_catalog", schema_version: 1, agent_id: "osa_1", items: [], next_offset: null }),
  getAgentForkSettings: vi.fn(),
  getAgentWorkingCopy: vi.fn(),
  listAgentReleases: vi.fn(),
  publishAgentWorkingCopy: vi.fn(),
  restoreAgent: vi.fn(),
  runAgentSafetyCheck: vi.fn(),
  saveAgentWorkingCopy: vi.fn(),
  setManagedAgentResourceStatus: vi.fn(),
  updateAgentForkPolicy: vi.fn(),
}));

vi.mock("@/lib/team-plugins-api", () => ({
  listTeamPlugins: vi.fn(),
}));

vi.mock("@/lib/personal-mcp-api", () => ({ personalMcpApi: personalMcpMock.api }));

const spaceSelection = vi.hoisted(() => ({ activeId: "ws_1" as string | null }));
vi.mock("@/lib/use-space", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/use-space")>(),
  useSpaceSelection: () => ({ activeId: spaceSelection.activeId, items: [], loading: false, error: null, setActiveId: vi.fn(), refresh: vi.fn() }),
}));

vi.mock("@/lib/use-app-router", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/use-app-router")>(),
  useAppRouter: () => ({ push: vi.fn() }),
}));

const GRANT: AgentExactSkillGrant = {
  distribution: "reference",
  permissions: ["read"],
  revision: `sha256:${"1".repeat(64)}`,
  skill_ref: { id: "builtin:pptx", kind: "builtin" as const, name: "pptx" },
};

const CATALOG: AgentAuthoringCatalog = {
  kind: "agent_authoring_catalog",
  schema_version: 2,
  fingerprint: `sha256:${"2".repeat(64)}`,
  behavior_catalog_fingerprint: `sha256:${"4".repeat(64)}`,
  risk_registry_version: "risk-v1",
  model_policy: {
    allowed_roles: ["analyst"],
    effective_role: "analyst",
    locked: true,
    policy_version: "team-agent-model-policy-v1",
  },
  primary_capabilities: [{
    exact_ref: "cap_report@1",
    capability_id: "cap_report",
    version: 1,
    category: "artifact",
    display_name: "撰写报告",
    description: "形成结构清晰、可复核的报告。",
    display_name_en: "Write reports",
    description_en: "Produce structured, reviewable reports.",
    selectable: true,
    eligible_role_ids: ["knowledge"],
    verification_state: "reviewed",
    reason_codes: [],
    output_contract_refs: ["report.document.v1"],
  }],
  tools: [{
    stable_ref: "bi_query",
    display_name: "Query data",
    description: "Query data available to the current operator.",
    source_kind: "native",
    source_display_name: "Bowhead",
    capability_classes: ["data_query"],
    risk: "read",
    selectable: true,
    locked_reason: null,
    approval_required: false,
    confirmation_summary: null,
  }],
  skills: [{
    name: "pptx",
    display_name: "Presentations",
    description: "Create a presentation.",
    source_kind: "builtin",
    linked_tools: ["bi_query"],
    allowed_audiences: ["private", "custom", "teamwide"],
    grant: GRANT,
  }],
};

function detail(overrides: Partial<AgentWorkingCopyDetail> = {}): AgentWorkingCopyDetail {
  return {
    kind: "agent_working_copy_detail",
    schema_version: 1,
    agent_id: "osa_1",
    aggregate_row_version: 4,
    aggregate: { name: "finance-helper", status: "enabled", lifecycle_state: "active" },
    live: {
      release_id: "arl_1",
      release_sequence: 1,
      revision_id: "arv_1",
      revision_kind: "imported_baseline",
      definition: {
        system_prompt: "Analyze the monthly result.",
        max_iterations: 10,
        tool_refs_json: JSON.stringify(["bi_query"]),
        skills_json: JSON.stringify(["pptx"]),
      },
      listing: { display_name: "Finance Helper", description: "Explains monthly performance." },
      audience: { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] },
      activated_at: "2026-09-03T00:00:00Z",
    },
    working_copy: null,
    pending_candidate: null,
    actions: { edit: true, publish: false, enable: false, disable: true, archive: true, restore: false },
    ...overrides,
  };
}

const WORKING_COPY: NonNullable<AgentWorkingCopyDetail["working_copy"]> = {
  id: "awc_1",
  base_release_id: "arl_1",
  definition: {
    system_prompt: "Analyze the monthly result.",
    max_iterations: 10,
    tool_refs_json: JSON.stringify(["bi_query"]),
    skills_json: JSON.stringify(["pptx"]),
  },
  listing: { display_name: "Finance Helper", description: "Explains monthly performance." },
  audience: { schema_version: 1, effective_mode: "teamwide", user_ids: [], group_ids: [] },
  validation: {},
  change_kind: "authored",
  rollback_source_release_id: null,
  row_version: 2,
  updated_at: "2026-09-04T00:00:00Z",
};

const SAFETY_PASSED: AgentSafetyCheckReport = {
  kind: "agent_safety_check_report",
  schema_version: 1,
  id: "asc_1",
  agent_id: "osa_1",
  subject: { kind: "working_copy", ref: "awc_1", content_hash: `sha256:${"2".repeat(64)}`, working_copy_row_version: 2 },
  status: "pass",
  deterministic: { status: "pass", reason_codes: [] },
  semantic: { status: "pass", reason_codes: [] },
  summary: { status: "pass", reason_codes: [] },
  created_at: "2026-09-04T00:00:00Z",
  actions: { admin_override: false, publish: true },
};

describe("AgentReleaseManager", () => {
  // Configure is one page of three sections: "What it does" and "What it can use" start open,
  // "Who can use it" starts folded, and the resource catalog opens in a drawer.
  async function openConfigView(name: string) {
    await screen.findByRole("tablist");
    const who: Record<string, RegExp> = {
      "可见范围": /^谁能用、怎么上架/,
      "介绍与高级": /^谁能用、怎么上架/,
      "Listing details and advanced": /^Who can use it and how it's listed/,
    };
    if (!who[name]) return;
    const header = await screen.findByRole("button", { name: who[name] });
    if (header.getAttribute("aria-expanded") === "false") fireEvent.click(header);
  }

  async function openResourceCatalog() {
    fireEvent.click(await screen.findByRole("button", { name: "添加…" }));
    return screen.findByRole("dialog", { name: "从目录添加" });
  }

  function closeResourceCatalog() {
    fireEvent.click(within(screen.getByRole("dialog", { name: "从目录添加" })).getByRole("button", { name: "取消" }));
  }

  beforeEach(() => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1");
    versionWorkbenchMock.availability = "disabled";
    versionWorkbenchMock.resultAt = null;
    personalMcpMock.api.mockImplementation(async (path: string) => path === "/agents/bindings" ? { agents: [] } : { enabled: false });
    vi.mocked(getAgentOptionalSkillCatalog).mockResolvedValue({ kind: "agent_optional_skill_catalog", schema_version: 1, agent_id: "osa_1", items: [], next_offset: null });
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail());
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue(CATALOG);
    vi.mocked(listTeamPlugins).mockResolvedValue([]);
    vi.mocked(getAgentEvalManager).mockResolvedValue({} as never);
    vi.mocked(listAgentReleases).mockResolvedValue({
      kind: "agent_release_history",
      schema_version: 1,
      agent_id: "osa_1",
      items: [],
    });
    vi.mocked(getAgentForkSettings).mockResolvedValue({
      kind: "agent_fork_settings",
      schema_version: 1,
      agent_id: "osa_1",
      policy: { policy: "not_allowed", row_version: 0, updated_at: null },
      lineage: null,
    });
    vi.mocked(updateAgentForkPolicy).mockResolvedValue({
      agent_id: "osa_1",
      policy: "recipients",
      row_version: 1,
      updated_at: "2026-09-06T00:00:00Z",
    });
    vi.mocked(saveAgentWorkingCopy).mockResolvedValue({
      kind: "agent_working_copy",
      schema_version: 1,
      id: "awc_1",
      agent_id: "osa_1",
      base_release_id: "arl_1",
      definition: {},
      listing: {},
      audience: { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] },
      row_version: 1,
      updated_at: "2026-09-03T00:00:00Z",
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows the personal tool note beside shared audience choices only for a bound Agent", async () => {
    personalMcpMock.api.mockImplementation(async (path: string) => path === "/agents/bindings" ? {
      agents: [{ id: "osa_1", display_name: "Finance Helper", slug: "finance-helper", revision_id: "arv_1", tool_ids_json: '["pmt_1"]', binding_version: 1 }],
    } : { enabled: false });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    await openConfigView("可见范围");
    expect(screen.queryByText("已绑定个人工具")).toBeNull();
    expect(personalMcpMock.api).not.toHaveBeenCalledWith("/agents/bindings");

    fireEvent.click(screen.getByRole("radio", { name: "指定成员或群组" }));
    const note = (await screen.findByText("已绑定个人工具")).closest(".am-release-audience-personal-tools");
    expect(note?.getAttribute("role")).toBe("status");
    expect(note?.textContent).toContain("其他成员运行时不会获得它们");
    fireEvent.click(screen.getByRole("radio", { name: "全团队" }));
    expect(note?.textContent).toContain("只能使用自己有权限的其他工具");
    fireEvent.click(screen.getByRole("radio", { name: "仅自己" }));
    expect(screen.queryByText("已绑定个人工具")).toBeNull();
  });

  it("omits the personal tool note when this Agent has no binding", async () => {
    personalMcpMock.api.mockImplementation(async (path: string) => path === "/agents/bindings" ? {
      agents: [
        { id: "osa_1", display_name: "Finance Helper", slug: "finance-helper", revision_id: "arv_1", tool_ids_json: "[]", binding_version: 0 },
        { id: "osa_other", display_name: "Other", slug: "other", revision_id: "arv_2", tool_ids_json: '["pmt_2"]', binding_version: 1 },
      ],
    } : { enabled: false });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    await openConfigView("可见范围");
    fireEvent.click(screen.getByRole("radio", { name: "全团队" }));
    await waitFor(() => expect(personalMcpMock.api).toHaveBeenCalledWith("/agents/bindings"));
    expect(screen.queryByText("已绑定个人工具")).toBeNull();
  });

  it("hosts original configuration without changing the Chat URL and guards retained input", async () => {
    window.history.replaceState(null, "", "/?session=s_original&section=automation");
    const controllerRef = { current: null as { requestClose: () => void } | null };
    const onClose = vi.fn();
    const hosted = { controllerRef, onClose, onActivate: vi.fn(), onSaved: vi.fn(), readOnly: false };
    let setReadOnly!: (value: boolean) => void;
    function Host() { const [readOnly, change] = useState(false); setReadOnly = change;
      return <AgentReleaseManager agentId="osa_1" hosted={{ ...hosted, readOnly }} />; }
    renderIntl(<Host />);
    const prompt = await screen.findByDisplayValue("Analyze the monthly result.");
    fireEvent.change(prompt, { target: { value: "Retained original input" } });
    fireEvent.click(screen.getByRole("button", { name: /^谁能用、怎么上架/ }));
    expect(window.location.search).toBe("?session=s_original&section=automation");
    expect(screen.getByDisplayValue("Retained original input")).toBeTruthy();
    // The canvas has no Release switch: Coach's changes sit above the configuration, the release check below.
    expect(screen.getByTestId("version-workbench-view").getAttribute("data-view")).toBe("canvas");
    expect(screen.queryByRole("radio", { name: "应用" })).toBeNull();
    act(() => controllerRef.current?.requestClose());
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "继续编辑" }));
    expect(onClose).not.toHaveBeenCalled();
    act(() => setReadOnly(true));
    expect(screen.getByDisplayValue("Retained original input").matches(":disabled")).toBe(true);
  });

  it("lets Chat save or discard the hosted editor's input through its port", async () => {
    const controllerRef = { current: null as import("@/lib/coach-canvas-target").CoachEditorPort | null };
    vi.mocked(saveAgentWorkingCopy).mockResolvedValue({} as never);
    renderIntl(<AgentReleaseManager agentId="osa_1" hosted={{ controllerRef, onClose: vi.fn(), onActivate: vi.fn(), onSaved: vi.fn(), readOnly: false }} />);
    fireEvent.change(await screen.findByDisplayValue("Analyze the monthly result."), { target: { value: "Edited in canvas." } });
    expect(controllerRef.current?.capture?.()?.dirty).toBe(true);
    let saved: boolean | undefined;
    await act(async () => { saved = await controllerRef.current!.save!(); });
    expect(saved).toBe(true);
    expect(saveAgentWorkingCopy).toHaveBeenCalledWith("osa_1", expect.objectContaining({
      definition: expect.objectContaining({ system_prompt: "Edited in canvas." }),
    }));

    // The mocked server answers with its original draft after the save, so edit whatever it shows now.
    fireEvent.change(screen.getByRole("textbox", { name: "智能体指令" }), { target: { value: "Throw this away." } });
    let discarded: Promise<boolean> | undefined;
    act(() => { discarded = controllerRef.current!.discard!(); });
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "放弃修改" }));
    expect(await discarded).toBe(true);
    await waitFor(() => expect(screen.queryByDisplayValue("Throw this away.")).toBeNull());
  });

  it("preserves input and the original version after a hosted save conflict", async () => {
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail({ working_copy: WORKING_COPY }));
    vi.mocked(saveAgentWorkingCopy).mockRejectedValueOnce(new ApiError(409, "Original baseline changed"));
    const onSaved = vi.fn();
    renderIntl(<AgentReleaseManager agentId="osa_1" hosted={{ readOnly: false, controllerRef: { current: null },
      onActivate: vi.fn(), onClose: vi.fn(), onSaved }} />);
    fireEvent.change(await screen.findByDisplayValue("Analyze the monthly result."), { target: { value: "Keep my focused edit" } });
    // Hosted in Canvas the working copy saves through the shared save bar (D10).
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("无法确认草稿是否已保存，请核对当前版本后再试。");
    expect(screen.queryByText("Original baseline changed")).toBeNull();
    expect(screen.getByDisplayValue("Keep my focused edit")).toBeTruthy();
    expect(vi.mocked(saveAgentWorkingCopy).mock.calls[0][1].expected_row_version).toBe(WORKING_COPY.row_version);
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("blocks close during the original save and ignores its result after disposal", async () => {
    let finish!: () => void;
    vi.mocked(saveAgentWorkingCopy).mockImplementationOnce(() => new Promise((resolve) => { finish = () => resolve({} as never); }));
    const controllerRef = { current: null as { requestClose: () => void } | null };
    const onClose = vi.fn(); const onSaved = vi.fn();
    const view = renderIntl(<AgentReleaseManager agentId="osa_1" hosted={{ readOnly: false, controllerRef,
      onActivate: vi.fn(), onClose, onSaved }} />);
    const prompt = await screen.findByDisplayValue("Analyze the monthly result.");
    fireEvent.change(prompt, { target: { value: "Pending write" } });
    // Hosted in Canvas the working copy saves through the shared save bar (D10).
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledTimes(1));
    expect(prompt.matches(":disabled")).toBe(true);
    act(() => controllerRef.current?.requestClose());
    expect(onClose).not.toHaveBeenCalled();
    view.unmount();
    await act(async () => finish());
    expect(onSaved).not.toHaveBeenCalled();
    expect(getAgentWorkingCopy).toHaveBeenCalledTimes(1);
  });

  it("creates the first working copy without dropping legacy Tool and Skill selections", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    const save = await screen.findByRole("button", { name: "保存草稿" });
    expect(screen.getByText("所需 Tool：Query data")).toBeTruthy();
    expect(save.hasAttribute("disabled")).toBe(false);
    fireEvent.click(save);

    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledWith(
      "osa_1",
      expect.objectContaining({
        definition: expect.objectContaining({
          tool_refs: ["bi_query"],
          skill_grants: [GRANT],
        }),
        expected_row_version: null,
      }),
    ));
  });

  it("saves a selected team Plugin by stable ID", async () => {
    vi.mocked(listTeamPlugins).mockResolvedValue([{
      id: "tcap_research",
      team_id: "team_1",
      name: "Research",
      description: "Research resources",
      status: "active",
      revision: 1,
      row_version: 1,
      content_hash: `sha256:${"a".repeat(64)}`,
      tool_refs: ["bi_query"],
      skill_grants: [],
      agent_count: 0,
    }]);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    const capabilities = await screen.findByRole("region", { name: "插件" });
    expect(within(capabilities).getByText("选择插件后，智能体会在下一次执行时使用管理员发布的最新工具和技能组合。")).toBeTruthy();
    fireEvent.click(within(capabilities).getByRole("checkbox", { name: /Research/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledWith(
      "osa_1",
      expect.objectContaining({
        definition: expect.objectContaining({ capability_ids: ["tcap_research"] }),
      }),
    ));
  });

  it("saves the built-in tool guidance toggle into the definition only when it is on", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    const toggle = await screen.findByRole("checkbox", { name: /内置工具使用规范/ });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText(/发布版本后生效/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledTimes(1));
    expect(vi.mocked(saveAgentWorkingCopy).mock.calls[0][1].definition).not.toHaveProperty("capability_protocol_block");

    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledTimes(2));
    expect(vi.mocked(saveAgentWorkingCopy).mock.calls[1][1].definition).toMatchObject({ capability_protocol_block: true });
  });

  it("loads an enabled built-in tool guidance toggle from the live definition and can turn it off", async () => {
    const original = detail();
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail({
      live: { ...original.live, definition: { ...original.live.definition, capability_protocol_block: true } },
    }));
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    const toggle = await screen.findByRole("checkbox", { name: /内置工具使用规范/ });
    expect((toggle as HTMLInputElement).checked).toBe(true);

    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledTimes(1));
    expect(vi.mocked(saveAgentWorkingCopy).mock.calls[0][1].definition).not.toHaveProperty("capability_protocol_block");
  });

  it("does not restore a deleted Plugin from a historical live definition", async () => {
    const original = detail();
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail({
      live: { ...original.live, definition: { ...original.live.definition, capability_ids: ["tcap_deleted"] } },
    }));
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    await screen.findByRole("textbox", { name: "智能体指令" });
    expect(screen.queryByText("tcap_deleted")).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "智能体指令" }), { target: { value: "Use current data." } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledWith(
      "osa_1", expect.objectContaining({ definition: expect.objectContaining({ capability_ids: [] }) }),
    ));
  });

  it("distinguishes a new Agent's missing working copy from browser-only edits", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    expect(await screen.findByText("你在看已发布的 v1。保存后会生成草稿；发布前，大家用的仍是 v1。")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "发布" }));
    expect(screen.getByText("还没有可以发布的草稿。请先到配置页保存草稿，再准备候选。")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "配置" }));
    fireEvent.change(screen.getByRole("textbox", { name: "智能体指令" }), {
      target: { value: "Use only the user's current input." },
    });
    expect(screen.getByText("修改还没保存。保存后进入草稿，v1 不变。")).toBeTruthy();
  });

  it("heads an archived Agent as read-only history instead of the draft editor", async () => {
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail({
      aggregate: { name: "finance-helper", status: "disabled", lifecycle_state: "archived" },
      actions: { edit: false, publish: false, enable: false, disable: false, archive: false, restore: true },
    }));
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    expect(await screen.findByRole("heading", { name: "智能体配置" })).toBeTruthy();
    expect(screen.getByText("已归档 · 只读")).toBeTruthy();
    expect(screen.getByText("最后发布 v1 · 已归档")).toBeTruthy();
    expect(screen.queryByText(/^已发布 v1 · /)).toBeNull();
    expect(screen.queryByRole("heading", { name: "编辑智能体配置" })).toBeNull();
    expect(screen.queryByText("草稿", { selector: ".am-release-task-heading span" })).toBeNull();
  });

  it("keeps a private Agent's Configure in plain words and folds the main responsibility under Advanced", async () => {
    const view = renderIntl(<AgentReleaseManager agentId="osa_1" />);
    expect(await screen.findByRole("heading", { name: "工作说明" })).toBeTruthy();
    expect(screen.getByText("写清它负责什么、什么算做完、哪些事不能做。")).toBeTruthy();
    const advanced = view.container.querySelector("details.am-config-advanced") as HTMLDetailsElement;
    expect(advanced.open).toBe(false);
    expect(advanced.querySelector("summary")!.textContent).toBe("高级可选");
    // Nothing a private Owner needs at first: no Outcome, Lead, result contract, Live or working copy outside Advanced.
    const visible = [...view.container.querySelectorAll(".am-release-layout *")]
      .filter((element) => !element.closest("details.am-config-advanced:not([open]) > :not(summary)") && element.children.length === 0)
      .map((element) => element.textContent ?? "").join(" ");
    expect(visible).not.toMatch(/Outcome|Lead|结果契约|Live|工作副本|审批人/);
    const summary = view.container.querySelector(".am-config-summary")!;
    expect([...summary.querySelectorAll("dt")].map((term) => term.textContent)).toEqual(["提示词", "能用什么", "谁能用"]);
    expect(within(advanced).getByText("向团队承诺它能稳定完成的一类工作。私有智能体一般不需要。只有已验证的职责可以选。")).toBeTruthy();
  });

  it("opens Advanced for a shared Agent with a main responsibility and starts with what reviewers see", async () => {
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail({
      working_copy: { ...WORKING_COPY, definition: { ...WORKING_COPY.definition, primary_capability_ref: "cap_report@1" } },
    }));
    const view = renderIntl(<AgentReleaseManager agentId="osa_1" />, { locale: "en-US" });
    await screen.findByRole("heading", { name: "Instructions" });
    const advanced = view.container.querySelector("details.am-config-advanced") as HTMLDetailsElement;
    await waitFor(() => expect(advanced.open).toBe(true));
    expect(advanced.querySelector("summary")!.textContent).toBe("AdvancedMain responsibility: Write reports");
    const note = within(advanced).getByRole("note");
    expect(note.textContent).toBe("After you submit, reviewers see the candidate's safety-related changes; they never see your unsubmitted draft.");
    expect(within(advanced).getByText("Lead (the main agent) hands this kind of work to it. Only verified responsibilities can be chosen.")).toBeTruthy();
    expect([...view.container.querySelectorAll(".am-config-summary dt")].map((term) => term.textContent)).toEqual(["Prompt", "Main responsibility", "What it can use", "Who can use it"]);
  });

  it("shows one task at a time, with evaluation suites as their own tab", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    const release = await screen.findByRole("tab", { name: "发布" });
    expect(screen.getByText(/^已发布 v1 · /)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "智能体介绍" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "一句话介绍" })).toBeTruthy();
    fireEvent.click(release);
    expect(release.getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByRole("textbox", { name: "一句话介绍" })).toBeNull();
    // Version history lives under the release card instead of its own tab.
    expect(screen.getByRole("heading", { name: "版本历史" })).toBeTruthy();
    fireEvent.click(await screen.findByRole("tab", { name: "评测" }));
    expect(new URLSearchParams(window.location.search).get("section")).toBe("evaluate");
  });

  it("marks Release while a result waits, until this viewer opens it", async () => {
    const stored = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => void stored.set(key, value) });
    versionWorkbenchMock.resultAt = "2026-10-18T08:00:00+00:00";
    const first = renderIntl(<AgentReleaseManager agentId="osa_1" />);
    const release = await screen.findByRole("tab", { name: /^发布/ });
    expect(within(release).getByRole("img", { name: "有结果待处理" })).toBeTruthy();
    fireEvent.click(release);
    expect(within(release).queryByRole("img", { name: "有结果待处理" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: /^配置/ }));
    expect(within(screen.getByRole("tab", { name: /^发布/ })).queryByRole("img")).toBeNull();
    first.unmount();
    // Seen is remembered per viewer; a newer result marks the tab again.
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    expect(within(await screen.findByRole("tab", { name: /^发布/ })).queryByRole("img")).toBeNull();
    cleanup();
    versionWorkbenchMock.resultAt = "2026-10-18T09:00:00+00:00";
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    expect(within(await screen.findByRole("tab", { name: /^发布/ })).getByRole("img", { name: "有结果待处理" })).toBeTruthy();
    vi.unstubAllGlobals();
  });

  it("keeps the Agent and the management return in the URL when switching tabs", async () => {
    const returnHref = "/agents/manage/governance/";
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&return_view=team");
    renderIntl(<AgentReleaseManager agentId="osa_1" returnHref={returnHref} />);
    fireEvent.click(await screen.findByRole("tab", { name: "发布" }));
    const params = new URLSearchParams(window.location.search);
    expect(params.get("section")).toBe("release");
    expect(params.get("agent_id")).toBe("osa_1");
    expect(agentManagementReturnHref(window.location.search)).toBe(returnHref);
  });

  it("shows Configure as three sections, with who can use it folded until opened", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    expect(await screen.findByRole("textbox", { name: "智能体指令" })).toBeTruthy();
    expect(screen.getByRole("radio", { name: /撰写报告/ })).toBeTruthy();
    expect(screen.queryByRole("searchbox", { name: "搜索 Skill" })).toBeNull();
    const who = screen.getByRole("button", { name: /^谁能用、怎么上架/ });
    expect(who.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(who);
    expect(who.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("radio", { name: "仅自己" })).toBeTruthy();
  });

  it("searches and adds a Skill from an explicit resource catalog", async () => {
    const toolsOnly = detail();
    toolsOnly.live.definition.skills_json = JSON.stringify([]);
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(toolsOnly);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    await openResourceCatalog();
    // One search over Plugins, Tools and Skills; no kind switch.
    expect(screen.queryByRole("radio", { name: "添加 Skill" })).toBeNull();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索插件、工具、技能" }), { target: { value: "presentation" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Presentations/ }));
    closeResourceCatalog();

    expect(screen.getByText("当前已添加 0 个插件、1 个技能、0 个团队工具连接和 1 个内置工具（含插件带来的）。连接内的具体能力会在保存时固化。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "保存草稿" }).hasAttribute("disabled")).toBe(false);
  });

  it("counts what a selected Plugin brings in every resource summary instead of saying nothing was added", async () => {
    const pluginOnly = detail();
    pluginOnly.live.definition.tool_refs_json = JSON.stringify([]);
    pluginOnly.live.definition.skills_json = JSON.stringify([]);
    pluginOnly.live.definition.builtin_plugin_ids = JSON.stringify(["builtin:code"]);
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(pluginOnly);
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({
      ...CATALOG,
      plugins: {
        builtin: [{
          id: "builtin:code",
          name: "code",
          version: 1,
          tools: ["python_sandbox", "bi_query"],
          ready: true,
          unavailable_tools: [],
          selectable: true,
          locked_reason: null,
          team_disabled: false,
          my_disabled: false,
          family: "general",
          listing: { "zh-CN": { name: "代码计算", description: "运行代码完成计算" } },
          prompt_modules: [],
        }],
        team: [],
      },
    });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    expect(await screen.findByText("当前已添加 1 个插件、0 个技能、0 个团队工具连接和 2 个内置工具（含插件带来的）。连接内的具体能力会在保存时固化。")).toBeTruthy();
    expect(screen.getByText("这个智能体能用的资源都来自上方已选的插件：代码计算。在上方取消勾选插件即可移除它带来的资源。")).toBeTruthy();
    expect(screen.queryByText("还没有添加执行资源")).toBeNull();
    // The section summary and the save card state the same counts, with the creation receipt's rule: the Plugin
    // counts once, not as its two Tools, and only the kinds present are named (no "0 Skills / 2 Tools").
    expect(screen.getAllByText("1 个插件")).toHaveLength(2);
  });

  it("finds Plugins in the one catalog search and selects Email with its consequence line", async () => {
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({
      ...CATALOG,
      plugins: {
        builtin: [{
          id: "builtin:email",
          name: "email",
          version: 1,
          tools: ["send_email"],
          ready: true,
          unavailable_tools: [],
          selectable: true,
          locked_reason: null,
          team_disabled: false,
          my_disabled: false,
          family: "general",
          listing: { "zh-CN": { name: "邮件", description: "发邮件", use_when: "要把结果发邮件给别人时", example: "「发给 sales@example.com」" } },
          prompt_modules: [],
        }],
        team: [],
      },
    });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    const dialog = await openResourceCatalog();
    fireEvent.change(within(dialog).getByRole("searchbox", { name: "搜索插件、工具、技能" }), { target: { value: "邮件" } });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /邮件/ }));
    closeResourceCatalog();
    expect(screen.getByText("选上后，这个智能体会直接发邮件，不再每次问你。")).toBeTruthy();

    const again = await openResourceCatalog();
    fireEvent.change(within(again).getByRole("searchbox", { name: "搜索插件、工具、技能" }), { target: { value: "发短信" } });
    expect(within(again).getByText("没有匹配「发短信」的插件、工具或技能")).toBeTruthy();
  });

  it("shows Skill dependencies and adds their exact grants together", async () => {
    const framework = {
      ...CATALOG.skills[0],
      name: "analyst-framework",
      display_name: "分析师框架",
      description: "通用分析骨架",
      linked_tools: [],
      required_skills: [],
      grant: { ...GRANT, skill_ref: { ...GRANT.skill_ref, id: "builtin:analyst-framework", name: "analyst-framework" } },
    };
    const method = {
      ...CATALOG.skills[0],
      name: "analyst-method-roe-dupont",
      display_name: "ROE 杜邦归因",
      description: "拆解 ROE 的经营与杠杆来源",
      linked_tools: [],
      required_skills: ["analyst-framework"],
      grant: { ...GRANT, skill_ref: { ...GRANT.skill_ref, id: "builtin:analyst-method-roe-dupont", name: "analyst-method-roe-dupont" } },
    };
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({ ...CATALOG, skills: [framework, method] });
    const noSkills = detail();
    noSkills.live.definition.skills_json = JSON.stringify([]);
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(noSkills);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    await openResourceCatalog();
    expect(await screen.findByText("会同时添加：分析师框架")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: /ROE 杜邦归因/ }));
    closeResourceCatalog();

    expect(screen.getByText("当前已添加 0 个插件、2 个技能、0 个团队工具连接和 1 个内置工具（含插件带来的）。连接内的具体能力会在保存时固化。")).toBeTruthy();
    const frameworkCheckbox = screen.getAllByRole("checkbox").find((element) => (
      element.closest("label")?.querySelector("strong")?.textContent === "分析师框架"
    )) as HTMLInputElement;
    expect(frameworkCheckbox.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalled());
    const savedGrants = vi.mocked(saveAgentWorkingCopy).mock.calls[0][1].definition.skill_grants as AgentExactSkillGrant[];
    expect(savedGrants.map((grant) => grant.skill_ref.name)).toEqual([
      "analyst-framework",
      "analyst-method-roe-dupont",
    ]);
  });

  it("selects a team Tool connection once and pins all of its current Tools on save", async () => {
    const datarcTools = [
      ["datarc_workspaces", "查看工作区"],
      ["datarc_schema", "查看数据结构"],
      ["datarc_query", "查询数据"],
    ].map(([operation, displayName]) => ({
      ...CATALOG.tools[0],
      stable_ref: `teammcp.ibt_full.${operation}`,
      display_name: displayName,
      description: `${displayName}说明`,
      source_kind: "team_mcp" as const,
      source_display_name: "北极九章 - 全功能",
      connection_id: "ibt_full",
    }));
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({ ...CATALOG, tools: [...CATALOG.tools, ...datarcTools] });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    await openResourceCatalog();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索插件、工具、技能" }), { target: { value: "全功能" } });
    const connectionCheckbox = screen.getByRole("checkbox", { name: "北极九章 - 全功能" });
    expect(screen.getAllByRole("checkbox", { name: "北极九章 - 全功能" })).toHaveLength(1);
    expect(connectionCheckbox.closest("article")?.querySelector('[data-source-icon="datarc"]')).toBeTruthy();
    fireEvent.click(screen.getByText("查看包含的能力"));
    expect(screen.getByText("查询数据")).toBeTruthy();
    expect(screen.getByRole("link", { name: "工具" }).getAttribute("href")).toBe("/capabilities/?type=tools");
    fireEvent.click(connectionCheckbox);
    closeResourceCatalog();
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalled());
    const definition = vi.mocked(saveAgentWorkingCopy).mock.calls[0][1].definition;
    expect(definition.team_tool_connections).toEqual(["ibt_full"]);
    expect(definition.tool_refs).toEqual([
      "bi_query",
      "teammcp.ibt_full.datarc_query",
      "teammcp.ibt_full.datarc_schema",
      "teammcp.ibt_full.datarc_workspaces",
    ]);
  });

  it("bounds a large Skill catalog and expands it on demand", async () => {
    const manySkills = Array.from({ length: 10 }, (_, index) => ({
      ...CATALOG.skills[0],
      name: `method-${index}`,
      display_name: `Method ${index}`,
      grant: {
        ...GRANT,
        skill_ref: { ...GRANT.skill_ref, id: `builtin:method-${index}`, name: `method-${index}` },
      },
    }));
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({ ...CATALOG, skills: manySkills });
    const toolsOnly = detail();
    toolsOnly.live.definition.skills_json = JSON.stringify([]);
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(toolsOnly);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    await openResourceCatalog();
    expect(document.querySelectorAll('input[name="agent-skill"]')).toHaveLength(8);
    fireEvent.click(screen.getByRole("button", { name: "显示更多（剩余 2 项）" }));
    expect(document.querySelectorAll('input[name="agent-skill"]')).toHaveLength(10);
  });

  it("redirects an old configuration link to its section and keeps unsaved input across tabs", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&section=config&config_view=access");
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    const prompt = await screen.findByRole("textbox", { name: "智能体指令" });
    await waitFor(() => expect(window.location.search).toBe("?agent_id=osa_1&section=config&config_section=who"));
    expect(screen.getByRole("button", { name: /^谁能用、怎么上架/ }).getAttribute("aria-expanded")).toBe("true");
    fireEvent.change(prompt, { target: { value: "Keep this unsaved prompt." } });
    fireEvent.click(screen.getByRole("tab", { name: "发布" }));
    fireEvent.click(screen.getByRole("tab", { name: "配置" }));
    expect((screen.getByRole("textbox", { name: "智能体指令" }) as HTMLTextAreaElement).value).toBe("Keep this unsaved prompt.");
  });

  it("keeps a section the Owner folded after the link opened it, while availability settles", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&section=config&config_section=who");
    versionWorkbenchMock.availability = "enabled";
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    const who = await screen.findByRole("button", { name: /^谁能用、怎么上架/ });
    await waitFor(() => expect(who.getAttribute("aria-expanded")).toBe("true"));
    fireEvent.click(who);
    // Evaluation availability settles after this, re-running the navigation sync.
    expect(await screen.findByRole("tab", { name: "评测" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^谁能用、怎么上架/ }).getAttribute("aria-expanded")).toBe("false");
  });

  it("opens Chat with @Coach and the request staged in the composer from Ask Coach to change this", async () => {
    versionWorkbenchMock.availability = "enabled";
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "让 Coach 帮我改" }));
    await waitFor(() => expect(handoffAgentToNewChat).toHaveBeenCalledWith({
      agentId: "system:agent-coach",
      label: "Coach",
      text: "帮我修改智能体「Finance Helper」：",
    }));
  });

  it("asks before leaving unsaved configuration for Coach", async () => {
    versionWorkbenchMock.availability = "enabled";
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    fireEvent.change(await screen.findByRole("textbox", { name: "智能体指令" }), { target: { value: "Unsaved." } });
    fireEvent.click(screen.getByRole("button", { name: "让 Coach 帮我改" }));
    const dialog = await screen.findByRole("dialog");
    expect(handoffAgentToNewChat).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "继续编辑" }));
    expect(handoffAgentToNewChat).not.toHaveBeenCalled();
  });

  it("drops an unknown configuration deep link", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&section=config&config_view=unknown");
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    expect(await screen.findByRole("textbox", { name: "智能体指令" })).toBeTruthy();
    await waitFor(() => expect(window.location.search).toBe("?agent_id=osa_1&section=config"));
  });

  it("sends an old evaluation-suites link to Release when evaluation is unavailable", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&section=release&release_view=tests");
    vi.mocked(getAgentEvalManager).mockRejectedValue(new ApiError(404, "Not found"));

    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    await waitFor(() => expect(screen.getByTestId("version-workbench-view").getAttribute("data-view")).toBe("validation"));
    expect(screen.queryByRole("tab", { name: "评测" })).toBeNull();
    await waitFor(() => expect(window.location.search).toBe("?agent_id=osa_1&section=release"));
  });

  it("keeps an in-progress eval case while the owner switches tabs", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    fireEvent.click(await screen.findByRole("tab", { name: "评测" }));
    const evalDraft = await screen.findByRole("textbox", { name: "评测用例草稿" });
    fireEvent.change(evalDraft, { target: { value: "回滚后仍应保持的评测草稿" } });
    fireEvent.click(screen.getByRole("tab", { name: "发布" }));
    expect(screen.queryByRole("textbox", { name: "评测用例草稿" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "评测" }));
    expect((screen.getByRole("textbox", { name: "评测用例草稿" }) as HTMLInputElement).value).toBe("回滚后仍应保持的评测草稿");
  });

  it("restores task and nested release state from browser history without dropping a config draft", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    const description = await screen.findByRole("textbox", { name: "一句话介绍" });
    fireEvent.change(description, { target: { value: "尚未保存的配置草稿" } });
    window.history.pushState(null, "", "/agents/manage/?agent_id=osa_1&section=history");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(await screen.findByRole("heading", { name: "版本历史" })).toBeTruthy();

    window.history.pushState(null, "", "/agents/manage/?agent_id=osa_1&section=release&release_view=tests");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(await screen.findByRole("textbox", { name: "评测用例草稿" })).toBeTruthy();

    window.history.pushState(null, "", "/agents/manage/?agent_id=osa_1&section=config");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect((await screen.findByRole("textbox", { name: "一句话介绍" }) as HTMLTextAreaElement).value).toBe("尚未保存的配置草稿");
  });

  it("opens a version task directly from its stable URL", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&section=history");
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    expect(await screen.findByRole("heading", { name: "版本历史" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "发布" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByRole("textbox", { name: "一句话介绍" })).toBeNull();
  });

  it("shows the three tabs and Configure's pending changes when version governance is available", async () => {
    versionWorkbenchMock.availability = "enabled";
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    expect(await screen.findByRole("tab", { name: "评测" })).toBeTruthy();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["配置", "评测", "发布"]);
    expect(screen.getByTestId("version-workbench-view").getAttribute("data-view")).toBe("proposals");
    // The workbench gets the name the Owner sees in the page title, not the internal aggregate name
    // (116 showed "gina" under a page titled 数据查询师).
    expect(screen.getByTestId("version-workbench-view").getAttribute("data-agent-name")).toBe("Finance Helper");
  });

  it("opens Configure from an old Automation link", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&section=automation");
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    expect(await screen.findByRole("textbox", { name: "一句话介绍" })).toBeTruthy();
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("section")).toBe("config"));
  });

  it.each(["live", "working_copy"] as const)("preserves optional policy from %s while saving unrelated text", async (source) => {
    const existing = detail({ working_copy: source === "working_copy" ? structuredClone(WORKING_COPY) : null });
    const policy = { schema_version: 1, skill_read: { enabled: true, selection_sources: ["user_explicit"],
      allowed_sources: ["builtin"], resource_allowlist: [{ source_kind: "builtin", id: "builtin:dupont" }],
      require_read_only_contract: true, max_selected: 2 } };
    existing[source]!.definition.runtime_extensions = policy;
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(existing);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    fireEvent.change(await screen.findByRole("textbox", { name: "一句话介绍" }), { target: { value: "Updated summary." } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalled());
    const payload = vi.mocked(saveAgentWorkingCopy).mock.calls[0][1];
    expect(payload.definition.runtime_extensions).toEqual(policy);
    expect(payload.definition.skill_grants).toEqual([GRANT]);
    expect(payload.definition.tool_refs).toEqual(["bi_query"]);
  });

  it.each(["live", "working_copy"] as const)("preserves selected knowledge from %s during unrelated edits", async (source) => {
    const policy = { schema_version: 1, mode: "selected_files", file_ids: [], defaults: { top_k: 5 } };
    const existing = detail(source === "working_copy" ? { working_copy: structuredClone(WORKING_COPY) } : {});
    existing[source]!.definition.knowledge_access = policy;
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(existing);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    fireEvent.change(await screen.findByRole("textbox", { name: "一句话介绍" }), { target: { value: "Updated summary." } });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalled());
    expect(vi.mocked(saveAgentWorkingCopy).mock.calls[0][1].definition.knowledge_access).toEqual(policy);
  });

  it("saves Owner-selected optional methods without changing baseline grants or tools, and blocks a shared personal method", async () => {
    const resource = { kind: "skill", source_kind: "user", id: "skill_personal", version: `sha256:${"a".repeat(64)}` };
    vi.mocked(getAgentOptionalSkillCatalog).mockResolvedValue({ kind: "agent_optional_skill_catalog", schema_version: 1, agent_id: "osa_1",
      items: [{ resource, label: "杜邦分析", description: "只读盈利分析", allowed_audiences: ["private"] }], next_offset: null });
    const savedWorkingCopy = structuredClone(WORKING_COPY);
    savedWorkingCopy.audience = { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] };
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail({ working_copy: savedWorkingCopy }));
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    await openConfigView("可见范围");
    fireEvent.click(await screen.findByRole("button", { name: "配置可选方法" }));
    const method = await screen.findByRole("checkbox", { name: /杜邦分析/ });
    expect((screen.getByRole("button", { name: "保存草稿" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(method);
    expect((screen.getByRole("button", { name: "保存草稿" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "清除可选方法策略" }));
    expect((screen.getByRole("button", { name: "保存草稿" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "配置可选方法" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /杜邦分析/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: "允许用户为单个任务添加方法" }));
    fireEvent.click(screen.getByRole("radio", { name: "全团队" }));
    expect((screen.getByRole("button", { name: "保存草稿" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(saveAgentWorkingCopy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("radio", { name: "仅自己" }));
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(saveAgentWorkingCopy).mock.calls[0][1];
    expect(payload.definition.runtime_extensions).toMatchObject({ schema_version: 1, skill_read: { enabled: true,
      resource_allowlist: [{ source_kind: "user", id: "skill_personal", version: resource.version }] } });
    expect(payload.definition.skill_grants).toEqual([GRANT]);
    expect(payload.definition.tool_refs).toEqual(["bi_query"]);
    expect(publishAgentWorkingCopy).not.toHaveBeenCalled();
  });

  it.each([false, true])("preserves missing versus explicitly cleared optional scope (%s)", async (cleared) => {
    const existing = detail();
    if (cleared) existing.live.definition.runtime_extensions = null;
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(existing);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalled());
    const definition = vi.mocked(saveAgentWorkingCopy).mock.calls[0][1].definition;
    expect(Object.hasOwn(definition, "runtime_extensions")).toBe(cleared);
    if (cleared) expect(definition.runtime_extensions).toBeNull();
  });

  it("keeps approval-locked selections editable for removal, without allowing re-addition", async () => {
    const existing = detail();
    existing.live!.definition.tool_refs_json = JSON.stringify(["bi_query", "teammcp.crm.update"]);
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(existing);
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({ ...CATALOG, tools: [...CATALOG.tools, {
      ...CATALOG.tools[0], stable_ref: "teammcp.crm.update", display_name: "Update CRM", source_kind: "team_mcp", risk: "external_write",
      source_display_name: "CRM", connection_id: "crm", selectable: false, locked_reason: "approval_executor_unavailable", approval_required: true,
    }] });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    const checkbox = await screen.findByRole("checkbox", { name: "CRM" }) as HTMLInputElement;
    const card = checkbox.closest("article")!;
    expect(card.textContent).toContain("当前部署无法执行此工具所需的审批流程");
    expect(checkbox.disabled).toBe(false);
    expect(checkbox.checked).toBe(true);
    fireEvent.click(checkbox);
    await openResourceCatalog();
    const unavailableCheckbox = screen.getByRole("checkbox", { name: "CRM" }) as HTMLInputElement;
    expect(unavailableCheckbox.checked).toBe(false);
    expect(unavailableCheckbox.disabled).toBe(true);
    closeResourceCatalog();
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledWith("osa_1", expect.objectContaining({
      definition: expect.objectContaining({ tool_refs: ["bi_query"] }),
    })));
  });

  it("explains a changed approval availability when saving a stale picker", async () => {
    vi.mocked(saveAgentWorkingCopy).mockRejectedValue(new ApiError(422, "Unavailable", { code: "approval_executor_unavailable" }));
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "保存草稿" }));
    expect(await screen.findByText(/当前部署无法执行此工具所需的审批流程/)).toBeTruthy();
  });

  it("uses the server model policy and preserves an exact primary responsibility", async () => {
    const primaryDetail = detail();
    primaryDetail.live.definition = {
      ...primaryDetail.live.definition,
      primary_capability_ref: "cap_report@1",
    };
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(primaryDetail);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    expect(await screen.findByText("由团队模型策略固定；发布时服务端会再次校验。")).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "模型角色" })).toBeNull();
    await openConfigView("职责承诺");
    expect((screen.getByRole("radio", { name: /撰写报告/ }) as HTMLInputElement).checked).toBe(true);

    await openConfigView("Prompt");
    fireEvent.change(screen.getByRole("textbox", { name: "一句话介绍" }), {
      target: { value: "Updated responsibility." },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledWith(
      "osa_1",
      expect.objectContaining({
        // The Agent has no stored model, so it keeps following the policy role instead of pinning it.
        definition: expect.objectContaining({
          model: null,
          primary_capability_ref: "cap_report@1",
        }),
      }),
    ));
  });

  it("renders future model choices from server policy without a frontend enum", async () => {
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({
      ...CATALOG,
      model_policy: {
        ...CATALOG.model_policy,
        allowed_roles: ["analyst", "specialist"],
        locked: false,
        policy_version: "team-agent-model-policy-v2",
      },
    });

    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    const policy = await screen.findByRole("combobox", { name: "模型角色" });
    expect(policy.textContent).toContain("specialist");
  });

  it("shows shadow responsibilities but does not allow selecting them", async () => {
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({
      ...CATALOG,
      primary_capabilities: [{
        ...CATALOG.primary_capabilities[0],
        selectable: false,
        verification_state: "shadow",
        reason_codes: ["no_reviewed_implementation:cap_report@1"],
      }],
    });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    await openConfigView("职责承诺");
    fireEvent.click(screen.getByText("查看 1 项正在验证的职责"));
    const responsibility = screen.getByRole("radio", { name: /撰写报告/ });
    expect(responsibility.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("平台正在逐项验证可承诺的主职责。你仍可继续配置 Tool 与 Skill；未验证能力不会被包装成已可用。")).toBeTruthy();
  });

  it("keeps a selected personal Skill removable but blocks sharing it", async () => {
    const personalGrant = {
      ...GRANT,
      revision: `sha256:${"3".repeat(64)}`,
      skill_ref: { id: "sk_private", kind: "user" as const, name: "my-method" },
    };
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({
      ...CATALOG,
      skills: [{
        name: "my-method",
        display_name: "My method",
        description: "Private operating method.",
        source_kind: "user",
        linked_tools: [],
        allowed_audiences: ["private"],
        grant: personalGrant,
      }],
    });
    const privateDetail = detail();
    privateDetail.live.definition = {
      ...privateDetail.live.definition,
      skills_json: JSON.stringify(["my-method"]),
    };
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(privateDetail);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    await openConfigView("可见范围");
    fireEvent.click(await screen.findByRole("radio", { name: "全团队" }));

    // The section header and the inline note both name the blocker.
    expect(screen.getAllByText(/个人技能只能添加到/).length).toBeGreaterThan(0);
    const personalSkill = screen.getByRole("checkbox", { name: /My method/ });
    expect(personalSkill.hasAttribute("disabled")).toBe(false);
    expect(screen.getByRole("button", { name: "保存草稿" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(personalSkill);
    await openResourceCatalog();
    const unavailableSkill = screen.getByRole("checkbox", {
      name: /My method/, description: /当前智能体的使用范围为“全团队”/,
    });
    expect(unavailableSkill.hasAttribute("disabled")).toBe(true);
  });

  it("saves the catalog grant, which carries run permission, in place of an older stored grant", async () => {
    const runnableGrant: AgentExactSkillGrant = {
      ...GRANT,
      permissions: ["read", "invoke"],
      revision: `sha256:${"5".repeat(64)}`,
    };
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({
      ...CATALOG,
      skills: [{ ...CATALOG.skills[0], grant: runnableGrant, script_runtime: "enabled" }],
    });
    const exactDetail = detail();
    exactDetail.live.definition = {
      ...exactDetail.live.definition,
      skill_grants: [GRANT],
      skills_json: undefined,
    };
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(exactDetail);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    fireEvent.change(await screen.findByRole("textbox", { name: "一句话介绍" }), {
      target: { value: "Updated description" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledWith(
      "osa_1",
      expect.objectContaining({
        definition: expect.objectContaining({ skill_grants: [runnableGrant] }),
      }),
    ));
  });

  it("drops stored Skill operation Tools and never offers them in the picker", async () => {
    const skillTool = (ref: string) => ({ ...CATALOG.tools[0], stable_ref: ref, display_name: ref, description: ref });
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({
      ...CATALOG,
      tools: [...CATALOG.tools, skillTool("skill_read"), skillTool("skill_invoke"), skillTool("skill_list")],
    });
    const storedDetail = detail();
    storedDetail.live.definition = {
      ...storedDetail.live.definition,
      tool_refs_json: JSON.stringify(["bi_query", "skill_invoke", "skill_read"]),
    };
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(storedDetail);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    const save = await screen.findByRole("button", { name: "保存草稿" });
    expect(screen.queryByText("需要处理的旧 Tool")).toBeNull();
    await openResourceCatalog();
    for (const ref of ["skill_read", "skill_invoke", "skill_list"]) {
      expect(screen.queryByRole("checkbox", { name: new RegExp(ref) })).toBeNull();
    }
    closeResourceCatalog();
    fireEvent.click(save);
    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledWith("osa_1", expect.objectContaining({
      definition: expect.objectContaining({ tool_refs: ["bi_query"] }),
    })));
  });

  it("notes beside a runnable Skill that this deployment does not run Skill scripts", async () => {
    vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({
      ...CATALOG,
      skills: [{ ...CATALOG.skills[0], grant: { ...GRANT, permissions: ["read", "invoke"] }, script_runtime: "disabled" }],
    });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    expect(await screen.findByText("当前部署未开启技能脚本；智能体可以读取此技能，但不能运行其中的脚本。")).toBeTruthy();
  });

  it("names each blocked Skill and its reason when saving is refused", async () => {
    vi.mocked(saveAgentWorkingCopy).mockRejectedValue(new ApiError(422, "Blocked", {
      code: "agent_dependency_blocked",
      details: {
        reason_codes: ["skill_pending_reapproval"],
        blocked_items: [
          { kind: "skill", name: "Sales report", identity: "team:sk_1", reason_codes: ["skill_pending_reapproval"] },
          { kind: "tool", name: "CRM", identity: "teammcp.crm.get", reason_codes: ["not_a_known_code"] },
        ],
      },
    }));
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "保存草稿" }));

    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("技能「Sales report」：已修改，正在等待团队管理员重新审核。");
    expect(banner.textContent).toContain("工具「CRM」：此智能体当前无法使用它。");
  });

  it("explains a dependency block without item details", async () => {
    vi.mocked(saveAgentWorkingCopy).mockRejectedValue(new ApiError(422, "Blocked", { code: "agent_dependency_blocked" }));
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "保存草稿" }));
    expect(await screen.findByText(/有技能或工具无法用于此智能体/)).toBeTruthy();
  });

  it("tells the owner which Skills were updated since the live release", async () => {
    const updated = detail();
    updated.live.skill_updates = [{
      name: "Sales report",
      identity: "team:sk_1",
      release_revision: `sha256:${"1".repeat(64)}`,
      current_revision: `sha256:${"2".repeat(64)}`,
      changed_at: null,
    }];
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(updated);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    const notice = await screen.findByText(/所用技能已更新：Sales report。/);
    expect(notice.textContent).toContain("建议重新评测。");
  });

  it("does not discard unsaved edits when a resource action is requested", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    fireEvent.change(await screen.findByRole("textbox", { name: "一句话介绍" }), {
      target: { value: "An unsaved description" },
    });
    fireEvent.click(screen.getByRole("button", { name: /的更多操作$/ }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "停用智能体" }));

    expect(setManagedAgentResourceStatus).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("请先保存当前修改");
  });

  it("brings a newly reported failure into view", async () => {
    // Every action on this page reports into one banner under the hero, while most of them sit far
    // below it. Dogfooding 116 found a rejected webhook URL rendered 1,174px above the button that
    // caused it — correct copy, never seen. jsdom has no scrollIntoView, so record the calls.
    const scrolled: Element[] = [];
    const proto = Element.prototype as { scrollIntoView?: (arg?: unknown) => void };
    const original = proto.scrollIntoView;
    proto.scrollIntoView = function (this: Element) { scrolled.push(this); };
    try {
      renderIntl(<AgentReleaseManager agentId="osa_1" />);
      fireEvent.change(await screen.findByRole("textbox", { name: "一句话介绍" }), {
        target: { value: "An unsaved description" },
      });
      fireEvent.click(screen.getByRole("button", { name: /的更多操作$/ }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "停用智能体" }));

      const banner = screen.getByRole("alert");
      expect(banner.textContent).toContain("请先保存当前修改");
      await waitFor(() => expect(scrolled).toContain(banner));
    } finally {
      if (original) proto.scrollIntoView = original;
      else delete proto.scrollIntoView;
    }
  });

  it("follows the latest Skill revision instead of asking to remove and reselect it", async () => {
    const olderGrant: AgentExactSkillGrant = {
      ...GRANT,
      revision: `sha256:${"9".repeat(64)}`,
    };
    const olderDetail = detail();
    olderDetail.live.definition = {
      ...olderDetail.live.definition,
      skill_grants: [olderGrant],
      skills_json: undefined,
    };
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(olderDetail);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    const save = await screen.findByRole("button", { name: "保存草稿" });
    expect(screen.queryByText("需要处理的旧 Skill")).toBeNull();
    expect(save.hasAttribute("disabled")).toBe(false);
    fireEvent.click(save);

    await waitFor(() => expect(saveAgentWorkingCopy).toHaveBeenCalledWith(
      "osa_1",
      expect.objectContaining({
        definition: expect.objectContaining({ skill_grants: [GRANT] }),
      }),
    ));
  });

  it("still asks to remove a Skill whose name now belongs to a different Skill", async () => {
    const otherGrant: AgentExactSkillGrant = {
      ...GRANT,
      skill_ref: { id: "sk_other", kind: "team", name: "pptx" },
    };
    const otherDetail = detail();
    otherDetail.live.definition = {
      ...otherDetail.live.definition,
      skill_grants: [otherGrant],
      skills_json: undefined,
    };
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(otherDetail);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    const save = await screen.findByRole("button", { name: "保存草稿" });
    expect(screen.getByText("需要处理的旧 Skill")).toBeTruthy();
    expect(save.hasAttribute("disabled")).toBe(true);
  });

  it("explains and manages future copy eligibility without changing the current agent", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    await openConfigView("介绍与高级");
    expect(screen.getByText(/启用版本不是独立的市场上架操作/)).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "复制设置" })).toBeTruthy();
    expect(screen.getByText("允许复制后会发生什么？")).toBeTruthy();
    expect(screen.getByText(/受众、分享关系、访问授权、凭据、审批与评测历史不会复制/)).toBeTruthy();
    expect(screen.getByText(/都不会自动改动或撤销已经创建的副本/)).toBeTruthy();
    const policy = screen.getByRole("combobox", { name: "谁可以复制为自己的版本？" });
    expect((policy as HTMLSelectElement).value).toBe("not_allowed");
    fireEvent.change(policy, { target: { value: "recipients" } });

    await waitFor(() => expect(updateAgentForkPolicy).toHaveBeenCalledWith(
      "osa_1",
      "recipients",
      0,
    ));
    expect(screen.getByText(/只影响之后创建的副本/)).toBeTruthy();
  });

  it("localizes copy settings and its explanation in English without exposing Fork", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" />, { locale: "en-US" });

    await openConfigView("Listing details and advanced");
    expect(await screen.findByRole("heading", { name: "Copy settings" })).toBeTruthy();
    expect(screen.getByText("What happens when copying is allowed?")).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Who can copy this agent?" })).toBeTruthy();
    expect(screen.queryByText(/\bfork\b/i)).toBeNull();
  });

  it("links an accessible copy source without exposing its definition", async () => {
    vi.mocked(getAgentForkSettings).mockResolvedValueOnce({
      kind: "agent_fork_settings",
      schema_version: 1,
      agent_id: "osa_1",
      policy: { policy: "not_allowed", row_version: 0, updated_at: null },
      lineage: {
        lineage_id: "afk_1",
        source_kind: "team",
        source_agent_id: "osa_source",
        source_display_name: "Source Analyst",
        source_revision_ref: "arv_source",
        fork_revision_ref: "arv_fork",
        source_accessible: true,
        upstream: {
          current_revision_ref: "arv_source",
          update_available: false,
          applied_automatically: false,
          comparison: null,
        },
        dependency_gaps: [],
        created_at: "2026-09-06T00:00:00Z",
      },
    });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    await openConfigView("介绍与高级");
    const source = await screen.findByRole("link", { name: "Source Analyst" });
    expect(source.getAttribute("href")).toBe("/agents/detail/?id=osa_source");
    expect(screen.queryByText("Initial private instructions.")).toBeNull();
  });

  it("says who can unblock a release the server refused, instead of the generic failure", async () => {
    // Agent Eval on means every non-private release now goes through review, so a team
    // with no admin hits `qualified_reviewer_unavailable` on a path that used to publish
    // directly.  "Fix the safety or dependency issue and retry" sends them nowhere.
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail({
      working_copy: WORKING_COPY,
      actions: { edit: true, publish: true, enable: false, disable: true, archive: true, restore: false },
    }));
    vi.mocked(runAgentSafetyCheck).mockResolvedValue(SAFETY_PASSED);
    vi.mocked(publishAgentWorkingCopy).mockRejectedValue(
      new ApiError(422, "qualified_reviewer_unavailable", { code: "qualified_reviewer_unavailable" }),
    );

    renderIntl(<AgentReleaseManager agentId="osa_1" />);

    fireEvent.click(await screen.findByRole("tab", { name: "发布" }));
    fireEvent.click(await screen.findByRole("button", { name: "运行安全检查" }));
    await waitFor(() => expect(runAgentSafetyCheck).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: "提交管理员审批" }));

    expect(await screen.findByText(/这个团队目前没有可审批的人/)).toBeTruthy();
  });

  it.each([
    { requires: true, button: "提交管理员审批", note: /这次发布需要管理员审批/ },
    { requires: false, button: "启用新版本", note: /常规风险的指定分享可直接发布/ },
  ])(
    "renders the publish action from the server's own routing answer (requires_admin_review=$requires)",
    async ({ requires, button, note }) => {
      // A `custom` share used to be labelled "Release new version" unconditionally,
      // because the frontend guessed from the audience. With the Agent Eval gate on,
      // the server queues every non-private release for approval, and the guess
      // became a promise the server does not keep.
      vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail({
        working_copy: { ...WORKING_COPY, audience: { schema_version: 1, effective_mode: "custom", user_ids: ["user_2"], group_ids: [] } },
        actions: { edit: true, publish: true, enable: false, disable: true, archive: true, restore: false },
      }));
      vi.mocked(runAgentSafetyCheck).mockResolvedValue({ ...SAFETY_PASSED, requires_admin_review: requires });

      renderIntl(<AgentReleaseManager agentId="osa_1" />);

      fireEvent.click(await screen.findByRole("tab", { name: "发布" }));
      fireEvent.click(await screen.findByRole("button", { name: "运行安全检查" }));
      await waitFor(() => expect(runAgentSafetyCheck).toHaveBeenCalled());

      expect(await screen.findByRole("button", { name: button })).toBeTruthy();
      expect(screen.getByText(note)).toBeTruthy();
    },
  );

  it("consumes a feedback prefill handoff, gates apply on review, and asks to publish after save", async () => {
    // agent-feedback-loop FE batch B: inbox Adopt and Prefill → editor diff review → save → ask to release (one journey).
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&feedback_prefill=1");
    sessionStorage.setItem("bowhead:feedback-prefill", JSON.stringify({
      candidate_id: "fbc_5c21",
      target_kind: "agent_def",
      target_ref: "osa_1",
      prefill: { kind: "agent_system_prompt", edited_content: "Analyze the monthly result.\nLead with the conclusion.", rationale: "ok" },
      base: { definition_sha256: "abc" },
      banner: { candidate_id: "fbc_5c21", proposer_id: "u_wang", evidence_count: 3 },
    }));
    const saved = detail({
      working_copy: {
        kind: "agent_working_copy", schema_version: 1, id: "awc_1", agent_id: "osa_1", base_release_id: "arl_1",
        definition: { system_prompt: "Analyze the monthly result.\nLead with the conclusion.", max_iterations: 10, tool_refs_json: JSON.stringify(["bi_query"]), skills_json: JSON.stringify(["pptx"]) },
        listing: { display_name: "Finance Helper", description: "Explains monthly performance." },
        audience: { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] },
        row_version: 1, updated_at: "2026-09-09T00:00:00Z",
      } as never,
      actions: { edit: true, publish: true, enable: false, disable: true, archive: true, restore: false },
    });
    vi.mocked(getAgentWorkingCopy).mockResolvedValueOnce(detail()).mockResolvedValue(saved);
    // Safety check raises row_version (1→2): release must use the report's version or get 409 (caught on real 116).
    vi.mocked(runAgentSafetyCheck).mockResolvedValue({ ...SAFETY_PASSED, subject: { ...SAFETY_PASSED.subject, working_copy_row_version: 2 } });
    // The server freezes a candidate (`prepared`); it never activates from this request.
    vi.mocked(publishAgentWorkingCopy).mockResolvedValue({ kind: "agent_version_preparation", state: "prepared" } as never);

    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    await screen.findByTestId("feedback-prefill-panel");
    // Mount only reads, never deletes: refresh/load failure must not lose an accepted candidate's coaching draft; clear only on apply.
    expect(sessionStorage.getItem("bowhead:feedback-prefill")).not.toBeNull();
    const apply = screen.getByRole("button", { name: "应用到编辑器" }) as HTMLButtonElement;
    expect(apply.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: "我已逐行审阅预填内容" }));
    fireEvent.click(apply);
    expect((document.querySelector('textarea[name="agent-system-prompt"]') as HTMLTextAreaElement).value).toContain("Lead with the conclusion.");
    expect(screen.queryByTestId("feedback-prefill-panel")).toBeNull();
    expect(sessionStorage.getItem("bowhead:feedback-prefill")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByTestId("feedback-ask-publish");
    expect(screen.getByText("已保存。要现在发布吗？")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发布" }));
    await waitFor(() => expect(publishAgentWorkingCopy).toHaveBeenCalled());
    expect(runAgentSafetyCheck).toHaveBeenCalledBefore(vi.mocked(publishAgentWorkingCopy));
    // The server answers 422 agent_eval_workspace_required without the current Space.
    expect(vi.mocked(publishAgentWorkingCopy).mock.calls[0][1]).toMatchObject({ working_copy_row_version: 2, workspace_id: "ws_1" });
    expect(await screen.findByText(/候选已准备，已发布版本不变/)).toBeTruthy();
    expect(screen.queryByText(/新版本已成为当前可用版本/)).toBeNull();
  });

  it("asks for a Space before publishing instead of sending a request the server must refuse", async () => {
    spaceSelection.activeId = null;
    try {
      window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&feedback_prefill=1");
      sessionStorage.setItem("bowhead:feedback-prefill", JSON.stringify({
        candidate_id: "fbc_1", target_kind: "agent_def", target_ref: "osa_1",
        prefill: { kind: "agent_system_prompt", edited_content: "Analyze the monthly result.\nX", rationale: "" },
        base: {}, banner: { candidate_id: "fbc_1", proposer_id: "u", evidence_count: 1 },
      }));
      const saved = detail({
        working_copy: {
          kind: "agent_working_copy", schema_version: 1, id: "awc_1", agent_id: "osa_1", base_release_id: "arl_1",
          definition: { system_prompt: "Analyze the monthly result.\nX", max_iterations: 10, tool_refs_json: JSON.stringify(["bi_query"]), skills_json: JSON.stringify(["pptx"]) },
          listing: { display_name: "Finance Helper", description: "Explains monthly performance." },
          audience: { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] },
          row_version: 1, updated_at: "2026-09-09T00:00:00Z",
        } as never,
        actions: { edit: true, publish: true, enable: false, disable: true, archive: true, restore: false },
      });
      vi.mocked(getAgentWorkingCopy).mockResolvedValueOnce(detail()).mockResolvedValue(saved);
      vi.mocked(runAgentSafetyCheck).mockResolvedValue(SAFETY_PASSED);
      renderIntl(<AgentReleaseManager agentId="osa_1" />);
      await screen.findByTestId("feedback-prefill-panel");
      fireEvent.click(screen.getByRole("checkbox", { name: "我已逐行审阅预填内容" }));
      fireEvent.click(screen.getByRole("button", { name: "应用到编辑器" }));
      fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
      await screen.findByTestId("feedback-ask-publish");
      fireEvent.click(screen.getByRole("button", { name: "发布" }));
      expect(await screen.findByText("请先选择空间；评测要用明确的数据范围。")).toBeTruthy();
      expect(publishAgentWorkingCopy).not.toHaveBeenCalled();
    } finally {
      spaceSelection.activeId = "ws_1";
    }
  });

  it("hides the publish prompt again when the user edits after saving, and 先不发布 never publishes", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&feedback_prefill=1");
    sessionStorage.setItem("bowhead:feedback-prefill", JSON.stringify({
      candidate_id: "fbc_1", target_kind: "agent_def", target_ref: "osa_1",
      prefill: { kind: "agent_system_prompt", edited_content: "Analyze the monthly result.\nX", rationale: "" },
      base: {}, banner: { candidate_id: "fbc_1", proposer_id: "u", evidence_count: 1 },
    }));
    const saved = detail({
      working_copy: {
        kind: "agent_working_copy", schema_version: 1, id: "awc_1", agent_id: "osa_1", base_release_id: "arl_1",
        definition: { system_prompt: "Analyze the monthly result.\nX", max_iterations: 10, tool_refs_json: JSON.stringify(["bi_query"]), skills_json: JSON.stringify(["pptx"]) },
        listing: { display_name: "Finance Helper", description: "Explains monthly performance." },
        audience: { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] },
        row_version: 1, updated_at: "2026-09-09T00:00:00Z",
      } as never,
      actions: { edit: true, publish: true, enable: false, disable: true, archive: true, restore: false },
    });
    vi.mocked(getAgentWorkingCopy).mockResolvedValueOnce(detail()).mockResolvedValue(saved);
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    await screen.findByTestId("feedback-prefill-panel");
    fireEvent.click(screen.getByRole("checkbox", { name: "我已逐行审阅预填内容" }));
    fireEvent.click(screen.getByRole("button", { name: "应用到编辑器" }));
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByTestId("feedback-ask-publish");
    fireEvent.change(screen.getByRole("textbox", { name: "一句话介绍" }), { target: { value: "edited again" } });
    expect(screen.queryByTestId("feedback-ask-publish")).toBeNull(); // Editing prevents immediate release.
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByTestId("feedback-ask-publish"); // Save again, ask again.
    fireEvent.click(screen.getByRole("button", { name: "先不发布" }));
    expect(screen.queryByTestId("feedback-ask-publish")).toBeNull();
    expect(publishAgentWorkingCopy).not.toHaveBeenCalled();
  });

  it("does not ask to publish after an ordinary save without a prefill", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1");
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "保存草稿" }));
    await screen.findByText("草稿已保存，已发布的 v1 没有变化。");
    expect(screen.queryByTestId("feedback-ask-publish")).toBeNull();
  });

  it("ignores a stale handoff for another agent and clears it", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&feedback_prefill=1");
    sessionStorage.setItem("bowhead:feedback-prefill", JSON.stringify({ candidate_id: "x", target_kind: "agent_def", target_ref: "osa_other", prefill: { kind: "agent_system_prompt", edited_content: "nope", rationale: "" }, base: {}, banner: { candidate_id: "x", proposer_id: "u", evidence_count: 1 } }));
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    await screen.findByRole("button", { name: "保存草稿" });
    expect(screen.queryByTestId("feedback-prefill-panel")).toBeNull();
    expect(sessionStorage.getItem("bowhead:feedback-prefill")).toBeNull();
  });

  it("locks a stale prefill and realigns it against the current working copy", async () => {
    const { redraftFeedbackCandidate } = await import("@/lib/feedback-candidates-api");
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1&feedback_prefill=1");
    sessionStorage.setItem("bowhead:feedback-prefill", JSON.stringify({
      candidate_id: "fbc_9", target_kind: "agent_def", target_ref: "osa_1",
      prefill: { kind: "agent_system_prompt", edited_content: "old draft", rationale: "" },
      base: { definition_sha256: "x", working_copy_row_version: 1 },
      banner: { candidate_id: "fbc_9", proposer_id: "u", evidence_count: 1 },
    }));
    vi.mocked(getAgentWorkingCopy).mockResolvedValue(detail({
      working_copy: {
        kind: "agent_working_copy", schema_version: 1, id: "awc_1", agent_id: "osa_1", base_release_id: "arl_1",
        definition: { system_prompt: "Analyze the monthly result.", max_iterations: 10, tool_refs_json: JSON.stringify(["bi_query"]), skills_json: JSON.stringify(["pptx"]) },
        listing: { display_name: "Finance Helper", description: "Explains monthly performance." },
        audience: { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] },
        row_version: 3, updated_at: "2026-09-09T00:00:00Z",
      } as never,
    }));
    vi.mocked(redraftFeedbackCandidate).mockResolvedValue({
      id: "fbc_9", state: "accepted",
      prefill: { kind: "agent_system_prompt", edited_content: "Analyze the monthly result.\nRealigned.", rationale: "" },
      base: { definition_sha256: "y", working_copy_row_version: 3 },
      banner: { candidate_id: "fbc_9", proposer_id: "u", evidence_count: 1 },
    });
    renderIntl(<AgentReleaseManager agentId="osa_1" />);
    await screen.findByTestId("feedback-prefill-stale");
    expect((screen.getByRole("checkbox", { name: "我已逐行审阅预填内容" }) as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "基于新基线重新对齐" }));
    await waitFor(() => expect(screen.queryByTestId("feedback-prefill-stale")).toBeNull());
    expect(redraftFeedbackCandidate).toHaveBeenCalledWith("fbc_9");
    // New draft = unreviewed: reset checkbox, keep Apply disabled.
    expect((screen.getByRole("checkbox", { name: "我已逐行审阅预填内容" }) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole("button", { name: "应用到编辑器" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("feedback-prefill-diff").textContent).toContain("Realigned.");
    expect(JSON.parse(sessionStorage.getItem("bowhead:feedback-prefill")!).base.working_copy_row_version).toBe(3);
  });
  it("keeps the Owner draft when an in-app departure is cancelled", async () => {
    renderIntl(<AgentReleaseManager agentId="osa_1" returnHref="/agents/manage/archived/" />);
    const prompt = await screen.findByRole("textbox", { name: "智能体指令" });
    fireEvent.change(prompt, { target: { value: "Unsaved owner instructions" } });
    const proceed = vi.fn();
    const { BEFORE_APP_NAVIGATION_EVENT } = await import("@/lib/use-app-router");
    let allowed = true;
    act(() => { allowed = window.dispatchEvent(new CustomEvent(BEFORE_APP_NAVIGATION_EVENT, { cancelable: true, detail: { href: "/skills/", proceed } })); });
    expect(allowed).toBe(false);
    fireEvent.click(await screen.findByRole("button", { name: "继续编辑" }));
    expect(proceed).not.toHaveBeenCalled();
    expect((prompt as HTMLTextAreaElement).value).toBe("Unsaved owner instructions");
    act(() => { window.dispatchEvent(new CustomEvent(BEFORE_APP_NAVIGATION_EVENT, { cancelable: true, detail: { href: "/skills/", proceed } })); });
    fireEvent.click(await screen.findByRole("button", { name: "放弃修改" }));
    await waitFor(() => expect(proceed).toHaveBeenCalledOnce());
  });

});

describe("agentManagerLocation", () => {
  it.each([
    ["?section=changes", { task: "config", section: null, anchor: "agent-pending-changes", legacy: true }],
    ["?section=history", { task: "release", section: null, anchor: "agent-release-history", legacy: true }],
    ["?section=release&release_view=tests", { task: "evaluate", section: null, anchor: null, legacy: true }],
    ["?section=release&release_view=gate", { task: "release", section: null, anchor: null, legacy: true }],
    ["?section=config&config_view=prompt", { task: "config", section: "does", anchor: null, legacy: true }],
    ["?section=config&config_view=capabilities", { task: "config", section: "does", anchor: null, legacy: true }],
    ["?section=config&config_view=resources", { task: "config", section: "uses", anchor: null, legacy: true }],
    ["?section=config&config_view=access", { task: "config", section: "who", anchor: null, legacy: true }],
    ["?section=config&config_view=market", { task: "config", section: "who", anchor: null, legacy: true }],
    ["?section=evaluate", { task: "evaluate", section: null, anchor: null, legacy: false }],
    ["?section=config&config_section=uses", { task: "config", section: "uses", anchor: null, legacy: false }],
    ["?section=unknown", { task: "config", section: null, anchor: null, legacy: false }],
  ])("maps %s", (search, expected) => {
    expect(agentManagerLocation(search)).toEqual(expected);
  });
});
