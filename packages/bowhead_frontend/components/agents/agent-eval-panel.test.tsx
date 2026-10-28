import { useState } from "react";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  getAgentEvalManager,
  getAgentEvalRun,
  getAgentEvalRunCaseAnswers,
  listAgentEvalConversationCandidates,
  runAgentEval,
  saveAgentEvalSuite,
  suggestAgentEvalCases,
  suggestAgentEvalCasesFromTurns,
  updateAgentEvalTriggerPolicy,
  type AgentEvalManager,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { reviewAgentEvalSuite } from "@/lib/agent-eval-alignment-api";
import { setAgentEvalAlignmentCapabilityForTest } from "@/lib/use-agent-eval-alignment-capability";
import { AgentEvalPanel, savedCaseBody, unsavedCaseChanges } from "./agent-eval-panel";

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ activeId: "ws_1", items: [], loading: false, error: null }),
}));

vi.mock("@/lib/agent-eval-alignment-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agent-eval-alignment-api")>()),
  reviewAgentEvalSuite: vi.fn().mockResolvedValue({ status: "unavailable", gaps: [], duplicate_cases: [] }),
  reviewAgentEvalPoints: vi.fn().mockResolvedValue({ status: "unavailable", points: [] }),
}));

vi.mock("@/lib/agents-api", () => ({
  cancelAgentEvalRun: vi.fn(),
  getAgentEvalManager: vi.fn(),
  getAgentEvalRun: vi.fn(),
  getAgentEvalRunCaseAnswers: vi.fn(),
  runAgentEval: vi.fn(),
  saveAgentEvalSuite: vi.fn(),
  suggestAgentEvalCases: vi.fn(),
  listAgentEvalConversationCandidates: vi.fn(),
  suggestAgentEvalCasesFromTurns: vi.fn(),
  updateAgentEvalTriggerPolicy: vi.fn(),
}));

const MANAGER: AgentEvalManager = {
  kind: "agent_eval_manager",
  schema_version: 1,
  agent_id: "osa_1",
  target: { kind: "candidate", candidate_id: "arc_1", revision_id: "arv_2" },
  suites: [{
    suite_id: "aes_contract",
    suite_kind: "platform_contract",
    name: "Safe execution contract",
    visibility: "team_evidence",
    state: "active",
    row_version: 1,
    platform_managed: true,
    revision: {
      suite_revision_id: "aev_contract",
      sequence: 1,
      case_count: 1,
      created_at: "2026-09-05T00:00:00Z",
      cases: [],
    },
    evidence: { state: "missing", stale_reason: null, updated_at: null, run: null },
  }],
  runs: [],
  trigger_policy: {
    enabled: true,
    on_definition_change: true,
    on_dependency_change: true,
    on_model_change: true,
    row_version: 1,
  },
  release_gate: { required: true, ready: false, reason: "platform_contract_not_current" },
};

