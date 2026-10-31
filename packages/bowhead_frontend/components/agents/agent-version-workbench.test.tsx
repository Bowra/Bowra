import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState, type ComponentProps } from "react";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError, readApiError } from "@/lib/api-client";
import {
  acceptAgentProposalSuggestedCases,
  applyAgentChangeProposal,
  cancelAgentVersionComparison,
  completeAgentVersionRelease,
  dismissAgentChangeProposal,
  getAgentComparisonEstimate,
  getAgentEvalManager,
  getAgentEvalRun,
  getAgentVersionCaseAnswers,
  getAgentVersionCaseChanges,
  getAgentVersionWorkbench,
  releasePrivateAgentDraft,
  testAgentChange,
  withdrawAgentVersionCandidate,
  type AgentChangeProposal,
  type AgentEvalProgress,
  type AgentVersionCaseChanges,
  type AgentVersionWorkbench,
} from "@/lib/agents-api";
import { appNavigationAllowed } from "@/lib/use-app-router";
import { toast } from "@/lib/use-toast";
import { AgentVersionWorkbenchPanel, deferDismissal } from "./agent-version-workbench";
import { pendingProposals } from "@/lib/agent-draft-changes";
import { resetCiMerges } from "@/lib/agent-ci-merges";

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ activeId: "ws_1", items: [{ id: "ws_1", name: "华东经营分析" }], loading: false, error: null, setActiveId: vi.fn(), refresh: vi.fn() }),
}));

vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));

const modelAccess = vi.hoisted(() => ({ canConfigure: false }));
vi.mock("@/lib/use-can-configure-models", () => ({
  JUDGE_MODEL_SETTINGS_PATH: "/team/settings/models/assignments/",
  useCanConfigureModels: () => modelAccess.canConfigure,
}));

vi.mock("@/lib/coach-ci-api", () => ({ listCoachCI: vi.fn(async () => { throw Object.assign(new Error("off"), { status: 404 }); }) }));

vi.mock("@/lib/agents-api", () => ({
  acceptAgentProposalSuggestedCases: vi.fn(),
  applyAgentChangeProposal: vi.fn(),
  cancelAgentVersionComparison: vi.fn(),
  completeAgentVersionRelease: vi.fn(),
  createAgentCoachProposal: vi.fn(),
  createAgentVersionOverride: vi.fn(),
  dismissAgentChangeProposal: vi.fn(),
  dismissAgentProposalSuggestedCases: vi.fn(),
  getAgentComparisonEstimate: vi.fn(),
  getAgentEvalManager: vi.fn(),
  getAgentEvalRun: vi.fn(),
  getAgentVersionCaseAnswers: vi.fn(),
  getAgentVersionCaseChanges: vi.fn(),
  getAgentVersionWorkbench: vi.fn(),
  releasePrivateAgentDraft: vi.fn(),
  testAgentChange: vi.fn(),
  withdrawAgentVersionCandidate: vi.fn(),
}));

const MONEY = /[¥$€]|USD|credit|额度|费用|金额|Kosten|coût/i;

const WORKBENCH: AgentVersionWorkbench = {
  schema_version: 1,
  current_eval_policy_revision_id: "aepr_current",
  live_identity: { release_id: "arl_1", release_sequence: 3, revision_id: "arv_live" },
  candidate_identity: {
    audience_mode: "private",
    candidate_hash: `sha256:${"1".repeat(64)}`,
    candidate_id: "arc_1",
    revision_id: "arv_candidate",
    row_version: 2,
    state: "prepared",
  },
  next_draft_identity: null,
  active_plan_identity: { id: "aep_1", hash: `sha256:${"2".repeat(64)}` },
  active_snapshot_identity: null,
  active_override_identity: null,
  active_execution_summary: null,
  control_row_version: 4,
  display_state: "prepared",
  draft_rebase_state: "current",
  readiness: "missing",
  reason_codes: [],
  stale_subject: null,
  primary_action: { kind: "run_release_comparison", enabled: true, disabled_reason_codes: [] },
  secondary_actions: [{ kind: "withdraw_candidate", enabled: true }],
  change_proposals: [],
};

const PROGRESS: AgentEvalProgress = {
  schema_version: 1,
  cases: { completed: 17, total: 24 },
  attempts: { completed: 51, total: 72, in_flight: 2 },
  model_calls: { used: 63, basis: "successful_calls", limit: null, estimated_total: 104 },
  usage: { tokens: 9800, tokens_input: 7000, tokens_output: 2800, tokens_cached: 0, estimated_total_tokens: 15000, settled_only: true, partial: false },
  elapsed_ms: 252_000,
  eta_ms: { low: 90_000, high: 150_000 },
  as_of: "2026-10-05T06:18:12Z",
};

const COMPLETED_EXECUTION = {
  can_cancel: false, execution_id: "aex_1", failure_code: null, finished_at: "2026-10-05T06:26:00Z",
  row_version: 3, started_at: "2026-10-05T06:18:00Z", status: "completed", progress: { ...PROGRESS, cases: { completed: 24, total: 24 }, model_calls: { ...PROGRESS.model_calls, used: 104 } },
};

function renderPanel(props: Partial<ComponentProps<typeof AgentVersionWorkbenchPanel>> = {}, locale: "zh-CN" | "en-US" = "zh-CN") {
  return renderIntl(<AgentVersionWorkbenchPanel
    agentName="Revenue Analyst"
    agentId="osa_1"
    view="validation"
    workingCopy={null}
    onAvailability={vi.fn()}
    onChanged={vi.fn().mockResolvedValue(undefined)}
    onError={vi.fn()}
    {...props}
  />, { locale });
}