describe("AgentEvalPanel", () => {
  beforeEach(() => {
    vi.mocked(getAgentEvalManager).mockResolvedValue(MANAGER);
    vi.mocked(runAgentEval).mockResolvedValue({
      kind: "agent_eval_run",
      schema_version: 1,
      run_id: "aer_1",
      suite_revision_id: "aev_contract",
      target_kind: "candidate",
      revision_id: "arv_2",
      status: "running",
      trigger_kind: "manual",
      summary: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("says only the basic safety check exists when the Owner has no cases, even with the gate ready", async () => {
    vi.mocked(getAgentEvalManager).mockResolvedValue({ ...MANAGER, release_gate: { required: true, ready: true, reason: null } });
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    expect(await screen.findByText("只有基础安全检查")).toBeTruthy();
    expect(screen.queryByText("发布证据已就绪")).toBeNull();
    expect(screen.getAllByRole("button", { name: "添加用例" }).length).toBe(2);
  });

  it("explains the exact candidate gate and starts the managed contract in the active space", async () => {
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    expect(await screen.findByText("评测对象：这次修改")).toBeTruthy();
    expect(screen.getByText("审批前需要完成评测")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "运行这组评测" }));

    await waitFor(() => expect(runAgentEval).toHaveBeenCalledWith("osa_1", {
      suite_revision_id: "aev_contract",
      workspace_id: "ws_1",
      target: "candidate",
      expected_revision_id: "arv_2",
      attempts: 1,
    }, expect.any(String)));
    expect(await screen.findByText(/评测正在后台运行/)).toBeTruthy();
  });

  it("speaks to a private Agent's Owner without approval, and names the released version", async () => {
    vi.mocked(getAgentEvalManager).mockResolvedValue({ ...MANAGER, target: { kind: "live" } });
    const onSuiteSaved = vi.fn();
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" privateAgent liveVersion={2} hideSafetyOnly onSuiteSaved={onSuiteSaved} />);

    expect(await screen.findByText("评测对象：已发布 v2")).toBeTruthy();
    expect(screen.getByText("还没有评测运行记录")).toBeTruthy();
    expect(screen.queryByText("审批前需要完成评测")).toBeNull();
    expect(screen.queryByText(/管理员批准/)).toBeNull();
    // A suite's state counts only its own runs, and says so.
    expect(screen.getAllByText("还没单独运行").length).toBeGreaterThan(0);
  });

  it("tells the page when the suite is saved and leaves out a safety-only card the test card already shows", async () => {
    vi.mocked(getAgentEvalManager).mockResolvedValue({ ...MANAGER, release_gate: { required: true, ready: true, reason: null } });
    vi.mocked(saveAgentEvalSuite).mockResolvedValue(MANAGER);
    const onSuiteSaved = vi.fn();
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" hideSafetyOnly onSuiteSaved={onSuiteSaved} />);

    fireEvent.change(await screen.findByLabelText("测试用例 1"), { target: { value: "华东上季度收入变化多少？" } });
    expect(screen.queryByText("只有基础安全检查")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "保存效果测试" }));
    await waitFor(() => expect(onSuiteSaved).toHaveBeenCalledOnce());
  });

  it("saves a new owner case that lets the Agent call its Tools", async () => {
    vi.mocked(saveAgentEvalSuite).mockResolvedValue(MANAGER);
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    fireEvent.change(await screen.findByLabelText("测试用例 1"), { target: { value: "华东上季度收入变化多少？" } });
    fireEvent.click(screen.getByRole("button", { name: "保存效果测试" }));

    await waitFor(() => expect(saveAgentEvalSuite).toHaveBeenCalledTimes(1));
    const scorerConfig = vi.mocked(saveAgentEvalSuite).mock.calls[0][1].cases[0].scorer_config;
    expect(scorerConfig).not.toHaveProperty("max_tool_calls");
    expect(scorerConfig.critical ?? []).not.toContain("no_fabrication");
  });

  it("saves a loaded suite with its points and without server-only fields", async () => {
    const point = { id: "p_0001", kind: "judgment" as const, text: "Compares with last year", source: "coach_task" as const, state: "suggested" as const };
    vi.mocked(getAgentEvalManager).mockResolvedValue({
      ...MANAGER,
      suites: [...MANAGER.suites, {
        ...MANAGER.suites[0],
        suite_id: "aes_owner",
        suite_kind: "owner_quality",
        name: "My checks",
        platform_managed: false,
        revision: {
          suite_revision_id: "aev_owner",
          sequence: 1,
          case_count: 1,
          created_at: "2026-10-18T00:00:00Z",
          cases: [{
            case_key: "quality.1",
            case_revision_id: "aec_owner_1",
            input: { task: "How did revenue change?" },
            fixture_ref: {},
            expected_contract: { allowed_terminal_statuses: ["completed"], points: [point] },
            scorer_config: {},
            effect_policy: { mode: "no_effects" },
          }],
        },
      }],
    });
    vi.mocked(saveAgentEvalSuite).mockResolvedValue(MANAGER);
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    expect(await screen.findByText("How did revenue change?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "保存效果测试" }));

    await waitFor(() => expect(saveAgentEvalSuite).toHaveBeenCalledTimes(1));
    const [saved] = vi.mocked(saveAgentEvalSuite).mock.calls[0][1].cases;
    // The save endpoint forbids unknown fields, so echoing `input` or `case_revision_id` would fail the save.
    expect(saved).not.toHaveProperty("input");
    expect(saved).not.toHaveProperty("case_revision_id");
    expect(saved).toMatchObject({ case_key: "quality.1", task: "How did revenue change?" });
    expect(saved.expected_contract.points).toEqual([point]);
  });

  it("removes the last saved case, saves the empty set and can add a case again", async () => {
    const owner = {
      ...MANAGER.suites[0], suite_id: "aes_owner", suite_kind: "owner_quality" as const,
      name: "My checks", platform_managed: false,
      revision: { ...MANAGER.suites[0].revision!, cases: [{
        case_revision_id: "aec_owner", case_key: "quality.1", input: { task: "Explain the result" },
        fixture_ref: {}, expected_contract: {}, scorer_config: {}, effect_policy: { mode: "no_effects" }, valid_until: null,
      }] },
    };
    vi.mocked(getAgentEvalManager).mockResolvedValue({ ...MANAGER, suites: [...MANAGER.suites, owner] });
    vi.mocked(saveAgentEvalSuite).mockResolvedValue(MANAGER);
    const onSuiteSaved = vi.fn();
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" onSuiteSaved={onSuiteSaved} />, { locale: "en-US" });

    fireEvent.click(await screen.findByRole("button", { name: "More actions for case 1" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remove Case" }));
    expect(screen.queryByTestId("case-1")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save tests" }));
    await waitFor(() => expect(saveAgentEvalSuite).toHaveBeenCalledWith("osa_1", {
      suite_id: "aes_owner", name: "My checks", visibility: "owner", cases: [],
    }));
    await waitFor(() => expect(onSuiteSaved).toHaveBeenCalledOnce());
    expect(screen.queryByRole("heading", { name: "My checks" })).toBeNull();
    expect(screen.getAllByText(/You have no custom test cases/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByRole("button", { name: "Add case" })[0]);
    expect(await screen.findByLabelText("Test case 1")).toBeTruthy();
  });

  it("distinguishes evaluation infrastructure failure from Agent quality", async () => {
    const run = {
      kind: "agent_eval_run" as const,
      schema_version: 1 as const,
      run_id: "aer_infra",
      suite_revision_id: "aev_contract",
      target_kind: "candidate" as const,
      revision_id: "arv_2",
      status: "incomplete" as const,
      trigger_kind: "manual",
      summary: { case_count: 1, "pass@k_cases": 0 },
      results: [{ case_revision_id: "aec_1", case_key: "platform.basic", attempt: 1, status: "infra_failed", deterministic: {}, judge: null, duration_ms: null, cost_microusd: null, failure_code: "eval_infra_unavailable" }],
    };
    vi.mocked(getAgentEvalManager).mockResolvedValue({
      ...MANAGER,
      suites: MANAGER.suites.map((suite) => ({ ...suite, evidence: { ...suite.evidence, state: "incomplete", run } })),
    });
    vi.mocked(getAgentEvalRun).mockResolvedValue(run);
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "查看回答与评分" }));
    // The cause is named (an unclassified platform error here), with the case and what to do; never "environment".
    expect(await screen.findByText(/部分用例运行因平台错误中断 · 受影响的用例：「platform\.basic」。请重新测试；如果再次出现，复制诊断信息/)).toBeTruthy();
  });

  it("labels each suite's last run with the version it ran on (PRD R20)", async () => {
    const run = {
      kind: "agent_eval_run" as const,
      schema_version: 1 as const,
      run_id: "aer_live",
      suite_revision_id: "aev_contract",
      target_kind: "live" as const,
      revision_id: "arv_1",
      status: "passed" as const,
      trigger_kind: "manual",
      summary: { case_count: 1, "pass@k_cases": 1 },
      finished_at: "2026-10-18T08:00:00Z",
    };
    vi.mocked(getAgentEvalManager).mockResolvedValue({
      ...MANAGER,
      suites: MANAGER.suites.map((suite) => ({ ...suite, evidence: { ...suite.evidence, state: "current", run } })),
    });
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />, { locale: "en-US" });

    expect(await screen.findByText(/^Last suite run · on the released version · /)).toBeTruthy();
  });

  it("shows an actionable message when AI suggestions are unavailable", async () => {
    vi.mocked(suggestAgentEvalCases).mockRejectedValue(new Error("Request failed"));
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "让 AI 建议测试用例" }));
    fireEvent.click(screen.getByRole("button", { name: "生成建议" }));

    expect(await screen.findByText("暂时无法生成 AI 建议，你仍然可以手写用例。", { selector: "[role=alert]" })).toBeTruthy();
    expect(screen.queryByText("Request failed")).toBeNull();
  });

  it("lets the owner switch auto-regression on when no policy row exists yet", async () => {
    // Server GET no longer creates policy rows for owners, so absent policy is normal.
    // Previously the panel combined `disabled={!manager.trigger_policy}` for null policy with
    // `if (!policy) return` in `changeRegression`, leaving the switch **permanently unusable**:
    // owners could never enable automatic regression—removing automatic subscription also removed explicit subscription.
    vi.mocked(getAgentEvalManager).mockResolvedValue({ ...MANAGER, trigger_policy: null });
    vi.mocked(updateAgentEvalTriggerPolicy).mockResolvedValue({
      enabled: true,
      on_definition_change: false,
      on_dependency_change: false,
      on_model_change: false,
      row_version: 1,
    });

    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    const disclosure = (await screen.findAllByText("重要变更后自动重跑"))[0].closest("details")!;
    disclosure.open = true;
    const toggle = screen.getByRole("checkbox", { name: /重要变更后自动重跑/ });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    expect((toggle as HTMLInputElement).disabled).toBe(false);

    fireEvent.click(toggle);

    await waitFor(() => expect(updateAgentEvalTriggerPolicy).toHaveBeenCalledWith("osa_1", {
      enabled: true,
      on_definition_change: false,
      on_dependency_change: false,
      on_model_change: false,
      row_version: 0,
    }));
  });

  it("names Owner cases on the old defaults and moves only them to the current defaults (R18)", async () => {
    const owner = {
      ...MANAGER.suites[0],
      suite_id: "aes_owner",
      suite_kind: "owner_quality" as const,
      name: "Quality",
      visibility: "owner" as const,
      platform_managed: false,
      revision: {
        suite_revision_id: "aev_owner",
        sequence: 1,
        case_count: 2,
        created_at: "2026-09-05T00:00:00Z",
        cases: [
          { case_key: "quality.1", input: { task: "Old case" }, expected_contract: {}, scorer_config: { max_tool_calls: 0, critical: ["no_fabrication"] }, effect_policy: { mode: "no_effects" } },
          { case_key: "quality.2", input: { task: "Current case" }, expected_contract: {}, scorer_config: { max_duration_ms: 1 }, effect_policy: { mode: "no_effects" } },
        ],
        cases_on_old_defaults: ["quality.1"],
      },
    };
    vi.mocked(getAgentEvalManager).mockResolvedValue({ ...MANAGER, suites: [...MANAGER.suites, owner] });
    vi.mocked(saveAgentEvalSuite).mockResolvedValue(MANAGER);
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    expect(await screen.findByText(/有 1 条用例还在用之前的默认设置/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "改为当前默认设置" }));

    await waitFor(() => expect(saveAgentEvalSuite).toHaveBeenCalledTimes(1));
    const body = vi.mocked(saveAgentEvalSuite).mock.calls[0][1];
    expect(body.suite_id).toBe("aes_owner");
    expect(body.cases.map((item) => [item.case_key, item.task, item.scorer_config])).toEqual([
      ["quality.1", "Old case", {}],
      ["quality.2", "Current case", { max_duration_ms: 1 }],
    ]);
    expect((await screen.findAllByText(/评测组生成了新版本/)).length).toBeGreaterThan(0);
  });

  it("drafts one case per chosen conversation and keeps the question as the task (R14)", async () => {
    vi.mocked(listAgentEvalConversationCandidates).mockResolvedValue({
      kind: "agent_eval_conversation_candidates",
      schema_version: 1,
      max_selection: 32,
      items: [
        { turn_id: "t_1", question: "华东上季度收入是多少？", answered_at: "2026-10-10T00:00:00Z", shared_by: null },
        { turn_id: "t_2", question: "哪个产品增长最快？", answered_at: "2026-10-09T00:00:00Z", shared_by: "林伟" },
      ],
    });
    vi.mocked(suggestAgentEvalCasesFromTurns).mockResolvedValue({
      kind: "agent_eval_case_suggestions",
      schema_version: 1,
      cases: [{
        case_key: "conversation.1",
        task: "华东上季度收入是多少？",
        expected_contract: { allowed_terminal_statuses: ["completed"], points: [{ id: "p_1a2b", kind: "data_fact", text: "给出华东区上季度营收", source: "coach_conversation", state: "suggested" }] },
        scorer_config: {},
        effect_policy: { mode: "no_effects" },
      }],
    });
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "从对话起草" }));
    fireEvent.click(await screen.findByLabelText(/华东上季度收入是多少/));
    expect(screen.getByText("已选 1 个，最多 32 个")).toBeTruthy();
    // A Turn a member shared names the sharer; the Owner's own Turn doesn't.
    expect(screen.getByLabelText(/哪个产品增长最快/).closest("label")?.textContent).toContain("林伟 分享");
    expect(screen.getByLabelText(/华东上季度收入是多少/).closest("label")?.textContent).not.toContain("分享");
    fireEvent.click(screen.getByRole("button", { name: "起草用例" }));

    await waitFor(() => expect(suggestAgentEvalCasesFromTurns).toHaveBeenCalledWith("osa_1", ["t_1"]));
    // The blank starter case is replaced by the drafted one, which keeps its suggested points.
    expect((await screen.findByLabelText("测试用例 1") as HTMLTextAreaElement).value).toBe("华东上季度收入是多少？");
    expect(screen.queryByLabelText("测试用例 2")).toBeNull();
    expect((await screen.findAllByText(/已从对话起草 1 条用例/)).length).toBeGreaterThan(0);
  });

  it("explains a conversation that disappeared and reloads the list", async () => {
    vi.mocked(listAgentEvalConversationCandidates).mockResolvedValue({
      kind: "agent_eval_conversation_candidates",
      schema_version: 1,
      max_selection: 32,
      items: [{ turn_id: "t_1", question: "华东上季度收入是多少？", answered_at: "2026-10-10T00:00:00Z", shared_by: null }],
    });
    vi.mocked(suggestAgentEvalCasesFromTurns).mockRejectedValue(new ApiError(404, "agent_eval_conversation_unavailable", { code: "agent_eval_conversation_unavailable" }));
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "从对话起草" }));
    fireEvent.click(await screen.findByLabelText(/华东上季度收入是多少/));
    fireEvent.click(screen.getByRole("button", { name: "起草用例" }));

    expect((await screen.findAllByText(/所选对话中有一个已经不可用/)).length).toBeGreaterThan(0);
    await waitFor(() => expect(listAgentEvalConversationCandidates).toHaveBeenCalledTimes(2));
  });

  describe("held-out cases and suite review", () => {
    const LOOP = `aeal_${"c".repeat(32)}`;
    const aligned = { id: "p_00aa", kind: "judgment" as const, text: "States the period", source: "coach_alignment" as const, state: "confirmed" as const, origin: { by: "u_owner", at: "2026-10-19T10:00:00Z", loop_id: LOOP } };
    const ownerCase = (key: string, task: string, heldOut: boolean) => ({
      case_key: key,
      case_revision_id: `aec_${key}`,
      input: { task },
      fixture_ref: {},
      expected_contract: { allowed_terminal_statuses: ["completed"], points: [aligned], ...(heldOut ? { held_out: true } : {}) },
      scorer_config: {},
      effect_policy: { mode: "no_effects" },
    });
    const withOwnerSuite = (): AgentEvalManager => ({
      ...MANAGER,
      suites: [...MANAGER.suites, {
        ...MANAGER.suites[0],
        suite_id: "aes_owner",
        suite_kind: "owner_quality",
        name: "My checks",
        platform_managed: false,
        revision: {
          suite_revision_id: "aev_owner",
          sequence: 2,
          case_count: 2,
          created_at: "2026-10-19T00:00:00Z",
          cases: [ownerCase("align.p_00aa", "East China September sales", false), ownerCase("quality.2", "South China last quarter margin", true)],
        },
      }],
    });

    it("keeps only an aligned case's key and renumbers every other case, including drafted ones", () => {
      const base = { task: "x", fixture_ref: {}, expected_contract: {}, scorer_config: {}, effect_policy: {} };
      expect(savedCaseBody({ ...base, case_key: "align.p_00aa" }, 0).case_key).toBe("align.p_00aa");
      expect(savedCaseBody({ ...base, case_key: "quality.7" }, 2).case_key).toBe("quality.3");
      // Drafting from conversations twice yields conversation.1 twice; saving must not send duplicates.
      expect([savedCaseBody({ ...base, case_key: "conversation.1" }, 1), savedCaseBody({ ...base, case_key: "conversation.1" }, 2)].map((item) => item.case_key))
        .toEqual(["quality.2", "quality.3"]);
    });

    it("keeps aligned and held-out cases when AI suggestions replace the hand-written ones", async () => {
      const suite = withOwnerSuite();
      const revision = suite.suites[1].revision!;
      revision.cases = [...revision.cases, ownerCase("quality.3", "A hand-written case", false)];
      vi.mocked(getAgentEvalManager).mockResolvedValue(suite);
      vi.mocked(suggestAgentEvalCases).mockResolvedValue({ kind: "agent_eval_case_suggestions", schema_version: 1, cases: [{
        case_key: "quality.1", task: "Suggested question", fixture_ref: {}, expected_contract: {}, scorer_config: {}, effect_policy: {},
      }] });
      vi.mocked(saveAgentEvalSuite).mockResolvedValue(suite);
      renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />, { locale: "en-US" });
      fireEvent.click(await screen.findByRole("button", { name: "Suggest test cases with AI" }));
      fireEvent.click(screen.getByRole("button", { name: "Suggest" }));
      expect(await screen.findByDisplayValue("Suggested question")).toBeTruthy();
      expect(screen.queryByText("A hand-written case")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Save tests" }));
      await waitFor(() => expect(saveAgentEvalSuite).toHaveBeenCalledTimes(1));
      const saved = vi.mocked(saveAgentEvalSuite).mock.calls[0][1].cases;
      expect(saved.map((item) => [item.case_key, item.task, item.expected_contract.held_out])).toEqual([
        ["align.p_00aa", "East China September sales", undefined],
        ["quality.2", "South China last quarter margin", true],
        ["quality.3", "Suggested question", undefined],
      ]);
    });

    it("moves a point's held-out case and saves the flag with the suite", async () => {
      vi.mocked(getAgentEvalManager).mockResolvedValue(withOwnerSuite());
      vi.mocked(saveAgentEvalSuite).mockResolvedValue(withOwnerSuite());
      renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />, { locale: "en-US" });
      expect((await screen.findByTestId("held-out-1")).getAttribute("aria-checked")).toBe("true");
      const first = screen.getByTestId("held-out-0");
      expect(first.getAttribute("aria-checked")).toBe("false");
      // One line under each switch; the details sit behind the question mark (decision 21).
      expect(screen.getAllByText("Coach can't see this case, so it checks whether a change really helped.")).toHaveLength(2);
      fireEvent.click(screen.getAllByRole("button", { name: "About held out" })[0]);
      const help = await screen.findByRole("dialog", { name: "What “held out” means" });
      expect(help.textContent).toContain("Release checks always run every case, held-out ones included.");
      fireEvent.keyDown(document, { key: "Escape" });
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "What “held out” means" })).toBeNull());

      fireEvent.click(first);
      expect(screen.getByTestId("held-out-0").getAttribute("aria-checked")).toBe("true");
      expect(screen.getByTestId("held-out-1").getAttribute("aria-checked")).toBe("false");
      fireEvent.click(screen.getByRole("button", { name: "Save tests" }));
      await waitFor(() => expect(saveAgentEvalSuite).toHaveBeenCalledTimes(1));
      const saved = vi.mocked(saveAgentEvalSuite).mock.calls[0][1].cases;
      expect(saved.map((item) => [item.case_key, item.expected_contract.held_out])).toEqual([["align.p_00aa", true], ["quality.2", undefined]]);
    });

    it("asks before turning off a point's only held-out case", async () => {
      vi.mocked(getAgentEvalManager).mockResolvedValue(withOwnerSuite());
      renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />, { locale: "en-US" });
      fireEvent.click(await screen.findByTestId("held-out-1"));
      expect(await screen.findByText("Turn off this point’s only held-out case?")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(screen.getByTestId("held-out-1").getAttribute("aria-checked")).toBe("true"));
      fireEvent.click(screen.getByTestId("held-out-1"));
      fireEvent.click(await screen.findByRole("button", { name: "Turn off" }));
      await waitFor(() => expect(screen.getByTestId("held-out-1").getAttribute("aria-checked")).toBe("false"));
    });

    it("with the alignment switch off hides held-out controls, origins and suite review but keeps held-out on save", async () => {
      setAgentEvalAlignmentCapabilityForTest(false);
      vi.mocked(getAgentEvalManager).mockResolvedValue(withOwnerSuite());
      vi.mocked(saveAgentEvalSuite).mockResolvedValue(withOwnerSuite());
      renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />, { locale: "en-US" });
      expect(await screen.findByText("East China September sales")).toBeTruthy();
      expect(screen.queryByTestId("held-out-0")).toBeNull();
      expect(screen.queryByTestId("held-out-1")).toBeNull();
      expect(screen.queryByText("Coach can't see this case, so it checks whether a change really helped.")).toBeNull();
      expect(screen.queryByText(/Coach reviewed this suite/)).toBeNull();
      expect(reviewAgentEvalSuite).not.toHaveBeenCalled();
      expect(screen.queryByText(/From what you told Coach/)).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Save tests" }));
      await waitFor(() => expect(saveAgentEvalSuite).toHaveBeenCalledTimes(1));
      const saved = vi.mocked(saveAgentEvalSuite).mock.calls[0][1].cases;
      // Stored held-out values go back unchanged: hidden, never stripped.
      expect(saved.map((item) => [item.case_key, item.expected_contract.held_out])).toEqual([["align.p_00aa", undefined], ["quality.2", true]]);
    });

    it("shows a duplicate pair side by side and removes one case through the suite save", async () => {
      vi.mocked(getAgentEvalManager).mockResolvedValue(withOwnerSuite());
      vi.mocked(saveAgentEvalSuite).mockResolvedValue(withOwnerSuite());
      vi.mocked(reviewAgentEvalSuite).mockResolvedValueOnce({ status: "reviewed", gaps: [], duplicate_cases: [["align.p_00aa", "quality.2"]] });
      renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />, { locale: "en-US" });
      fireEvent.click(await screen.findByRole("button", { name: "View" }));
      fireEvent.click(screen.getByRole("button", { name: "View side by side" }));
      const compare = screen.getByTestId("suite-review-compare");
      expect(within(compare).getByText("East China September sales")).toBeTruthy();
      expect(within(compare).getByText("South China last quarter margin")).toBeTruthy();
      fireEvent.click(within(compare).getByRole("button", { name: "Remove case 2" }));
      expect(screen.queryByTestId("suite-review-compare")).toBeNull();
      expect(screen.getAllByText("Case removed from the suite you’re editing. Save the suite to apply it.").length).toBeGreaterThan(0);
      fireEvent.click(screen.getByRole("button", { name: "Save tests" }));
      await waitFor(() => expect(saveAgentEvalSuite).toHaveBeenCalledTimes(1));
      expect(vi.mocked(saveAgentEvalSuite).mock.calls[0][1].cases.map((item) => item.case_key)).toEqual(["align.p_00aa"]);
    });

    it("keeps both cases of a duplicate pair when asked", async () => {
      vi.mocked(getAgentEvalManager).mockResolvedValue(withOwnerSuite());
      vi.mocked(reviewAgentEvalSuite).mockResolvedValueOnce({ status: "reviewed", gaps: [], duplicate_cases: [["align.p_00aa", "quality.2"]] });
      renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="arc_1" />, { locale: "en-US" });
      fireEvent.click(await screen.findByRole("button", { name: "View" }));
      fireEvent.click(screen.getByRole("button", { name: "View side by side" }));
      fireEvent.click(screen.getByRole("button", { name: "Keep both" }));
      expect(screen.queryByText("Cases 1 and 2 ask the same thing.")).toBeNull();
      expect(screen.getByTestId("case-2")).toBeTruthy();
    });

    it("shows Coach's suite review as one line and stays silent when unavailable", async () => {
      vi.mocked(getAgentEvalManager).mockResolvedValue(withOwnerSuite());
      vi.mocked(reviewAgentEvalSuite).mockResolvedValueOnce({
        status: "reviewed",
        gaps: [{ text: "No case checks what it says when data is missing.", suggested_task: "What were sales in a region with no data?" }],
        duplicate_cases: [["align.p_00aa", "quality.2"]],
      });
      const onAskCoach = vi.fn();
      renderIntl(<AgentEvalPanel agentId="osa_1" agentName="Growth analyst" archived={false} refreshToken="arc_1" onAskCoach={onAskCoach} />, { locale: "en-US" });
      expect(await screen.findByText("Coach reviewed this suite: 2 suggestions")).toBeTruthy();
      expect(reviewAgentEvalSuite).toHaveBeenCalledWith("osa_1", "aes_owner", "aev_owner", expect.anything());
      fireEvent.click(screen.getByRole("button", { name: "View" }));
      expect(screen.getByText("Cases 1 and 2 ask the same thing.")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Ask Coach to draft a case" }));
      expect(onAskCoach).toHaveBeenCalledWith("Draft an evaluation case for the Agent “Growth analyst” that covers: What were sales in a region with no data?");
      fireEvent.click(screen.getAllByRole("button", { name: "Dismiss" })[1]);
      expect(screen.queryByText("Cases 1 and 2 ask the same thing.")).toBeNull();
    });
  });
});


describe("unsavedCaseChanges (the case editor's save bar)", () => {
  const saved = [
    { case_key: "quality.1", task: "East sales", fixture_ref: {}, expected_contract: { points: [] }, scorer_config: {}, effect_policy: {}, valid_until: null },
    { case_key: "quality.2", task: "West sales", fixture_ref: {}, expected_contract: { points: [] }, scorer_config: {}, effect_policy: {}, valid_until: null },
  ];

  it("counts edited, added and removed cases, and ignores a blank new case", () => {
    const same = saved.map((item) => ({ ...item }));
    expect(unsavedCaseChanges(same, saved)).toBe(0);
    expect(unsavedCaseChanges([...same, { ...same[0], case_key: "quality.3", task: "" }], saved)).toBe(0);
    expect(unsavedCaseChanges([{ ...same[0], task: "East sales by month" }, same[1]], saved)).toBe(1);
    expect(unsavedCaseChanges([same[0]], saved)).toBe(1);
    expect(unsavedCaseChanges([...same, { ...same[0], case_key: "quality.3", task: "North sales" }], saved)).toBe(1);
    expect(unsavedCaseChanges([{ ...same[0], expected_contract: { points: [{ id: "p_1" }] } }, same[1]] as never, saved)).toBe(1);
  });
});