describe("AgentVersionWorkbenchPanel · release check", () => {
  beforeEach(() => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(WORKBENCH);
    vi.mocked(getAgentComparisonEstimate).mockRejectedValue(new ApiError(404, "disabled"));
    vi.mocked(getAgentVersionCaseChanges).mockRejectedValue(new ApiError(404, "disabled"));
    vi.mocked(getAgentEvalManager).mockResolvedValue({
      kind: "agent_eval_manager",
      schema_version: 1,
      agent_id: "osa_1",
      target: { kind: "live" },
      suites: [{
        suite_id: "aes_1",
        suite_kind: "owner_quality",
        name: "季度经营问答",
        visibility: "owner",
        state: "active",
        row_version: 2,
        platform_managed: false,
        revision: { suite_revision_id: "aev_1", sequence: 1, case_count: 24, created_at: "2026-09-14T08:00:00Z", cases: [] },
        evidence: { state: "missing", stale_reason: null, updated_at: null, run: null },
      }],
      runs: [],
      trigger_policy: null,
      release_gate: { required: true, ready: false, reason: "missing" },
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  /** A saved draft not tested yet: no candidate, the server asks to prepare (as the workbench reports it). */
  const UNTESTED: AgentVersionWorkbench = {
    ...WORKBENCH,
    candidate_identity: null,
    active_plan_identity: null,
    next_draft_identity: { base_release_id: "arl_1", id: "awc_1", row_version: 3 },
    primary_action: { kind: "prepare_candidate", enabled: true, disabled_reason_codes: [] },
    secondary_actions: [],
  };
  const READY: AgentVersionWorkbench = {
    ...WORKBENCH,
    display_state: "ready",
    readiness: "ready",
    active_snapshot_identity: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
    primary_action: { kind: "activate_candidate", enabled: true, disabled_reason_codes: [] },
  };
  const SHARED_READY: AgentVersionWorkbench = {
    ...READY,
    candidate_identity: { ...WORKBENCH.candidate_identity!, audience_mode: "teamwide" },
    primary_action: { kind: "submit_for_review", enabled: true, disabled_reason_codes: [] },
  };
  const RUNNING: AgentVersionWorkbench = {
    ...WORKBENCH,
    display_state: "comparison_running",
    active_execution_summary: { can_cancel: true, execution_id: "aex_1", failure_code: null, finished_at: null, row_version: 1, started_at: "2026-10-05T06:18:00Z", status: "running", progress: PROGRESS },
    primary_action: { kind: "view_comparison", enabled: true, disabled_reason_codes: [] },
  };

  it("tests a saved draft with one press and shows only the released version's number", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValueOnce(UNTESTED).mockResolvedValue(RUNNING);
    vi.mocked(testAgentChange).mockResolvedValue({ kind: "agent_version_test", schema_version: 1, step: "started", preparation: null, comparison: null });
    const onChanged = vi.fn().mockResolvedValue(undefined);
    const view = renderPanel({ onChanged, workingCopy: { id: "awc_1", row_version: 3, updated_at: "2026-10-05T03:20:00Z" } as never });

    const card = (await screen.findByRole("heading", { name: "你的修改还没测试" })).closest("section")!;
    expect(card.textContent).toContain("测试会用你最新保存的草稿，和已发布的 v3 跑同一批用例。");
    const rail = screen.getByRole("list", { name: "版本" });
    expect(within(rail).getByText("已发布")).toBeTruthy();
    expect(within(rail).getByText("v3")).toBeTruthy();
    expect(within(rail).getByText("这次修改")).toBeTruthy();
    expect(within(rail).getAllByText("还没测试").length).toBeGreaterThan(0);
    // Only the released version has a number, and the Owner never sees the candidate.
    expect(view.container.textContent).not.toMatch(/v4|候选|arv_|arc_|aep_|sha256/);
    expect(screen.queryByRole("textbox", { name: "发布说明（选填）" })).toBeNull();
    expect(view.container.querySelectorAll(".am-ci-test .settings-button--primary")).toHaveLength(1);

    fireEvent.click(within(card).getByRole("button", { name: "测试这次修改" }));
    await waitFor(() => expect(testAgentChange).toHaveBeenCalledWith("osa_1", "ws_1", expect.any(String)));
    expect(onChanged).toHaveBeenCalledOnce();
    // Straight from the press to the running test: no prepared-but-untested step in between.
    expect(await screen.findByRole("heading", { name: "正在运行用例 17/24" })).toBeTruthy();
  });

  it("says unsaved Configure edits are left out of the test", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(UNTESTED);
    renderPanel({ editorDirty: true }, "en-US");
    expect(await screen.findByText("Unsaved edits on Configure won't be included in this test.")).toBeTruthy();
  });

  it("lands a refused start in a could-not-run card that names the condition and the fix", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(UNTESTED);
    vi.mocked(testAgentChange)
      .mockRejectedValueOnce(new ApiError(503, "unavailable", { code: "agent_eval_model_config_unavailable" }))
      .mockResolvedValue({ kind: "agent_version_test", schema_version: 1, step: "started", preparation: null, comparison: null });
    const view = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "测试这次修改" }));
    const card = (await screen.findByRole("heading", { name: "测试没能开始" })).closest("section")!;
    expect(card.textContent).toContain("评估模型暂时不可用。请团队管理员在 团队设置 › 模型 检查评估模型，修好后重新测试。");
    expect(view.container.textContent).not.toMatch(/agent_eval_model_config_unavailable/);
    expect(within(card).getByRole("button", { name: "复制诊断信息" })).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: "重新测试" }));
    await waitFor(() => expect(testAgentChange).toHaveBeenCalledTimes(2));
    // Each press is its own request; the server finishes whatever the first one left.
    expect(vi.mocked(testAgentChange).mock.calls[0][2]).not.toBe(vi.mocked(testAgentChange).mock.calls[1][2]);
  });

  it("refuses a test without a usable model with the role, and links configurers to the model assignments", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(UNTESTED);
    vi.mocked(testAgentChange).mockRejectedValue(new ApiError(422, "no model", {
      code: "agent_eval_model_unavailable",
      details: { model_role: "analyst", model_reason: "role_unassigned" },
    }));
    modelAccess.canConfigure = true;
    try {
      renderPanel();
      fireEvent.click(await screen.findByRole("button", { name: "测试这次修改" }));
      const card = (await screen.findByRole("heading", { name: "评测用不了模型" })).closest("section")!;
      expect(card.textContent).toContain("「主模型」没有设置可用的模型。在 团队设置 › 模型 › 模型分工 设置后再测试。");
      expect(within(card).getByRole("link", { name: "打开模型设置" }).getAttribute("href")).toBe("/team/settings/models/assignments/");
      // Testing again gives the same refusal until the model is set, so there is no plain retry.
      expect(within(card).queryByRole("button", { name: "重新测试" })).toBeNull();
      expect(within(card).getByRole("button", { name: "修好后重新测试" })).toBeTruthy();
    } finally {
      modelAccess.canConfigure = false;
    }
  });

  it("tells everyone else who can set the missing model", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(UNTESTED);
    vi.mocked(testAgentChange).mockRejectedValue(new ApiError(422, "no model", {
      code: "agent_eval_model_unavailable",
      details: { model_role: "judge", model_reason: "unconfigured" },
    }));
    renderPanel({}, "en-US");
    fireEvent.click(await screen.findByRole("button", { name: "Test this change" }));
    const card = (await screen.findByRole("heading", { name: "The evaluation can't use a model" })).closest("section")!;
    expect(card.textContent).toContain("The team has no usable model for “Evaluation model”, so the test can't run. Ask a team administrator");
    expect(within(card).queryByRole("link", { name: "Open Models settings" })).toBeNull();
  });

  it("names each cause of attempts that stopped, with the cases, the fix and the diagnostic reference", async () => {
    const changes: AgentVersionCaseChanges = {
      kind: "agent_version_case_changes", schema_version: 1, execution_id: "aex_1",
      snapshot: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      baseline: { release_id: "arl_1", release_sequence: 3 }, candidate: { candidate_id: "arc_1", release_sequence: null },
      summary: { case_count: 2, worse: 0, unstable: 0, better: 0, not_comparable: 2, unchanged_required_failures: 0 },
      items: [],
      next_offset: null,
      unfinished: [
        { suite_revision_id: "aev_1", suite_name: "季度经营问答", case_revision_id: "aec_a", case_key: "quality.1", case_label: "渠道毛利率排名", reason_codes: ["attempt_agent_model_rate_limited"], tools: [], failures: [{ category: "agent_model", cause: "rate_limited", tool: null, reason_code: "attempt_agent_model_rate_limited", references: ["aej_rate"] }] },
        { suite_revision_id: "aev_1", suite_name: "季度经营问答", case_revision_id: "aec_b", case_key: "platform.safe_contract", case_label: "说明你能做什么", reason_codes: ["attempt_infrastructure_incomplete"], tools: [], failures: [{ category: "platform", cause: null, tool: null, reason_code: "attempt_infrastructure_incomplete", references: ["aej_platform_1"] }] },
      ],
    };
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue(changes);
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "unknown",
      readiness: "unknown",
      reason_codes: ["attempt_agent_model_rate_limited", "attempt_infrastructure_incomplete"],
      primary_action: { kind: "retry_after_environment_fix", enabled: true, disabled_reason_codes: [] },
      active_snapshot_identity: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z", execution_id: "aex_1" },
    } as AgentVersionWorkbench);
    renderPanel();
    const card = (await screen.findByRole("heading", { name: "测试没能完成" })).closest("section")!;
    await waitFor(() => expect(card.textContent).toContain("受影响的用例：「渠道毛利率排名」。等几分钟再重新测试"));
    expect(card.textContent).toContain("Agent 使用的模型服务限制了调用频率");
    expect(card.textContent).toContain("部分用例运行因平台错误中断");
    expect(card.textContent).toContain("把诊断编号 aej_platform_1 发给团队管理员");
    expect(card.textContent).not.toMatch(/环境问题|attempt_infrastructure_incomplete/);
    // Waiting out a rate limit and an unclassified error can both clear, so testing again is offered.
    expect((within(card).getByRole("button", { name: "重新测试" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("offers the model settings instead of a retry when the Agent's model rejects its credentials", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "unknown",
      readiness: "unknown",
      reason_codes: ["attempt_agent_model_auth_failed"],
      primary_action: { kind: "retry_after_environment_fix", enabled: true, disabled_reason_codes: [] },
    } as AgentVersionWorkbench);
    modelAccess.canConfigure = true;
    try {
      renderPanel();
      const card = (await screen.findByRole("heading", { name: "测试没能完成" })).closest("section")!;
      expect(card.textContent).toContain("Agent 使用的模型服务拒绝了访问凭据");
      expect(within(card).getAllByRole("link", { name: "打开模型设置" }).length).toBeGreaterThan(0);
      expect(within(card).queryByRole("button", { name: "重新测试" })).toBeNull();
      expect(within(card).getByRole("button", { name: "修好后重新测试" })).toBeTruthy();
    } finally {
      modelAccess.canConfigure = false;
    }
  });

  it("offers the Data Sources settings instead of a retry when the Team has no Datarc connection", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "unknown",
      readiness: "unknown",
      reason_codes: ["attempt_datarc_unavailable_no_connection"],
      primary_action: { kind: "retry_after_environment_fix", enabled: true, disabled_reason_codes: [] },
    } as AgentVersionWorkbench);
    modelAccess.canConfigure = true;
    try {
      renderPanel({}, "en-US");
      const card = (await screen.findByRole("heading", { name: "The test couldn't finish" })).closest("section")!;
      expect(card.textContent).toContain("The Team has no Datarc connection");
      const link = within(card).getByRole("link", { name: "Open Data Sources settings" }) as HTMLAnchorElement;
      expect(link.getAttribute("href")).toMatch(/\/team\/settings\/datasource\/$/);
      expect(within(card).queryByRole("button", { name: "Test again" })).toBeNull();
    } finally {
      modelAccess.canConfigure = false;
    }
  });

  it("offers Test again when Datarc was only busy", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "unknown",
      readiness: "unknown",
      reason_codes: ["attempt_datarc_unavailable_datarc_busy"],
      primary_action: { kind: "retry_after_environment_fix", enabled: true, disabled_reason_codes: [] },
    } as AgentVersionWorkbench);
    renderPanel({}, "en-US");
    const card = (await screen.findByRole("heading", { name: "The test couldn't finish" })).closest("section")!;
    expect(card.textContent).toContain("Datarc was too busy to answer");
    expect((within(card).getByRole("button", { name: "Test again" }) as HTMLButtonElement).disabled).toBe(false);
    expect(within(card).queryByRole("link", { name: "Open Data Sources settings" })).toBeNull();
  });

  it("names an expired test case when the plan refresh can't include it", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(UNTESTED);
    vi.mocked(testAgentChange).mockRejectedValue(new ApiError(422, "expired", {
      code: "agent_eval_fixture_expired",
      details: { case_key: "quality.7", suite_revision_id: "aev_gone", valid_until: "2026-10-01T00:00:00Z" },
    }));
    renderPanel({}, "en-US");
    fireEvent.click(await screen.findByRole("button", { name: "Test this change" }));
    const card = (await screen.findByRole("heading", { name: "The test couldn't start" })).closest("section")!;
    expect(card.textContent).toMatch(/The test case “quality\.7” expired on .+, so the test plan can’t include it\. On the Evaluate tab, remove it from its suite/);
  });

  it("offers only Test again when the draft changed after the test", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...READY,
      draft_ahead_of_candidate: true,
      secondary_actions: [{ kind: "prepare_from_latest_draft", enabled: true }, { kind: "withdraw_candidate", enabled: true }],
    });
    vi.mocked(testAgentChange).mockResolvedValue({ kind: "agent_version_test", schema_version: 1, step: "started", preparation: null, comparison: null });
    const view = renderPanel({}, "en-US");

    const card = (await screen.findByRole("heading", { name: "The draft changed after this test" })).closest("section")!;
    expect(within(card).getAllByRole("button").map((button) => button.getAttribute("aria-label") ?? button.textContent)).toEqual(["Test again", "More actions"]);
    expect(screen.queryByRole("button", { name: /Release as/ })).toBeNull();
    expect(view.container.textContent).not.toMatch(/Prepare from latest draft|candidate/i);
    fireEvent.click(within(card).getByRole("button", { name: "Test again" }));
    await waitFor(() => expect(testAgentChange).toHaveBeenCalledOnce());
  });

  it("says the released version moved when the test was on an older one", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      live_identity: { release_id: "arl_2", release_sequence: 4, revision_id: "arv_live_2" },
      candidate_identity: { ...WORKBENCH.candidate_identity!, base_release_id: "arl_1", release_sequence_if_activated: 5 },
      display_state: "candidate_stale",
      readiness: "ready",
      reason_codes: ["base_release_changed"],
      primary_action: { kind: "prepare_candidate", enabled: true, disabled_reason_codes: [] },
    });
    renderPanel();
    const card = (await screen.findByRole("heading", { name: "已发布版本变了" })).closest("section")!;
    expect(card.textContent).toContain("已发布版本已变为 v4，之前的结果不再适用。重新测试会和 v4 比较。");
    expect(within(card).getByRole("button", { name: "重新测试" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /发布为 v/ })).toBeNull();
  });

  it("asks for the Owner's own cases first when only the basic safety check exists", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(UNTESTED);
    vi.mocked(getAgentEvalManager).mockResolvedValue({
      kind: "agent_eval_manager", schema_version: 1, agent_id: "osa_1", target: { kind: "live" }, suites: [], runs: [],
      trigger_policy: null, release_gate: { required: true, ready: true, reason: null },
    } as never);
    const onOpenEvaluate = vi.fn();
    renderPanel({ onOpenEvaluate });
    const card = (await screen.findByRole("heading", { name: "只有基础安全检查" })).closest("section")!;
    fireEvent.click(within(card).getByRole("button", { name: "添加用例" }));
    expect(onOpenEvaluate).toHaveBeenCalledOnce();
    expect(within(card).getByRole("button", { name: "测试这次修改" }).className).toContain("secondary");
  });

  it("on Evaluate, adds a case first and rereads the cases once the suite is saved", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(UNTESTED);
    const noCases = {
      kind: "agent_eval_manager", schema_version: 1, agent_id: "osa_1", target: { kind: "live" }, suites: [], runs: [],
      trigger_policy: null, release_gate: { required: true, ready: true, reason: null },
    };
    vi.mocked(getAgentEvalManager).mockResolvedValue(noCases as never);
    const onAddCases = vi.fn();
    const onTestState = vi.fn();
    let bump = () => {};
    function Harness() {
      const [key, setKey] = useState(0);
      bump = () => setKey((value) => value + 1);
      return <AgentVersionWorkbenchPanel agentName="Revenue Analyst" agentId="osa_1" view="evaluate" workingCopy={null}
        onAvailability={vi.fn()} onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()}
        onAddCases={onAddCases} onTestState={onTestState} suitesRefreshKey={key} />;
    }
    renderIntl(<Harness />);
    const card = (await screen.findByRole("heading", { name: "只有基础安全检查" })).closest("section")!;
    fireEvent.click(within(card).getByRole("button", { name: "添加用例" }));
    expect(onAddCases).toHaveBeenCalledOnce();
    expect(within(card).getByRole("button", { name: "测试这次修改" }).className).toContain("secondary");
    expect(onTestState).toHaveBeenLastCalledWith("no_cases");

    // B8: saving the first case on this page ends the "no cases" state without a reload.
    vi.mocked(getAgentEvalManager).mockResolvedValue({
      ...noCases,
      suites: [{ suite_id: "aes_1", suite_kind: "owner_quality", name: "Mine", state: "active", revision: { case_count: 1, cases: [] } }],
    } as never);
    act(() => bump());
    expect(await screen.findByRole("heading", { name: "你的修改还没测试" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "只有基础安全检查" })).toBeNull();

    vi.mocked(getAgentEvalManager).mockResolvedValue(noCases as never);
    act(() => bump());
    expect(await screen.findByRole("heading", { name: "只有基础安全检查" })).toBeTruthy();
    expect(onTestState).toHaveBeenLastCalledWith("no_cases");
    expect(screen.getByText(/当前没有自定义测试用例/).textContent).toContain("不会阻止发布");
  });

  it("has nothing to test while the draft matches the released version", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...UNTESTED, next_draft_identity: null });
    const onOpenConfig = vi.fn();
    renderPanel({ onOpenConfig });
    const card = (await screen.findByRole("heading", { name: "还没有要测试的修改" })).closest("section")!;
    expect(within(card).queryByRole("button", { name: "测试这次修改" })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "去配置" }));
    expect(onOpenConfig).toHaveBeenCalledOnce();
  });

  it("shows a passed test once, with the comparison folded inside the same card", async () => {
    // 1.5.95 on 116: "Test passed · Better 0 · Same 6 · Worse 0" sat above a second "Comparison complete" card
    // repeating the same counts.
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...READY,
      active_snapshot_identity: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z", execution_id: "aex_1" },
      active_execution_summary: COMPLETED_EXECUTION,
    } as AgentVersionWorkbench);
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue({
      kind: "agent_version_case_changes", schema_version: 1, execution_id: "aex_1",
      snapshot: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      baseline: { release_id: "arl_1", release_sequence: 3 }, candidate: { candidate_id: "arc_1", release_sequence: null },
      summary: { case_count: 6, worse: 0, unstable: 0, better: 0, not_comparable: 0, unchanged_required_failures: 0 },
      items: [], next_offset: null,
    });
    const view = renderPanel({}, "en-US");

    const card = (await screen.findByRole("heading", { name: "Test passed" })).closest("section")!;
    await waitFor(() => expect(card.querySelector("details.am-ci-changes")).toBeTruthy());
    expect(card.textContent).toContain("Better 0 · Same 6 · Worse 0");
    // The counts appear once, and no second card repeats the result.
    expect(view.container.textContent!.match(/Same 6/g)).toHaveLength(1);
    expect(view.container.textContent).not.toContain("Comparison complete");
    const details = card.querySelector("details.am-ci-changes") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")!.textContent).toBe("Comparison details");
    expect(details.textContent).toContain("No case changed between the two versions.");
  });

  it("releases a passed test as v4 from a dialog that takes the release note", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(READY);
    vi.mocked(completeAgentVersionRelease).mockResolvedValue({} as never);
    renderPanel();

    expect(await screen.findByRole("heading", { name: "测试通过" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发布为 v4" }));
    const dialog = await screen.findByRole("dialog", { name: "发布为 v4？" });
    // A private Agent: only its Owner uses the released version.
    expect(dialog.textContent).toContain("测试已通过。发布后，你下一次使用时会用 v4；v3 留在版本历史里，可以恢复。");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(completeAgentVersionRelease).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "发布为 v4" }));
    const again = await screen.findByRole("dialog", { name: "发布为 v4？" });
    fireEvent.change(within(again).getByRole("textbox", { name: "发布说明（选填）" }), { target: { value: " 百分比保留一位小数 " } });
    fireEvent.click(within(again).getByRole("button", { name: "发布为 v4" }));
    await waitFor(() => expect(completeAgentVersionRelease).toHaveBeenCalledWith("osa_1", expect.anything(), "activate", expect.any(String), "百分比保留一位小数"));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.objectContaining({ kind: "success", text: "v4 已发布 · Revenue Analyst" })));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it.each([
    [{ code: "agent_eval_release_blocked", details: { reason_codes: ["critical_regression"] } }, "测试结果不允许这次发布：关键测试变差。处理后重新测试，再发布。"],
    [{ code: "agent_eval_evidence_stale" }, "测试结果和这次修改对不上了：测试之后草稿、测试本身或已发布的 v3 有变化。"],
    [{ code: "agent_eval_override_required" }, "这次结果有需要你决定的风险。"],
    [{ code: "agent_release_audience_action_mismatch" }, "这个 Agent 的使用范围变了，发布方式也随之改变。"],
    [{ code: "qualified_reviewer_unavailable" }, "这个团队目前没有可审批的人"],
  ])("names why a release was refused next to the button (%o)", async (error, text) => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(READY);
    vi.mocked(completeAgentVersionRelease).mockRejectedValue(new ApiError(409, "refused", error));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "发布为 v4" }));
    const dialog = await screen.findByRole("dialog", { name: "发布为 v4？" });
    fireEvent.click(within(dialog).getByRole("button", { name: "发布为 v4" }));
    await waitFor(() => expect(dialog.textContent).toContain(text));
    expect(dialog.textContent).not.toContain("没能发布这个版本。请刷新后重试。");
  });

  it("submits a shared Agent for review as v4 with the note from the dialog", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(SHARED_READY);
    vi.mocked(completeAgentVersionRelease).mockResolvedValue({} as never);
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "提交审批（发布为 v4）" }));
    const dialog = await screen.findByRole("dialog", { name: "提交审批（发布为 v4）" });
    expect(dialog.textContent).toContain("审批人和版本历史里能看到。");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "发布说明（选填）" }), { target: { value: "开放给销售团队" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "提交审批（发布为 v4）" }));
    await waitFor(() => expect(completeAgentVersionRelease).toHaveBeenCalledWith("osa_1", expect.anything(), "submit", expect.any(String), "开放给销售团队"));
    expect(await screen.findByText("已提交审批。管理员审批前，v3 继续服务。", { selector: "[role=status]" })).toBeTruthy();
  });

  it.each([
    ["agent_not_active", {}, "智能体已归档，无法执行此操作。"],
    ["agent_eval_required", { reason_codes: ["platform_contract_upgraded"] }, "这次检查之后，安全执行契约有了更新。请重新运行发布检查，智能体本身不需要修改。"],
  ])("explains a release refused with %s inside the release dialog", async (code, details, text) => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(SHARED_READY);
    // Built from the gateway's exact body so the real parse layer decides message and code.
    vi.mocked(completeAgentVersionRelease).mockRejectedValue(await readApiError(new Response(
      JSON.stringify({ detail: { error: { code, details } } }),
      { status: 422, headers: { "Content-Type": "application/json" } },
    )));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "提交审批（发布为 v4）" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "提交审批（发布为 v4）" }));
    // The failure stays in the dialog the Owner acted in.
    await waitFor(() => expect(within(dialog).getByRole("alert").textContent).toBe(text));
  });

  it("explains a release refused on a plan behind the saved cases and refetches instead of retrying", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(SHARED_READY);
    vi.mocked(completeAgentVersionRelease).mockRejectedValue(await readApiError(new Response(
      JSON.stringify({ detail: { error: { code: "agent_eval_plan_behind", details: { candidate_id: "arc_1", plan_id: "aep_1", plan_policy_revision_id: "aepr_old", current_policy_revision_id: "aepr_new" } } } }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    )));
    const view = renderPanel({}, "en-US");
    fireEvent.click(await screen.findByRole("button", { name: "Submit for review (as v4)" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Submit for review (as v4)" }));
    await waitFor(() => expect(within(dialog).getByRole("alert").textContent).toContain("Test cases were saved after this page loaded, so these results don’t include them."));
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(view.container.textContent).not.toMatch(/aepr_|agent_eval_plan_behind/);
    await waitFor(() => expect(vi.mocked(getAgentVersionWorkbench).mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("tells the Owner why a passed test has advisory risks in words; codes stay in diagnostics", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "ready_with_risks",
      readiness: "ready_with_risks",
      reason_codes: ["owner_quality_skipped", "brand_new_backend_code"],
      primary_action: { kind: "resolve_risks", enabled: true, disabled_reason_codes: [] },
    } as AgentVersionWorkbench);
    const view = renderPanel();

    expect(await screen.findByRole("heading", { name: "测试通过，有 2 个提示性风险" })).toBeTruthy();
    expect(screen.getByText("没有运行你自己的评测组")).toBeTruthy();
    expect(screen.getByLabelText("接受「没有运行你自己的评测组」的理由")).toBeTruthy();
    expect(screen.getByText("还有一项需要处理的问题").getAttribute("title")).toBeNull();
    expect(view.container.textContent).not.toMatch(/owner_quality_skipped|brand_new_backend_code/);
    expect((screen.getByRole("button", { name: "记录风险决定" }) as HTMLButtonElement).disabled).toBe(true);
  });

  /** A private Agent whose test has no conclusion because one case gave inconsistent results (the customer report, decided before the majority rule). */
  const UNSTABLE_CHANGES: AgentVersionCaseChanges = {
    kind: "agent_version_case_changes", schema_version: 1, execution_id: "aex_1",
    snapshot: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
    baseline: { release_id: "arl_1", release_sequence: 3 }, candidate: { candidate_id: "arc_1", release_sequence: null },
    summary: { case_count: 2, worse: 0, unstable: 1, better: 0, not_comparable: 0, unchanged_required_failures: 0 },
    items: [
      { suite_revision_id: "aev_1", suite_name: "季度经营问答", case_revision_id: "aec_u", case_key: "quality.3", case_label: "促销活动 ROI 排名", change: "unstable", classification: "inconclusive", reason_codes: ["flaky_limit_exceeded"], key_case: false, baseline: { passed: 3, failed: 0, runs: 3 }, candidate: { passed: 2, failed: 1, runs: 3 } },
    ],
    next_offset: null,
    unfinished: [],
  };
  const PRIVATE_UNSTABLE: AgentVersionWorkbench = {
    ...WORKBENCH,
    display_state: "unknown",
    readiness: "unknown",
    reason_codes: ["flaky_limit_exceeded"],
    active_snapshot_identity: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z", execution_id: "aex_1" },
    active_execution_summary: COMPLETED_EXECUTION,
    primary_action: { kind: "retry_after_environment_fix", enabled: true, disabled_reason_codes: [] },
    private_release: { available: true, blocked_reason_code: null, candidate_covers_draft: true, release_sequence: 4, test_state: "no_conclusion", warning: false, reason_codes: ["flaky_limit_exceeded"] },
  };

  it("names each unstable case with both sides' attempts and lets a private Agent release without a reason", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(PRIVATE_UNSTABLE);
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue(UNSTABLE_CHANGES);
    vi.mocked(releasePrivateAgentDraft).mockResolvedValue({} as never);
    vi.mocked(getAgentVersionCaseAnswers).mockResolvedValue({
      kind: "agent_version_case_answers", schema_version: 1, execution_id: "aex_1", suite_revision_id: "aev_1",
      case_revision_id: "aec_u", case_key: "quality.3", case_label: "促销活动 ROI 排名", task: "按 ROI 排名促销活动",
      baseline: { attempts: [{ attempt: 1, status: "passed", failure_code: null, duration_ms: 1_000, answer: "春季促销第一", answer_truncated: false }] },
      candidate: { attempts: [{ attempt: 1, status: "failed", failure_code: null, duration_ms: 1_000, answer: "夏季促销第一", answer_truncated: false }] },
    });
    renderPanel();

    expect(await screen.findByRole("heading", { name: "测试没有结论" })).toBeTruthy();
    // One unstable case is named as one, not "too many", with how its attempts split on both sides.
    expect(await screen.findByText("1 个用例结果不稳定")).toBeTruthy();
    expect(screen.queryByText(/太多/)).toBeNull();
    const row = screen.getByText(/这次修改 3 次里 2 次通过、1 次没通过；v3 3 次都通过/);
    expect(row.textContent).toBe("「促销活动 ROI 排名」：这次修改 3 次里 2 次通过、1 次没通过；v3 3 次都通过");
    fireEvent.click(screen.getByRole("button", { name: "查看回答" }));
    const answers = await screen.findByRole("dialog", { name: "促销活动 ROI 排名" });
    expect(await within(answers).findByText("夏季促销第一")).toBeTruthy();
    fireEvent.keyDown(answers, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    expect(screen.getByRole("button", { name: "重新测试" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发布为 v4" }));
    const dialog = await screen.findByRole("dialog", { name: "发布为 v4？" });
    expect(dialog.textContent).toContain("这次修改的测试没有结论：1 个用例结果不稳定。");
    expect(dialog.textContent).not.toContain("测试已通过");
    // The optional release note is the only field: no required reason.
    expect(within(dialog).getAllByRole("textbox")).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole("button", { name: "发布为 v4" }));
    await waitFor(() => expect(releasePrivateAgentDraft).toHaveBeenCalledWith("osa_1", expect.anything(), expect.objectContaining({ workspaceId: "ws_1", testState: "no_conclusion", releaseNote: null })));
    expect(completeAgentVersionRelease).not.toHaveBeenCalled();
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.objectContaining({ kind: "success", text: "v4 已发布 · Revenue Analyst" })));
  });

  it("says the same in English", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(PRIVATE_UNSTABLE);
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue(UNSTABLE_CHANGES);
    renderPanel({}, "en-US");
    expect(await screen.findByText("1 case gave inconsistent results")).toBeTruthy();
    expect(screen.getByText(/This change passed 2 of 3 runs and failed 1; v3 passed all 3 runs/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Release as v4" }));
    const dialog = await screen.findByRole("dialog", { name: "Release as v4?" });
    expect(dialog.textContent).toContain("This change's test has no verdict: 1 case gave inconsistent results.");
  });

  /** The same customer case under the majority rule: 2 of 3 passing passes, and the split stays visible. */
  const MAJORITY_CHANGES: AgentVersionCaseChanges = {
    ...UNSTABLE_CHANGES,
    summary: { case_count: 2, worse: 0, unstable: 0, better: 0, not_comparable: 0, unchanged_required_failures: 0 },
    items: [],
    split: [
      { suite_revision_id: "aev_1", suite_name: "季度经营问答", case_revision_id: "aec_u", case_key: "quality.3", case_label: "促销活动 ROI 排名", classification: "unchanged_pass", baseline: { passed: 3, failed: 0, runs: 3 }, candidate: { passed: 2, failed: 1, runs: 3 } },
    ],
  };
  const PRIVATE_MAJORITY: AgentVersionWorkbench = {
    ...PRIVATE_UNSTABLE,
    display_state: "ready",
    readiness: "ready",
    reason_codes: [],
    primary_action: { kind: "activate_candidate", enabled: true, disabled_reason_codes: [] },
    private_release: { ...PRIVATE_UNSTABLE.private_release!, test_state: "passed", reason_codes: [] },
  };

  it("passes a case its majority decided and still shows how its attempts split", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(PRIVATE_MAJORITY);
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue(MAJORITY_CHANGES);
    const view = renderPanel();

    expect(await screen.findByText("1 个用例几次的结果不一致，已按多数次的结果判定：")).toBeTruthy();
    expect(screen.getByText(/这次修改 3 次里 2 次通过、1 次没通过；v3 3 次都通过/).textContent).toBe("「促销活动 ROI 排名」：这次修改 3 次里 2 次通过、1 次没通过；v3 3 次都通过");
    expect(view.container.textContent).not.toMatch(/不稳定|没有结论/);
    fireEvent.click(screen.getByRole("button", { name: "发布为 v4" }));
    const dialog = await screen.findByRole("dialog", { name: "发布为 v4？" });
    expect(dialog.textContent).not.toMatch(/不稳定|没有结论/);
  });

  it("shows a majority-decided split alongside the Datarc Owner's access in English", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(PRIVATE_MAJORITY);
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue({
      ...MAJORITY_CHANGES,
      datarc_owner_access: { owner_name: "Allen" },
    });
    const view = renderPanel({}, "en-US");
    expect(await screen.findByText("1 case's runs disagreed; it was decided by most of its runs:")).toBeTruthy();
    expect(screen.getByText(/This change passed 2 of 3 runs and failed 1; v3 passed all 3 runs/)).toBeTruthy();
    expect(screen.getByText("Data cases were tested with Allen's Datarc access. Members with different access may see different data.")).toBeTruthy();
    expect(view.container.textContent).not.toMatch(/inconsistent|no verdict/i);
  });

  it("warns before a private release past a regression and labels the release as anyway", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...PRIVATE_UNSTABLE,
      display_state: "blocked",
      readiness: "blocked",
      reason_codes: ["critical_regression"],
      primary_action: { kind: "fix_next_draft", enabled: true, disabled_reason_codes: ["critical_regression"] },
      private_release: { ...PRIVATE_UNSTABLE.private_release!, test_state: "regressed", warning: true, reason_codes: ["critical_regression"] },
    });
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue({
      ...UNSTABLE_CHANGES,
      summary: { ...UNSTABLE_CHANGES.summary, worse: 1, unstable: 0 },
      items: [{ ...UNSTABLE_CHANGES.items[0], case_label: "渠道毛利率排名前五", change: "worse", classification: "regressed", reason_codes: [], key_case: true, candidate: { passed: 0, failed: 3, runs: 3 } }],
    });
    vi.mocked(releasePrivateAgentDraft).mockResolvedValue({} as never);
    renderPanel();

    expect(await screen.findByRole("heading", { name: "有 1 条用例变差了" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "仍然发布为 v4" }));
    const dialog = await screen.findByRole("dialog", { name: "发布为 v4？" });
    expect(dialog.textContent).toContain("这次修改让「渠道毛利率排名前五」变差了。");
    fireEvent.click(within(dialog).getByRole("button", { name: "仍然发布为 v4" }));
    await waitFor(() => expect(releasePrivateAgentDraft).toHaveBeenCalledWith("osa_1", expect.anything(), expect.objectContaining({ testState: "regressed" })));
  });

  it("offers releasing an untested private draft next to the test, and names a result that changed meanwhile", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...UNTESTED,
      private_release: { available: true, blocked_reason_code: null, candidate_covers_draft: false, release_sequence: 4, test_state: "not_tested", warning: false, reason_codes: [] },
    });
    vi.mocked(releasePrivateAgentDraft).mockRejectedValue(await readApiError(new Response(
      JSON.stringify({ detail: { error: { code: "agent_release_test_state_changed", details: { test_state: "no_conclusion" } } } }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    )));
    renderPanel();

    expect(await screen.findByRole("button", { name: "测试这次修改" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发布为 v4" }));
    const dialog = await screen.findByRole("dialog", { name: "发布为 v4？" });
    expect(dialog.textContent).toContain("还没测试这次修改。");
    fireEvent.click(within(dialog).getByRole("button", { name: "发布为 v4" }));
    await waitFor(() => expect(within(dialog).getByRole("alert").textContent).toBe("测试结果刚刚有了变化。看一下新的结果，再决定是否发布。"));
  });

  it("holds a private release while its test runs", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...RUNNING,
      private_release: { available: false, blocked_reason_code: "agent_release_test_running", candidate_covers_draft: true, release_sequence: 4, test_state: "not_tested", warning: false, reason_codes: [] },
    });
    renderPanel();
    expect(await screen.findByRole("button", { name: "停止测试" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "发布为 v4" })).toBeNull();
  });

  it("shows determinate progress while testing and stops only after the exact-consequence confirmation", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(RUNNING);
    vi.mocked(getAgentComparisonEstimate).mockResolvedValue({
      kind: "agent_eval_usage_estimate", schema_version: 1, status: "available", unavailable_reason: null,
      cases_planned: 24, attempts_planned: 72, tokens: { low: 12_000, high: 18_000 }, tokens_cached: null,
      // 1.5.93 on 116 showed "About 0–36 model requests": a lower end of 0 says nothing, so only the upper end shows.
      model_calls: { low: 0, high: 115 }, duration_ms: { low: 300_000, high: 540_000 },
      basis: { window_days: 30, scope: "same_cases", sample_attempts: 54 }, computed_at: "2026-10-05T06:12:00Z",
    });
    const view = renderPanel();

    const card = (await screen.findByRole("heading", { name: "正在运行用例 17/24" })).closest("section")!;
    expect(await within(card).findByText(/预计最多约 115 次模型请求/)).toBeTruthy();
    expect(card.textContent).not.toContain("0–115");
    expect(card.textContent).toContain("可以离开这一页，结果出来后会通知你。");
    expect(screen.getAllByRole("progressbar", { name: "用例 17 / 24" }).length).toBeGreaterThan(0);
    // No primary action while testing; stopping is a ghost action.
    expect(view.container.querySelector(".am-ci-test .settings-button--primary")).toBeNull();
    expect(view.container.textContent).not.toMatch(MONEY);

    fireEvent.click(within(card).getByRole("button", { name: "停止测试" }));
    const dialog = await screen.findByRole("dialog", { name: "停止这次测试？" });
    expect(dialog.textContent).toContain("已跑 17 / 24 条，已用 9,800 token、63 次模型请求。停止后结果不完整，要重新测试才能发布。");
    fireEvent.click(within(dialog).getByRole("button", { name: "继续测试" }));
    expect(cancelAgentVersionComparison).not.toHaveBeenCalled();
    fireEvent.click(within(card).getByRole("button", { name: "停止测试" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "停止测试" }));
    await waitFor(() => expect(cancelAgentVersionComparison).toHaveBeenCalledOnce());
  });

  it("labels a test Coach started after its edit and falls back to elapsed time without run insights", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...RUNNING,
      active_execution_summary: { ...RUNNING.active_execution_summary!, progress: undefined, origin_kind: "internal_coach", started_at: new Date(Date.now() - 125_000).toISOString() },
    });
    renderPanel({}, "en-US");
    const card = (await screen.findByRole("heading", { name: "Running cases" })).closest("section")!;
    expect(card.textContent).toContain("Started after Coach's edit");
    expect(screen.getByText(/^Running for 2 min/)).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("keeps polling a running test until it settles", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const settled = { ...WORKBENCH, display_state: "unknown", readiness: "unknown", reason_codes: ["attempt_infrastructure_incomplete"], primary_action: { kind: "retry_after_environment_fix", enabled: true, disabled_reason_codes: [] } } as AgentVersionWorkbench;
      vi.mocked(getAgentVersionWorkbench)
        .mockResolvedValueOnce(RUNNING)
        .mockResolvedValueOnce(RUNNING)
        .mockResolvedValueOnce(RUNNING)
        .mockResolvedValue(settled);
      renderPanel();
      await waitFor(() => expect(getAgentVersionWorkbench).toHaveBeenCalledTimes(1));
      expect(await screen.findByRole("heading", { name: "正在运行用例 17/24" })).toBeTruthy();
      for (let poll = 2; poll <= 4; poll += 1) {
        await vi.advanceTimersByTimeAsync(5100);
        await waitFor(() => expect(getAgentVersionWorkbench).toHaveBeenCalledTimes(poll));
      }
      await vi.advanceTimersByTimeAsync(10_000);
      expect(getAgentVersionWorkbench).toHaveBeenCalledTimes(4);
      expect(await screen.findByRole("heading", { name: "测试没能完成" })).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("names a time-limit stop as a time limit and offers to test again", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "unknown",
      readiness: "unknown",
      reason_codes: ["attempt_time_limit_exceeded"],
      primary_action: { kind: "retry_after_environment_fix", enabled: true, disabled_reason_codes: [] },
    } as AgentVersionWorkbench);
    renderPanel();
    expect(await screen.findByRole("heading", { name: "测试超出了时间上限" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "测试没能完成" })).toBeNull();
    expect(screen.getByText("部分评测运行超出了单次时间上限，没有得出结论")).toBeTruthy();
    expect((screen.getByRole("button", { name: "重新测试" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("sends a required Tool that can't run in evaluation to its point instead of a rerun", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "unknown",
      readiness: "unknown",
      reason_codes: ["required_tool_not_run_in_eval"],
      primary_action: { kind: "retry_after_environment_fix", enabled: true, disabled_reason_codes: [] },
    } as AgentVersionWorkbench);
    const onOpenEvaluate = vi.fn();
    renderPanel({ onOpenEvaluate });
    fireEvent.click(await screen.findByRole("button", { name: "去评测改要点" }));
    expect(onOpenEvaluate).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "重新测试" })).toBeNull();
  });

  it("says a data case can't be tested in evaluation, without a retry, a regression or the raw stop reason", async () => {
    const changes: AgentVersionCaseChanges = {
      kind: "agent_version_case_changes", schema_version: 1, execution_id: "aex_1",
      snapshot: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      baseline: { release_id: "arl_1", release_sequence: 13 }, candidate: { candidate_id: "arc_1", release_sequence: null },
      summary: { case_count: 1, worse: 0, unstable: 0, better: 0, not_comparable: 1, unchanged_required_failures: 0 },
      items: [],
      next_offset: null,
      unfinished: [
        { suite_revision_id: "aev_1", suite_name: "销售问答", case_revision_id: "aec_sales", case_key: "quality.1", case_label: "2026年8月销售额明细", reason_codes: ["tool_not_run_in_eval"], tools: ["bi_query", "bi_schema"], failures: [] },
      ],
    };
    const notRun = { status: "infra_failed", failure_code: "agent_eval_tool_not_run_in_eval", answer: null, answer_truncated: false, tools_not_run: ["bi_query"] };
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue(changes);
    vi.mocked(getAgentVersionCaseAnswers).mockResolvedValue({
      kind: "agent_version_case_answers", schema_version: 1, execution_id: "aex_1", suite_revision_id: "aev_1",
      case_revision_id: "aec_sales", case_key: "quality.1", case_label: "2026年8月销售额明细", task: "2026年8月销售额明细",
      // An older attempt scored before this rule kept the failure breaker's instruction as its answer.
      baseline: { attempts: [{ attempt: 1, status: "failed", failure_code: null, duration_ms: 0, answer: null, answer_truncated: false, answer_withheld: "stopped_without_answer" }] },
      candidate: { attempts: [{ attempt: 1, duration_ms: 0, ...notRun }, { attempt: 2, duration_ms: null, ...notRun }] },
    });
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "unknown",
      readiness: "unknown",
      reason_codes: ["tool_not_run_in_eval"],
      primary_action: { kind: "retry_after_environment_fix", enabled: true, disabled_reason_codes: [] },
      active_snapshot_identity: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z", execution_id: "aex_1" },
      active_execution_summary: COMPLETED_EXECUTION,
    } as AgentVersionWorkbench);
    const view = renderPanel();

    const card = (await screen.findByRole("heading", { name: "测试没有结论" })).closest("section")!;
    expect(card.textContent).toContain("部分用例的回答要用到评测里暂时不能运行的工具（例如查询数据）");
    await waitFor(() => expect(card.textContent).toContain("工具：「查询数据」、「查看数据字段」 · 用例：「2026年8月销售额明细」"));
    expect(within(card).queryByRole("button", { name: "重新测试" })).toBeNull();
    expect(within(card).queryByRole("button", { name: "修好后重新测试" })).toBeNull();
    // Neither version could be tested: no case got worse, and none counts as better, unchanged or worse.
    expect(view.container.textContent).not.toMatch(/有用例变差|条用例变差了|必需用例没通过/);
    expect(view.container.textContent).toContain("无法比较 1 条");

    fireEvent.click(within(card).getByRole("button", { name: "查看用例" }));
    const dialog = await screen.findByRole("dialog", { name: "2026年8月销售额明细" });
    expect(await within(dialog).findAllByText("评测里测不了")).toHaveLength(2);
    expect(within(dialog).getAllByText("评测里暂时不能运行「查询数据」，这条用例测不了。这不算智能体没通过，重新测试也不会变。")).toHaveLength(2);
    expect(within(dialog).getByText("智能体没有给出回答：它调用工具多次失败后停了下来。")).toBeTruthy();
    // An attempt with no recorded time shows none, never "0 秒"; nothing internal reaches the Owner.
    expect(dialog.textContent).not.toMatch(/0 ?秒|insufficient_data|peer_total_failures|环境故障|没有回答/);
  });

  it("withdraws a shared Agent's submission only after the confirmation names what is lost", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...SHARED_READY,
      candidate_identity: { ...SHARED_READY.candidate_identity!, state: "pending" },
      display_state: "pending_review",
      primary_action: { kind: "wait_for_review", enabled: true, disabled_reason_codes: [] },
    });
    vi.mocked(withdrawAgentVersionCandidate).mockResolvedValue({} as never);
    renderPanel();
    expect(await screen.findByRole("heading", { name: "已提交审批" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "测试这次修改" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "撤回候选版本…" }));
    const dialog = await screen.findByRole("dialog", { name: "撤回候选？" });
    expect(dialog.textContent).toContain("候选的对比结果和风险决定会作废，草稿内容保留。");
    fireEvent.click(within(dialog).getByRole("button", { name: "撤回候选版本" }));
    await waitFor(() => expect(withdrawAgentVersionCandidate).toHaveBeenCalledOnce());
    expect(await screen.findByText(/候选已撤回/, { selector: "[role=status]" })).toBeTruthy();
  });

  it("lists changed cases worse first and opens both versions' answers", async () => {
    const changes: AgentVersionCaseChanges = {
      kind: "agent_version_case_changes", schema_version: 1, execution_id: "aex_1",
      snapshot: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      baseline: { release_id: "arl_1", release_sequence: 3 }, candidate: { candidate_id: "arc_1", release_sequence: null },
      summary: { case_count: 24, worse: 1, unstable: 1, better: 1, not_comparable: 1, unchanged_required_failures: 0 },
      items: [
        { suite_revision_id: "aev_1", suite_name: "季度经营问答", case_revision_id: "aec_b", case_key: "quality.2", case_label: "新客复购率按月对比", change: "better", classification: "fixed", reason_codes: [], key_case: false, baseline: { passed: 2, runs: 3 }, candidate: { passed: 3, runs: 3 } },
        { suite_revision_id: "aev_1", suite_name: "季度经营问答", case_revision_id: "aec_u", case_key: "quality.3", case_label: "促销活动 ROI 排名", change: "unstable", classification: "inconclusive", reason_codes: ["flaky_limit_exceeded"], key_case: false, baseline: { passed: 2, runs: 3 }, candidate: { passed: 1, runs: 3 } },
        { suite_revision_id: "aev_1", suite_name: "季度经营问答", case_revision_id: "aec_w", case_key: "quality.1", case_label: "渠道毛利率排名前五", change: "worse", classification: "regressed", reason_codes: [], key_case: true, baseline: { passed: 3, runs: 3 }, candidate: { passed: 1, runs: 3 } },
      ],
      next_offset: null,
    };
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue(changes);
    vi.mocked(getAgentVersionCaseAnswers).mockResolvedValue({
      kind: "agent_version_case_answers", schema_version: 1, execution_id: "aex_1", suite_revision_id: "aev_1",
      case_revision_id: "aec_w", case_key: "quality.1", case_label: "渠道毛利率排名前五", task: "列出毛利率排名前五的渠道",
      baseline: { attempts: [{ attempt: 1, status: "passed", failure_code: null, duration_ms: 12_000, answer: "直营、电商…", answer_truncated: false, unsourced_figures: ["14", "52.3"] }] },
      candidate: { attempts: [
        { attempt: 1, status: "failed", failure_code: "contract_failed", duration_ms: 9_000, answer: null, answer_truncated: false },
        { attempt: 2, status: "infra_failed", failure_code: "agent_eval_attempt_timeout", duration_ms: 121_000, answer: null, answer_truncated: false, time_limit: { limit_ms: 120_000, observed_ms: 121_000, scope: "attempt" } },
      ] },
    });
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "blocked",
      readiness: "blocked",
      reason_codes: ["critical_regression"],
      active_snapshot_identity: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      active_execution_summary: COMPLETED_EXECUTION,
      primary_action: { kind: "fix_next_draft", enabled: true, disabled_reason_codes: ["critical_regression"] },
    });
    const onOpenConfig = vi.fn();
    const onAskCoach = vi.fn();
    const view = renderPanel({ onOpenConfig, onAskCoach });

    expect(await screen.findByRole("heading", { name: "有 1 条用例变差了" })).toBeTruthy();
    expect(await screen.findByText("「渠道毛利率排名前五」在 v3 通过 3 / 3，在这次修改只通过 1 / 3。")).toBeTruthy();
    const table = screen.getByRole("table");
    const names = within(table).getAllByRole("button").map((button) => button.textContent);
    expect(names).toEqual(["渠道毛利率排名前五", "促销活动 ROI 排名", "新客复购率按月对比"]);
    expect(within(table).getByText("关键用例")).toBeTruthy();
    expect(screen.getByText("无法比较 1 条")).toBeTruthy();
    expect(screen.getByText("持平 20")).toBeTruthy();
    // A case without a result means the comparison is not complete, and the pass rates leave it out.
    expect(screen.getByText("对比结果不完整：这次修改与已发布 v3")).toBeTruthy();
    // One result card: the comparison sits inside it, open because a case got worse.
    const details = table.closest("details")!;
    expect(details.open).toBe(true);
    expect(details.closest("section.am-ci-test")).toBeTruthy();
    expect(view.container.querySelectorAll("section.am-ci-card .am-ci-sum")).toHaveLength(1);
    expect(view.container.textContent).not.toContain("候选");

    fireEvent.click(within(table).getByRole("button", { name: "渠道毛利率排名前五" }));
    const dialog = await screen.findByRole("dialog", { name: "渠道毛利率排名前五" });
    expect(await within(dialog).findByText("直营、电商…")).toBeTruthy();
    expect(within(dialog).getAllByText("没有回答")).toHaveLength(2);
    expect(within(dialog).getByText("未通过")).toBeTruthy();
    // A time-limit stop names the limit and how long it ran, not an environment failure.
    expect(within(dialog).getByText(/^超出时间上限（上限 .+，已运行 .+）$/)).toBeTruthy();
    expect(within(dialog).queryByText("环境故障")).toBeNull();
    // The advisory number check shows its figures as a hint on a passed attempt.
    expect(within(dialog).getByText(/没有直接来源：14、52\.3。/)).toBeTruthy();
    expect(dialog.textContent).not.toMatch(/contract_failed|aec_/);
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "去草稿里修复" }));
    expect(onOpenConfig).toHaveBeenCalledOnce();
    // Ask Coach to fix it hands over the failed checks in the same words the card shows.
    fireEvent.click(screen.getByRole("button", { name: "让 Coach 修" }));
    expect(onAskCoach).toHaveBeenCalledWith([expect.any(String)]);
    expect(onAskCoach.mock.calls[0][0][0]).not.toMatch(/critical_regression/);
  });

  it("keeps reason sentences only when the change table is unavailable (flag off)", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      display_state: "blocked",
      readiness: "blocked",
      reason_codes: ["critical_regression"],
      active_snapshot_identity: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      active_execution_summary: { ...COMPLETED_EXECUTION, progress: undefined },
      primary_action: { kind: "fix_next_draft", enabled: true, disabled_reason_codes: ["critical_regression"] },
    });
    const view = renderPanel();
    expect(await screen.findByText("关键测试变差")).toBeTruthy();
    await waitFor(() => expect(getAgentVersionCaseChanges).toHaveBeenCalled());
    expect(screen.queryByRole("table")).toBeNull();
    expect(view.container.textContent).not.toMatch(MONEY);
  });

  it("explains a failed platform case only with checks that had something to check", async () => {
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue({
      kind: "agent_version_case_changes", schema_version: 1, execution_id: "aex_1",
      snapshot: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      baseline: { release_id: "arl_1", release_sequence: 3 }, candidate: { candidate_id: "arc_1", release_sequence: null },
      summary: { case_count: 0, worse: 0, unstable: 0, better: 0, not_comparable: 0, unchanged_required_failures: 0 },
      items: [],
      next_offset: null,
    });
    vi.mocked(getAgentEvalManager).mockResolvedValue({
      suites: [], runs: [{ run_id: "aer_1", target_kind: "candidate", revision_id: "arv_candidate", status: "failed" }],
    } as never);
    vi.mocked(getAgentEvalRun).mockResolvedValue({
      results: [{
        status: "failed", case_key: "platform.safe_contract",
        deterministic: { scorers: [
          { name: "no_fabrication", passed: false, applicable: false, detail: "not checked" },
          { name: "budget_timeout", passed: false, detail: "budget violations=['tool_calls']" },
        ] },
      }],
    } as never);
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH, display_state: "blocked", readiness: "blocked", reason_codes: ["required_quality_failed"],
      active_snapshot_identity: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      active_execution_summary: COMPLETED_EXECUTION,
      primary_action: { kind: "fix_next_draft", enabled: true, disabled_reason_codes: ["required_quality_failed"] },
    });
    const view = renderPanel();

    expect(await screen.findByText(/超出工具调用、耗时或费用预算/)).toBeTruthy();
    expect(view.container.textContent).not.toContain("出现了没有来源的数字");
  });

  it("names the unchanged required failure and opens its answers from the blocked card", async () => {
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue({
      kind: "agent_version_case_changes", schema_version: 1, execution_id: "aex_1",
      snapshot: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      baseline: { release_id: "arl_1", release_sequence: 3 }, candidate: { candidate_id: "arc_1", release_sequence: null },
      summary: { case_count: 1, worse: 0, unstable: 0, better: 0, not_comparable: 0, unchanged_required_failures: 1 },
      items: [{ suite_revision_id: "aev_1", suite_name: "季度经营问答", case_revision_id: "aec_1", case_key: "quality.1", case_label: "渠道毛利率排名", change: "required_failure", classification: "unchanged_fail", reason_codes: [], key_case: false, baseline: { passed: 0, runs: 3 }, candidate: { passed: 0, runs: 3 } }],
      next_offset: null,
    });
    vi.mocked(getAgentVersionCaseAnswers).mockResolvedValue({
      kind: "agent_version_case_answers", schema_version: 1, execution_id: "aex_1", suite_revision_id: "aev_1", case_revision_id: "aec_1", case_key: "quality.1", case_label: "渠道毛利率排名", task: "列出渠道毛利率排名",
      baseline: { attempts: [] }, candidate: { attempts: [{ attempt: 1, status: "failed", failure_code: null, duration_ms: 1000, answer: "渠道 A", answer_truncated: false }] },
    });
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH, display_state: "blocked", readiness: "blocked", reason_codes: ["required_quality_failed"],
      active_snapshot_identity: { id: "aevs_1", hash: "sha256:s", created_at: "2026-10-05T06:26:00Z" },
      active_execution_summary: COMPLETED_EXECUTION,
      primary_action: { kind: "fix_next_draft", enabled: true, disabled_reason_codes: ["required_quality_failed"] },
    });
    renderPanel();
    expect(await screen.findByText(/评测组「季度经营问答」的「渠道毛利率排名」在这次修改通过 0 \/ 3/)).toBeTruthy();
    expect(within(screen.getByRole("table")).getByText("必需用例未通过")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看用例" }));
    expect(await screen.findByText("渠道 A")).toBeTruthy();
  });

  it("settles a 404 workbench (archived agent) into the not-available state instead of an endless skeleton", async () => {
    vi.mocked(getAgentVersionWorkbench).mockRejectedValue(new ApiError(404, "not found"));
    const onAvailability = vi.fn();
    const view = renderPanel({ onAvailability }, "en-US");
    expect(await screen.findByText(/Version check isn't available for this agent/)).toBeTruthy();
    expect(onAvailability).toHaveBeenCalledWith("disabled");
    expect(view.container.querySelector("[aria-busy='true']")).toBeNull();
    expect(view.container.querySelector(".am-ci")?.getAttribute("data-state")).toBe("unavailable");
  });

  it("shows a retryable error when the workbench fails to load, and recovers on retry", async () => {
    vi.mocked(getAgentVersionWorkbench).mockRejectedValueOnce(new ApiError(500, "boom"));
    const onError = vi.fn();
    renderPanel({ onError }, "en-US");
    expect(await screen.findByText("Version details couldn't be loaded.")).toBeTruthy();
    expect(onError).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(getAgentVersionWorkbench).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("Version details couldn't be loaded.")).toBeNull());
  });
});

describe("AgentVersionWorkbenchPanel · proposals", () => {
  const proposal: AgentChangeProposal = {
    kind: "agent_change_proposal",
    schema_version: 1,
    proposal_id: "acp_1",
    agent_id: "osa_1",
    source_type: "coach",
    source_ref: "owner:req_1",
    rationale: "先给结论",
    status: "open",
    row_version: 1,
    created_at: "2026-09-14T08:00:00Z",
    decided_at: null,
    operations: [{ op: "replace", path: "/definition/system_prompt", value: "new" }],
    atomic_groups: [{ id: "behavior", operation_indexes: [0], depends_on: [] }],
    suggested_cases: [],
    base: { release_id: "arl_1", working_copy_id: "awc_1", working_copy_row_version: 3 },
  };
  const manager: Awaited<ReturnType<typeof getAgentEvalManager>> = {
    kind: "agent_eval_manager", schema_version: 1, agent_id: "osa_1", target: { kind: "live" },
    suites: [{
      suite_id: "aes_1", suite_kind: "owner_quality", name: "发布回归", visibility: "owner", state: "active", row_version: 2, platform_managed: false,
      revision: { suite_revision_id: "aev_1", sequence: 1, case_count: 1, created_at: "2026-09-14T08:00:00Z", cases: [] },
      evidence: { state: "missing", stale_reason: null, updated_at: null, run: null },
    }],
    runs: [], trigger_policy: null, release_gate: { required: true, ready: false, reason: "missing" },
  };
  const caseProposal: AgentChangeProposal = {
    ...proposal, operations: [], atomic_groups: [], base: { ...proposal.base, suite_revision_id: "aev_1" },
    suggested_cases: [{ id: "one", task: "Check the supplied total", failure_mode: "Invented total", decision_status: "pending", covers_group_ids: [] }],
  };

  beforeEach(() => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      change_proposals: [proposal],
      next_draft_identity: { base_release_id: "arl_1", id: "awc_1", row_version: 3 },
    });
    vi.mocked(getAgentEvalManager).mockReset().mockResolvedValue(manager);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("saves the whole suggested update without requiring individual choices", async () => {
    vi.mocked(applyAgentChangeProposal).mockResolvedValue({ status: "accepted" });
    const view = renderPanel({ view: "proposals" });

    expect(await screen.findByText("先给结论")).toBeTruthy();
    expect(screen.getByText("将修改：智能体指令。")).toBeTruthy();
    expect(view.container.querySelector("details")?.open).toBe(false);
    expect(view.container.textContent).not.toMatch(/behavior|\/definition\/system_prompt/);
    expect(view.container.querySelectorAll(".settings-button--primary")).toHaveLength(1);
    const merge = screen.getByRole("button", { name: "将这组修改保存到草稿" }) as HTMLButtonElement;
    fireEvent.click(merge);
    await waitFor(() => expect(applyAgentChangeProposal).toHaveBeenCalledWith("osa_1", proposal, ["behavior"], 3));
    expect(await screen.findByText("已合入 1 项变更到草稿，已发布版本不变。", { selector: "[role=status]" })).toBeTruthy();
  });

  it("compares with and merges into the released version while no draft is saved", async () => {
    // 1.5.93 on 116: the draft matched released v4, the card said no draft was loaded and the merge stayed disabled.
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [proposal], next_draft_identity: null });
    vi.mocked(applyAgentChangeProposal).mockResolvedValue({ status: "accepted" });
    const released = {
      definition: { system_prompt: "old" },
      listing: {},
      audience: { schema_version: 1 as const, effective_mode: "private" as const, user_ids: [], group_ids: [] },
    };
    const view = renderPanel({ view: "proposals", released }, "en-US");

    expect(await screen.findByText("先给结论")).toBeTruthy();
    expect(view.container.textContent).not.toContain("couldn't be read");
    fireEvent.click(screen.getByText("Review or adjust changes"));
    expect(view.container.querySelector(".am-ci-pdiff del")?.textContent).toBe("old");
    const merge = screen.getByRole("button", { name: "Save these changes to draft" }) as HTMLButtonElement;
    expect(merge.disabled).toBe(false);
    fireEvent.click(merge);
    await waitFor(() => expect(applyAgentChangeProposal).toHaveBeenCalledWith("osa_1", proposal, ["behavior"], { liveReleaseId: "arl_1" }));
  });

  it("says why the merge is disabled when no change is selected", async () => {
    renderPanel({ view: "proposals" }, "en-US");
    fireEvent.click(await screen.findByText("Review or adjust changes"));
    fireEvent.click(screen.getByRole("checkbox", { name: /Agent instructions/ }));
    expect((screen.getByRole("button", { name: "Merge selected changes" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Select at least one change to merge.")).toBeTruthy();
  });

  it("names the change groups a merge was refused for instead of a generic failure", async () => {
    // Built from the gateway's exact body so the real parse layer decides code and details.
    const rejection = await readApiError(new Response(
      JSON.stringify({ detail: { error: { code: "agent_change_groups_invalid", details: {
        rule: "selected_group_unknown", group_ids: ["tone"], known_group_ids: ["behavior"], message: "Groups ['tone'] are not groups of this proposal.",
      } } } }),
      { status: 422, headers: { "Content-Type": "application/json" } },
    ));
    vi.mocked(applyAgentChangeProposal).mockRejectedValue(rejection);
    renderPanel({ view: "proposals" });

    fireEvent.click(await screen.findByRole("button", { name: "将这组修改保存到草稿" }));

    expect((await screen.findByRole("alert")).textContent).toBe("这些变更组不属于此提议：tone。请刷新页面后重新选择变更组。");
  });

  it("dismisses a proposal through an 8-second undo toast and commits only when it closes", async () => {
    vi.mocked(dismissAgentChangeProposal).mockResolvedValue(proposal);
    renderPanel({ view: "proposals" });

    fireEvent.click(await screen.findByRole("button", { name: "驳回提案" }));
    expect(screen.queryByText("先给结论")).toBeNull();
    expect(dismissAgentChangeProposal).not.toHaveBeenCalled();
    const first = vi.mocked(toast).mock.calls[0][0];
    expect(first).toMatchObject({ undo: true, text: "已驳回提案「先给结论」", action: { label: "撤销" } });

    // Undo restores the card without any request.
    await first.action!.onAction();
    expect(await screen.findByText("先给结论")).toBeTruthy();
    expect(dismissAgentChangeProposal).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "驳回提案" }));
    const second = vi.mocked(toast).mock.calls[1][0];
    second.onDismiss!();
    await waitFor(() => expect(dismissAgentChangeProposal).toHaveBeenCalledWith("osa_1", proposal));
  });

  it("keeps a pending dismissal hidden across a task switch and enqueues it only once", async () => {
    function Harness() {
      const [view, setView] = useState<"proposals" | "validation">("proposals");
      return <>
        <button type="button" onClick={() => setView("validation")}>打开发布</button>
        <button type="button" onClick={() => setView("proposals")}>打开变更建议</button>
        <AgentVersionWorkbenchPanel
          agentName="Revenue Analyst" agentId="osa_1" view={view} workingCopy={null}
          onAvailability={vi.fn()}
          onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()}
        />
      </>;
    }
    vi.mocked(dismissAgentChangeProposal).mockResolvedValue(proposal);
    renderIntl(<Harness />);
    fireEvent.click(await screen.findByRole("button", { name: "驳回提案" }));
    expect(toast).toHaveBeenCalledTimes(1);

    // The proposals view unmounts and remounts inside the 8-second window: still hidden, no second toast.
    fireEvent.click(screen.getByRole("button", { name: "打开发布" }));
    fireEvent.click(screen.getByRole("button", { name: "打开变更建议" }));
    // Nothing else is pending, so the section stays hidden.
    await waitFor(() => expect(screen.queryByRole("heading", { name: "待处理的提案" })).toBeNull());
    expect(screen.queryByText("先给结论")).toBeNull();
    expect(toast).toHaveBeenCalledTimes(1);

    vi.mocked(toast).mock.calls[0][0].onDismiss!();
    await waitFor(() => expect(dismissAgentChangeProposal).toHaveBeenCalledTimes(1));
  });

  it("enqueues a second dismissal of the same item only once", async () => {
    const commit = vi.fn().mockResolvedValue(undefined);
    const input = { keys: ["osa_x:proposal:acp_x"], text: "t", undoLabel: "u", failureText: "f", commit };
    expect(deferDismissal(input)).toBe(true);
    expect(deferDismissal(input)).toBe(false);
    expect(toast).toHaveBeenCalledTimes(1);
    vi.mocked(toast).mock.calls[0][0].onDismiss!();
    await waitFor(() => expect(commit).toHaveBeenCalledTimes(1));
    // Settled: the same item can be dismissed again later.
    await waitFor(() => expect(deferDismissal(input)).toBe(true));
    vi.mocked(toast).mock.calls[1][0].action!.onAction();
  });

  it("reports a failed deferred dismissal with an error toast and shows the proposal again", async () => {
    vi.mocked(dismissAgentChangeProposal).mockRejectedValue(new ApiError(500, "boom"));
    renderPanel({ view: "proposals" });
    fireEvent.click(await screen.findByRole("button", { name: "驳回提案" }));
    expect(screen.queryByText("先给结论")).toBeNull();
    vi.mocked(toast).mock.calls[0][0].onDismiss!();
    await waitFor(() => expect(toast).toHaveBeenCalledWith({ kind: "error", text: "没能驳回提案，已恢复显示。" }));
    expect(await screen.findByText("先给结论")).toBeTruthy();
  });

  it("adds untouched case suggestions as one set while keeping technical settings optional", async () => {
    const suggested: AgentChangeProposal = {
      ...proposal, status: "accepted", row_version: 2,
      suggested_cases: [
        { id: "one", task: "Summarize supplied evidence.", failure_mode: "Missing citations", decision_status: "pending", covers_group_ids: [],
          case_spec: { case_key: "citations", fixture_ref: {}, expected_contract: { points: [{ id: "p_one", kind: "judgment", text: "Cites the supplied evidence." }] }, scorer_config: {}, effect_policy: { mode: "no_effects" }, valid_until: null } },
        { id: "two", task: "Explain missing evidence.", failure_mode: "Invented evidence", decision_status: "pending", covers_group_ids: [] },
      ],
    };
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [suggested] });
    vi.mocked(acceptAgentProposalSuggestedCases).mockResolvedValue({ decision_id: "acpd_1", suite_revision_id: "aev_2" });
    const view = renderPanel({ view: "proposals" }, "en-US");
    expect(await screen.findByText("Cites the supplied evidence.")).toBeTruthy();
    expect(view.container.querySelector("details")?.open).toBe(false);
    const add = screen.getByRole("button", { name: "Add these 2 tests" });
    await waitFor(() => expect((add as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(add);
    await waitFor(() => expect(acceptAgentProposalSuggestedCases).toHaveBeenCalledWith("osa_1", suggested, "aev_1", [
      { suggestedCaseId: "one", caseKey: "citations", task: "Summarize supplied evidence.", failureMode: "Missing citations", caseSpec: suggested.suggested_cases[0].case_spec },
      { suggestedCaseId: "two", caseKey: "coach.acp_1.two", task: "Explain missing evidence.", failureMode: "Invented evidence" },
    ]));
    expect(applyAgentChangeProposal).not.toHaveBeenCalled();
  });

  it("retains selective case adoption and rejects an invalid edited expectation", async () => {
    const suggested: AgentChangeProposal = {
      ...proposal, status: "accepted", row_version: 2,
      suggested_cases: [
        { id: "one", task: "First question", failure_mode: "First requirement", decision_status: "pending", covers_group_ids: [],
          case_spec: { case_key: "one", fixture_ref: {}, expected_contract: {}, scorer_config: {}, effect_policy: { mode: "no_effects" }, valid_until: null } },
        { id: "two", task: "Second question", failure_mode: "Second requirement", decision_status: "pending", covers_group_ids: [] },
      ],
    };
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [suggested] });
    vi.mocked(acceptAgentProposalSuggestedCases).mockResolvedValue({ decision_id: "acpd_1", suite_revision_id: "aev_2" });
    renderPanel({ view: "proposals" }, "en-US");
    fireEvent.click(await screen.findByText("Review or adjust tests"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select case: Second question" }));
    const expectations = screen.getByRole("textbox", { name: "Expected results and scoring (JSON)" });
    fireEvent.change(expectations, { target: { value: "invalid" } });
    const add = screen.getByRole("button", { name: "Add selected cases" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    expect(acceptAgentProposalSuggestedCases).not.toHaveBeenCalled();
    fireEvent.change(expectations, { target: { value: JSON.stringify({ expected_contract: { allowed_terminal_statuses: ["completed"] }, scorer_config: {} }) } });
    await waitFor(() => expect(add.disabled).toBe(false));
    fireEvent.click(add);
    await waitFor(() => expect(acceptAgentProposalSuggestedCases).toHaveBeenCalledWith("osa_1", suggested, "aev_1", [expect.objectContaining({ suggestedCaseId: "one", task: "First question" })]));
  });

  it.each([false, true])("reviews saved-case edits in the existing set and preserves their identity (mixed=%s)", async (mixed) => {
    const spec = { case_key: "period", fixture_ref: {}, expected_contract: {}, scorer_config: {}, effect_policy: { mode: "no_effects" }, valid_until: null };
    const edit: AgentChangeProposal["suggested_cases"][number] = {
      id: "edit_period", task: "Report the margin for this period", failure_mode: "Ambiguous period", decision_status: "pending", covers_group_ids: [], case_spec: spec,
      edit: { case_revision_id: "aec_original", case_hash: `sha256:${"a".repeat(64)}`, case_key: "period", before: { task: "Original question", case_spec: spec }, changed_fields: ["task"] },
    };
    const suggested: AgentChangeProposal = { ...proposal, base: { ...proposal.base, suite_revision_id: "aev_1" }, suggested_cases: [edit, ...(mixed ? [{ ...edit, id: "added", task: "Additional coverage", case_spec: { ...spec, case_key: "added" }, edit: undefined }] : [])] };
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [suggested] });
    vi.mocked(acceptAgentProposalSuggestedCases).mockResolvedValue({ decision_id: "acpd_1", suite_revision_id: "aev_2" });
    const view = renderPanel({ view: "proposals" }, "en-US");
    expect(await screen.findByText("Changes to a saved test")).toBeTruthy();
    expect(view.container.querySelector("details")?.open).toBe(false);
    fireEvent.click(screen.getByText("Review or adjust tests"));
    expect(screen.getByText("Original question").closest("del")).toBeTruthy();
    expect(view.container.querySelector("ins")?.textContent).toBe("Report the margin for this period");
    const save = screen.getByRole("button", { name: mixed ? "Save these test changes" : "Update this test" });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(acceptAgentProposalSuggestedCases).toHaveBeenCalledWith("osa_1", suggested, "aev_1", expect.arrayContaining([
      expect.objectContaining({ suggestedCaseId: "edit_period", caseKey: "period", task: edit.task, caseSpec: spec }),
    ])));
    expect(applyAgentChangeProposal).not.toHaveBeenCalled();
  });

  it("does not let whole-set adoption overwrite a changed suite", async () => {
    const suggested: AgentChangeProposal = {
      ...proposal, base: { ...proposal.base, suite_revision_id: "aev_old" },
      suggested_cases: [{ id: "one", task: "Original question", failure_mode: "Requirement", decision_status: "pending", covers_group_ids: [] }],
    };
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [suggested] });
    renderPanel({ view: "proposals" }, "en-US");
    expect(await screen.findByText(/The suite changed/)).toBeTruthy();
    const add = screen.getByRole("button", { name: "Add this test" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    expect(acceptAgentProposalSuggestedCases).not.toHaveBeenCalled();
  });

  it.each([false, true])("waits for suites without presenting a conflict (initial=%s)", async (initial) => {
    let resolve!: (value: typeof manager) => void;
    vi.mocked(getAgentEvalManager).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const suggested = { ...caseProposal, base: initial ? { ...caseProposal.base, suite_revision_id: null, initial_suite_name: "New checks" } : caseProposal.base };
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [suggested] });
    renderPanel({ view: "proposals" }, "en-US");
    expect(await screen.findByText("Loading this Agent’s evaluation suites…")).toBeTruthy();
    expect(screen.queryByText(/The suite changed/)).toBeNull();
    expect(screen.queryByText(/Create one of your own suites/)).toBeNull();
    const save = screen.getByRole("button", { name: "Add this test" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(screen.getByRole("button", { name: "Dismiss these suggestions" }).hasAttribute("disabled")).toBe(false);
    await act(async () => resolve(initial ? { ...manager, suites: [] } : manager));
    await waitFor(() => expect(save.disabled).toBe(false));
    expect(screen.queryByText(/The suite changed/)).toBeNull();
    expect(acceptAgentProposalSuggestedCases).not.toHaveBeenCalled();
  });

  it("retries a failed suite read without regenerating the proposal or losing edits", async () => {
    let resolve!: (value: typeof manager) => void;
    vi.mocked(getAgentEvalManager)
      .mockRejectedValueOnce(new ApiError(503, "Private upstream detail"))
      .mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [caseProposal] });
    vi.mocked(acceptAgentProposalSuggestedCases).mockResolvedValue({ decision_id: "acpd_retry", suite_revision_id: "aev_2" });
    const view = renderPanel({ view: "proposals" }, "en-US");
    expect((await screen.findAllByText(/The evaluation suites could not be loaded/)).length).toBeGreaterThan(0);
    expect(view.container.textContent).not.toContain("Private upstream detail");
    expect(screen.queryByText(/The suite changed/)).toBeNull();
    expect((screen.getByRole("button", { name: "Add this test" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByText("Review or adjust tests"));
    fireEvent.change(screen.getByRole("textbox", { name: "Case task" }), { target: { value: "Keep my edited question" } });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Loading this Agent’s evaluation suites…")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Add this test" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => resolve(manager));
    await waitFor(() => expect((screen.getByRole("button", { name: "Add this test" }) as HTMLButtonElement).disabled).toBe(false));
    expect((screen.getByRole("textbox", { name: "Case task" }) as HTMLInputElement).value).toBe("Keep my edited question");
    fireEvent.click(screen.getByRole("button", { name: "Add this test" }));
    await waitFor(() => expect(acceptAgentProposalSuggestedCases).toHaveBeenCalledWith("osa_1", caseProposal, "aev_1", [expect.objectContaining({ suggestedCaseId: "one", task: "Keep my edited question" })]));
    expect(acceptAgentProposalSuggestedCases).toHaveBeenCalledTimes(1);
  });

  it.each([401, 403, 404])("clears case content when suite access is unavailable (%s)", async (status) => {
    vi.mocked(getAgentEvalManager).mockRejectedValueOnce(new ApiError(status, "Unavailable"));
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [caseProposal] });
    const view = renderPanel({ view: "proposals" }, "en-US");
    expect(await screen.findByText(/This Agent’s evaluation suites are no longer available to you/)).toBeTruthy();
    expect(view.container.textContent).not.toContain(caseProposal.suggested_cases[0].task);
    expect(screen.queryByRole("button", { name: "Add this test" })).toBeNull();
    expect(screen.queryByText(/The suite changed/)).toBeNull();
    expect(acceptAgentProposalSuggestedCases).not.toHaveBeenCalled();
  });

  it("keeps independent configuration proposals when suite access is unavailable", async () => {
    const cases = { ...caseProposal, proposal_id: "acp_cases_unavailable" };
    vi.mocked(getAgentEvalManager).mockRejectedValueOnce(new ApiError(403, "Unavailable"));
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [proposal, cases] });
    renderPanel({ view: "proposals" }, "en-US");
    expect(await screen.findByText(/This Agent’s evaluation suites are no longer available to you/)).toBeTruthy();
    expect(screen.getByText(proposal.rationale!)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save these changes to draft" })).toBeTruthy();
    expect(screen.queryByText(cases.suggested_cases[0].task)).toBeNull();
    expect(acceptAgentProposalSuggestedCases).not.toHaveBeenCalled();
  });

  it("does not reuse a late suite read after the target Agent changes", async () => {
    let resolveOld!: (value: typeof manager) => void;
    let resolveNew!: (value: typeof manager) => void;
    const second = { ...caseProposal, agent_id: "osa_2", proposal_id: "acp_second", suggested_cases: [{ ...caseProposal.suggested_cases[0], task: "Second Agent question" }] };
    vi.mocked(getAgentVersionWorkbench).mockImplementation(async (id) => ({ ...WORKBENCH, change_proposals: [id === "osa_1" ? caseProposal : second] }));
    vi.mocked(getAgentEvalManager).mockImplementation((id) => new Promise((done) => { if (id === "osa_1") resolveOld = done; else resolveNew = done; }));
    function SwitchTarget() {
      const [agentId, setAgentId] = useState("osa_1");
      return <><button onClick={() => setAgentId("osa_2")}>Switch Agent</button><AgentVersionWorkbenchPanel agentId={agentId} agentName="Analyst" view="proposals" workingCopy={null} onAvailability={vi.fn()} onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} /></>;
    }
    renderIntl(<SwitchTarget />, { locale: "en-US" });
    expect(await screen.findByText("Loading this Agent’s evaluation suites…")).toBeTruthy();
    await waitFor(() => expect(resolveOld).toBeTypeOf("function"));
    fireEvent.click(screen.getByRole("button", { name: "Switch Agent" }));
    expect(await screen.findByText("Second Agent question")).toBeTruthy();
    await waitFor(() => expect(resolveNew).toBeTypeOf("function"));
    await act(async () => resolveOld(manager));
    expect(screen.queryByText("Check the supplied total")).toBeNull();
    expect((screen.getByRole("button", { name: "Add this test" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => resolveNew({ ...manager, agent_id: "osa_2" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Add this test" }) as HTMLButtonElement).disabled).toBe(false));
    expect(acceptAgentProposalSuggestedCases).not.toHaveBeenCalled();
  });

  it("keeps suggested cases separate and appends edited cases to an Owner suite", async () => {
    const accepted: AgentChangeProposal = {
      ...proposal,
      proposal_id: "acp_123456789012",
      status: "accepted",
      row_version: 2,
      suggested_cases: [{ id: "case_behavior", task: "原始任务", covers_group_ids: ["behavior"], failure_mode: "遗漏约束", decision_status: "pending" }],
    };
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [accepted] });
    vi.mocked(acceptAgentProposalSuggestedCases).mockResolvedValue({ decision_id: "acpd_1", suite_revision_id: "aev_2" });
    renderPanel({ view: "proposals" });

    fireEvent.click(await screen.findByText("查看或调整用例"));
    const task = await screen.findByRole("textbox", { name: "用例任务" });
    fireEvent.change(task, { target: { value: "编辑后的任务" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "加入这 1 条用例" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "加入这 1 条用例" }));
    await waitFor(() => expect(acceptAgentProposalSuggestedCases).toHaveBeenCalledWith("osa_1", accepted, "aev_1", [{
      suggestedCaseId: "case_behavior",
      caseKey: "coach.123456789012.case_behavior",
      task: "编辑后的任务",
      failureMode: "遗漏约束",
    }]));
    expect(screen.queryByRole("button", { name: "将这组修改保存到草稿" })).toBeNull();
  });
});

describe("pendingProposals", () => {
  const base = { suggested_cases: [], status: "accepted" } as unknown as AgentChangeProposal;
  it("keeps CI and feedback proposals and drops Coach writes that need nothing more", () => {
    const git = { ...base, proposal_id: "acp_git", source_type: "git" } as AgentChangeProposal;
    const coach = { ...base, proposal_id: "acp_coach", source_type: "coach" } as AgentChangeProposal;
    const legacyCoach = { ...coach, proposal_id: "acp_open", status: "open" } as AgentChangeProposal;
    const coachCases = { ...coach, proposal_id: "acp_cases", suggested_cases: [{ id: "c1", decision_status: "pending" }] } as unknown as AgentChangeProposal;
    expect(pendingProposals([git, coach, legacyCoach, coachCases]).map((item) => item.proposal_id)).toEqual(["acp_git", "acp_open", "acp_cases"]);
  });
});

describe("AgentVersionWorkbenchPanel · canvas", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("puts Coach's changes in the slot above the configuration and keeps the release check in place", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      draft_changes: [{
        path: "/definition/system_prompt", live: "Old.", draft: "Coach.", source: "coach",
        coach: { state: "applied", proposal_id: "acp_1", group_ids: ["g0"], before: "Old.", value: "Coach.", conflict: false },
      }],
    });
    const slot = document.createElement("div");
    document.body.appendChild(slot);
    const { container } = renderPanel({ view: "canvas", coachSlot: slot });
    expect(await within(slot).findByRole("heading", { name: "Coach 的修改" })).toBeTruthy();
    expect(within(container).queryByRole("heading", { name: "Coach 的修改" })).toBeNull();
    // The canvas lists Coach's changes itself, so the release check leaves out the change summary.
    expect(screen.queryByRole("heading", { name: /^本次改动/ })).toBeNull();
    expect(container.querySelector(".am-ci-card")).toBeTruthy();
    slot.remove();
  });
});