describe("saved-suite execution and honest answer criteria", () => {
  const point = { id: "p_ab12", kind: "judgment", text: "The total is 300", source: "owner", state: "confirmed" };
  const savedCase = { case_key: "quality.1", task: "Add 100 and 200", expected_contract: { points: [point] }, fixture_ref: {}, scorer_config: {}, effect_policy: { mode: "no_effects" }, valid_until: null };
  const owner = { ...MANAGER.suites[0], suite_id: "aes_owner", suite_kind: "owner_quality" as const, name: "Business quality examples", platform_managed: false,
    revision: { ...MANAGER.suites[0].revision!, suite_revision_id: "aev_saved", cases: [savedCase] } };
  const savedManager = { ...MANAGER, suites: [owner] };
  const finished = { kind: "agent_eval_run" as const, schema_version: 1 as const, run_id: "aer_finished", suite_revision_id: "aev_old", target_kind: "live" as const,
    revision_id: "arv_1", status: "passed" as const, trigger_kind: "manual", summary: { case_count: 1, "pass@k_cases": 1 },
    quality: { state: "ungraded" as const, met: 0, unmet: 0, ungraded: 1, point_assessments: 1 },
    results: [{ case_revision_id: "aec_old", case_key: "quality.1", task: "Historical total question", attempt: 1, status: "passed", deterministic: {}, judge: null, duration_ms: 10, cost_microusd: null, failure_code: null }] };
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAgentEvalManager).mockResolvedValue(MANAGER);
    vi.mocked(saveAgentEvalSuite).mockResolvedValue(savedManager);
    vi.mocked(runAgentEval).mockResolvedValue(finished);
    setAgentEvalAlignmentCapabilityForTest(false);
  });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it("waits for the exact saved revision and admits only one pending submission", async () => {
    let resolveSave!: (value: AgentEvalManager) => void;
    vi.mocked(saveAgentEvalSuite).mockImplementation(() => new Promise((resolve) => { resolveSave = resolve; }));
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="1" />, { locale: "en-US" });
    fireEvent.change(await screen.findByLabelText("Test case 1"), { target: { value: savedCase.task } });
    const action = screen.getByRole("button", { name: "Save and run" });
    fireEvent.click(action); fireEvent.click(action);
    expect(saveAgentEvalSuite).toHaveBeenCalledTimes(1);
    expect(runAgentEval).not.toHaveBeenCalled();
    resolveSave(savedManager);
    await waitFor(() => expect(runAgentEval).toHaveBeenCalledWith("osa_1", expect.objectContaining({ suite_revision_id: "aev_saved", target: "candidate", expected_revision_id: "arv_2" }), expect.any(String)));
  });

  it("keeps the edited question and starts nothing when saving fails", async () => {
    vi.mocked(saveAgentEvalSuite).mockRejectedValue(new Error("Save failed"));
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="1" />, { locale: "en-US" });
    const field = await screen.findByLabelText("Test case 1");
    fireEvent.change(field, { target: { value: savedCase.task } });
    fireEvent.click(screen.getByRole("button", { name: "Save and run" }));
    await screen.findAllByText("The quality suite could not be saved.");
    expect((field as HTMLTextAreaElement).value).toBe(savedCase.task);
    expect(runAgentEval).not.toHaveBeenCalled();
  });

  it("recovers a lost start response with the same key and saved suite", async () => {
    vi.mocked(getAgentEvalManager).mockResolvedValue(savedManager);
    vi.mocked(runAgentEval).mockRejectedValueOnce(new TypeError("Network disconnected")).mockResolvedValueOnce(finished);
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="1" />, { locale: "en-US" });
    await screen.findByText(savedCase.task);
    fireEvent.click(screen.getByRole("button", { name: "Run this suite" }));
    await screen.findAllByText(/Cases saved; the run could not be confirmed/);
    fireEvent.click(screen.getByRole("button", { name: "Run this suite" }));
    await waitFor(() => expect(runAgentEval).toHaveBeenCalledTimes(2));
    expect(vi.mocked(runAgentEval).mock.calls[0]).toEqual(vi.mocked(runAgentEval).mock.calls[1]);
    expect(saveAgentEvalSuite).not.toHaveBeenCalled();
  });

  it("keeps an unmet advisory criterion visible beside a passing execution", async () => {
    const run = { ...finished, quality: { state: "graded" as const, met: 0, unmet: 1, ungraded: 0, point_assessments: 1 } };
    vi.mocked(getAgentEvalManager).mockResolvedValue({ ...savedManager, suites: [{ ...owner, evidence: { state: "valid", stale_reason: null, updated_at: null, run } }], release_gate: { required: false, ready: true, reason: null } });
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="1" privateAgent />, { locale: "en-US" });
    expect(await screen.findByText(/Answer criteria: 0 met, 1 unmet/)).toBeTruthy();
    expect(screen.getByText(/These judgments are advisory/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Run this suite" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("discards a late manager response after selecting another Agent", async () => {
    let resolveFirst!: (value: AgentEvalManager) => void;
    vi.mocked(getAgentEvalManager).mockImplementation((id) => id === "osa_1" ? new Promise((resolve) => { resolveFirst = resolve; }) : Promise.resolve({ ...savedManager, agent_id: "osa_2", suites: [{ ...owner, name: "Second Agent examples" }] }));
    function Selection() {
      const [id, setId] = useState("osa_1");
      return <><button onClick={() => setId("osa_2")}>Select second Agent</button><AgentEvalPanel agentId={id} archived={false} refreshToken="1" /></>;
    }
    renderIntl(<Selection />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Select second Agent" }));
    await screen.findAllByText(/Second Agent examples/);
    await act(async () => { resolveFirst(savedManager); });
    expect(screen.queryByText(/Business quality examples/)).toBeNull();
    expect(screen.getAllByText(/Second Agent examples/).length).toBeGreaterThan(0);
  });

  it("shows ungraded execution passes and lazily reads the historical case answer", async () => {
    vi.mocked(getAgentEvalManager).mockResolvedValue({ ...savedManager, suites: [{ ...owner, evidence: { state: "valid", stale_reason: null, updated_at: null, run: finished } }], release_gate: { required: false, ready: true, reason: null } });
    vi.mocked(getAgentEvalRun).mockResolvedValue(finished);
    vi.mocked(getAgentEvalRunCaseAnswers).mockResolvedValue({ run_id: finished.run_id, revision_id: "arv_1", suite_revision_id: "aev_old", case_revision_id: "aec_old", case_key: "quality.1", task: "Historical total question", points: [], attempts: [{ attempt: 1, status: "passed", duration_ms: 10, failure_code: null, answer: "Total: 999", answer_truncated: false, quality: finished.quality }] });
    renderIntl(<AgentEvalPanel agentId="osa_1" archived={false} refreshToken="1" />, { locale: "en-US" });
    expect(await screen.findByText(/Answer criteria: not graded/)).toBeTruthy();
    expect(getAgentEvalRunCaseAnswers).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "View answers and criteria" }));
    const question = await screen.findByText("Case 1: Historical total question");
    expect(getAgentEvalRunCaseAnswers).not.toHaveBeenCalled();
    const details = question.closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    expect(await screen.findByText("Total: 999")).toBeTruthy();
    expect(getAgentEvalRunCaseAnswers).toHaveBeenCalledWith("osa_1", "aer_finished", "aec_old");
  });
});