describe("AgentVersionWorkbenchPanel · CI review", () => {
  const ciProposal: AgentChangeProposal = {
    kind: "agent_change_proposal",
    schema_version: 1,
    proposal_id: "acp_git",
    agent_id: "osa_1",
    source_type: "git",
    source_ref: "acme/agent@0123456789abcdef",
    rationale: "Lead with the conclusion",
    status: "open",
    row_version: 1,
    created_at: "2026-10-18T08:00:00Z",
    decided_at: null,
    operations: [
      { op: "replace", path: "/definition/system_prompt", value: "Lead with the answer.\nCite sources." },
      { op: "add", path: "/definition/tool_refs", value: ["bi.query", "web.search"] },
      { op: "add", path: "/definition/temperature_hint", value: 0.2 },
    ],
    atomic_groups: [
      { id: "prompt", operation_indexes: [0], depends_on: [] },
      { id: "tools", operation_indexes: [1, 2], depends_on: ["prompt"] },
    ],
    suggested_cases: [],
    base: { release_id: "arl_1", working_copy_id: "awc_1", working_copy_row_version: 3 },
    origin: { repository: "acme/agent", ref: "refs/heads/main", commit_sha: "0123456789abcdef", grant_id: "avag_1", key_name: "ci-prod" },
  };
  const workingCopy = {
    id: "awc_1",
    row_version: 3,
    definition: { system_prompt: "Answer.\nCite sources.", tool_refs: ["bi.query", "kb.search"] },
    listing: {},
    audience: { effective_mode: "private" },
  } as never;
  const blockedCaseChanges: AgentVersionCaseChanges = {
    kind: "agent_version_case_changes", schema_version: 1, execution_id: "aex_9",
    snapshot: { id: "aevs_9", hash: "sha256:s", created_at: "2026-10-18T08:30:00Z" },
    baseline: { release_id: "arl_1", release_sequence: 3 }, candidate: { candidate_id: "arc_1", release_sequence: null },
    summary: { case_count: 5, worse: 2, unstable: 0, better: 1, not_comparable: 0, unchanged_required_failures: 1 },
    items: [
      { suite_revision_id: "aev_1", suite_name: "Quarterly Q&A", case_revision_id: "aec_1", case_key: "q.1", case_label: "Channel margin ranking", change: "required_failure", classification: "unchanged_fail", reason_codes: [], key_case: false, baseline: { passed: 0, runs: 3 }, candidate: { passed: 0, runs: 3 } },
      { suite_revision_id: "aev_1", suite_name: "Quarterly Q&A", case_revision_id: "aec_2", case_key: "q.2", case_label: "Region growth", change: "worse", classification: "regressed", reason_codes: [], key_case: false, baseline: { passed: 3, runs: 3 }, candidate: { passed: 1, runs: 3 } },
      { suite_revision_id: "aev_2", suite_name: "New suite", case_revision_id: "aec_3", case_key: "n.1", case_label: "Fresh case", change: "worse", classification: "regressed", reason_codes: [], key_case: false, baseline: null, candidate: { passed: 0, runs: 3 } },
      { suite_revision_id: "aev_1", suite_name: "Quarterly Q&A", case_revision_id: "aec_4", case_key: "q.4", case_label: "Top product", change: "better", classification: "improved", reason_codes: [], key_case: false, baseline: { passed: 1, runs: 3 }, candidate: { passed: 3, runs: 3 } },
    ],
    next_offset: null,
  };
  const blockedWorkbench: AgentVersionWorkbench = {
    ...WORKBENCH,
    display_state: "blocked",
    readiness: "blocked",
    reason_codes: ["required_quality_failed"],
    // The comparison finished: the summary is gone, and only the snapshot names its execution.
    active_execution_summary: null,
    active_snapshot_identity: { id: "aevs_9", hash: "sha256:s", created_at: "2026-10-18T08:30:00Z", execution_id: "aex_9" },
    primary_action: { kind: "fix_next_draft", enabled: true, disabled_reason_codes: ["required_quality_failed"] },
  };

  beforeEach(() => {
    vi.mocked(getAgentComparisonEstimate).mockRejectedValue(new ApiError(404, "off"));
    vi.mocked(getAgentEvalManager).mockResolvedValue({
      kind: "agent_eval_manager", schema_version: 1, agent_id: "osa_1", target: { kind: "live" },
      suites: [], runs: [], trigger_policy: null, release_gate: { required: true, ready: false, reason: "missing" },
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    resetCiMerges();
  });

  it("lists the failing cases after the comparison completes, with each case's Live baseline label", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(blockedWorkbench);
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue(blockedCaseChanges);
    renderPanel({}, "en-US");

    const table = await screen.findByRole("table");
    expect(getAgentVersionCaseChanges).toHaveBeenCalledWith("osa_1", "arc_1", "aex_9");
    const row = (name: string) => within(table).getByRole("button", { name }).closest("tr")!;
    expect(row("Channel margin ranking").textContent).toContain("Also failing on the released version");
    expect(row("Region growth").textContent).toContain("New in this change");
    expect(row("Fresh case").textContent).toContain("Not run on the released version");
    expect(row("Top product").textContent).not.toMatch(/on the released version|this candidate/);
  });

  it("shows where a CI proposal came from, and says so when its key was deleted", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      change_proposals: [ciProposal, { ...ciProposal, proposal_id: "acp_git_2", origin: { ...ciProposal.origin!, ref: "refs/tags/v2", key_name: null } }],
    });
    renderPanel({ view: "proposals", workingCopy }, "en-US");

    const origins = await screen.findAllByLabelText("Origin");
    expect(origins[0].textContent).toBe("acme/agentmain0123456Key ci-prod");
    expect(origins[1].textContent).toBe("acme/agentrefs/tags/v20123456Deleted key");
  });

  it("previews each atomic group against the loaded draft", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, change_proposals: [ciProposal] });
    const view = renderPanel({ view: "proposals", workingCopy }, "en-US");

    await screen.findByText("Lead with the conclusion");
    const groups = view.container.querySelectorAll(".am-ci-group");
    expect(groups).toHaveLength(2);
    // Text: line diff of the instructions, labelled by the checkbox (one known field).
    const prompt = groups[0];
    expect(prompt.querySelector("code")?.textContent).toBe("prompt");
    expect([...prompt.querySelectorAll("del")].map((node) => node.textContent)).toEqual(["Answer."]);
    expect([...prompt.querySelectorAll("ins")].map((node) => node.textContent)).toEqual(["Lead with the answer."]);
    expect(prompt.textContent).toContain("Cite sources.");
    // List: items keyed by name; an unknown field is shown by its raw path.
    const tools = groups[1];
    expect(within(tools as HTMLElement).getByText("+ web.search")).toBeTruthy();
    expect(within(tools as HTMLElement).getByText("− kb.search")).toBeTruthy();
    expect(within(tools as HTMLElement).getByText("/definition/temperature_hint")).toBeTruthy();
    expect(within(tools as HTMLElement).getByText("0.2")).toBeTruthy();
  });

  it("keeps a merged CI proposal as Merged with the next step until CI's candidate appears", async () => {
    const onOpenRelease = vi.fn();
    vi.mocked(applyAgentChangeProposal).mockResolvedValue({ status: "accepted" });
    vi.mocked(getAgentVersionWorkbench)
      .mockResolvedValueOnce({ ...WORKBENCH, change_proposals: [ciProposal] })
      // After the merge the workbench lists only open proposals, and the old candidate is still there.
      .mockResolvedValue({ ...WORKBENCH, change_proposals: [] });

    function Harness() {
      const [view, setView] = useState<"proposals" | "validation">("proposals");
      return <>
        <button type="button" onClick={() => setView("validation")}>to release</button>
        <button type="button" onClick={() => setView("proposals")}>to config</button>
        <AgentVersionWorkbenchPanel
          agentName="Revenue Analyst" agentId="osa_1" view={view} workingCopy={workingCopy}
          onAvailability={vi.fn()}
          onChanged={vi.fn().mockResolvedValue(undefined)} onError={vi.fn()} onOpenRelease={onOpenRelease}
        />
      </>;
    }
    renderIntl(<Harness />, { locale: "en-US" });

    fireEvent.click(await screen.findByRole("button", { name: "Save these changes to draft" }));
    await waitFor(() => expect(getAgentVersionWorkbench).toHaveBeenCalledTimes(2));
    const card = (await screen.findByText("Lead with the conclusion")).closest("article")!;
    expect(within(card).getByText("Merged")).toBeTruthy();
    // The Owner page names the external pipeline and the Release tab; it never says "CI" or "candidate".
    expect(card.textContent).toContain("The external pipeline usually tests it next");
    expect(card.textContent).not.toMatch(/\bCI\b|candidate/);
    expect(within(card).queryByRole("button", { name: "Save these changes to draft" })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Test on the Release tab" }));
    expect(onOpenRelease).toHaveBeenCalledOnce();

    // Release shows the timeline waiting for CI.
    fireEvent.click(screen.getByRole("button", { name: "to release" }));
    const timeline = await screen.findByRole("list", { name: "Test progress" });
    expect(timeline.textContent).toContain("Waiting for the external pipeline to start the test");

    // CI prepares a new candidate; the next refresh (tab activation) settles the card.
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      candidate_identity: { ...WORKBENCH.candidate_identity!, candidate_id: "arc_2" },
      change_proposals: [],
    });
    fireEvent.click(screen.getByRole("button", { name: "to config" }));
    await waitFor(() => expect(screen.queryByText("Lead with the conclusion")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "to release" }));
    await waitFor(() => expect(screen.getByRole("list", { name: "Test progress" }).textContent).not.toContain("Waiting for the external pipeline"));
  });

  it("doesn't call a required case that fails on both versions a regression", async () => {
    const onOpenRelease = vi.fn();
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(blockedWorkbench);
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue({
      ...blockedCaseChanges,
      summary: { case_count: 1, worse: 0, unstable: 0, better: 0, not_comparable: 0, unchanged_required_failures: 1 },
      items: blockedCaseChanges.items.filter((item) => item.change === "required_failure"),
    });
    renderPanel({ view: "evaluate", onOpenRelease }, "en-US");

    const card = (await screen.findByRole("heading", { name: /can't be released yet/ })).closest("section")!;
    await waitFor(() => expect(card.textContent).toContain("No case got worse: 1 required case fails on both this change and v3."));
    expect(within(card).queryByRole("button", { name: "See what got worse" })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "See the failed cases" }));
    expect(onOpenRelease).toHaveBeenCalledOnce();
  });

  it("shows the same test card on Evaluate, with the details left on Release", async () => {
    const onOpenRelease = vi.fn();
    const onAskCoach = vi.fn();
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue(blockedWorkbench);
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue(blockedCaseChanges);
    const view = renderPanel({ view: "evaluate", onOpenRelease, onAskCoach }, "en-US");

    const card = (await screen.findByRole("heading", { name: /can't be released yet|got worse/ })).closest("section")!;
    expect(card.getAttribute("data-surface")).toBe("evaluate");
    // No rail or case table here; they stay on Release.
    expect(screen.queryByRole("list", { name: "Versions" })).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    // Cases got worse here (the case changes say so once they load), so the button says that.
    fireEvent.click(await within(card).findByRole("button", { name: "See what got worse" }));
    expect(onOpenRelease).toHaveBeenCalledOnce();
    fireEvent.click(within(card).getByRole("button", { name: "Ask Coach to fix it" }));
    expect(onAskCoach).toHaveBeenCalledOnce();
    expect(view.container.textContent).not.toMatch(/candidate/i);
  });
});
