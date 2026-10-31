import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  approveAgentReleaseCandidate,
  getAgentReleaseCandidate,
  rejectAgentReleaseCandidate,
  type AgentReleaseCandidateDetail,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { AgentReleaseReview } from "./agent-release-review";

vi.mock("@/lib/agents-api", () => ({
  approveAgentReleaseCandidate: vi.fn(),
  getAgentReleaseCandidate: vi.fn(),
  rejectAgentReleaseCandidate: vi.fn(),
  withdrawAgentReleaseCandidate: vi.fn(),
}));

const CANDIDATE: AgentReleaseCandidateDetail = {
  kind: "agent_release_candidate_detail",
  schema_version: 1,
  candidate_id: "arc_1",
  candidate_hash: `sha256:${"1".repeat(64)}`,
  candidate_row_version: 1,
  state: "pending",
  agent: { id: "osa_1", display_name: "Finance Helper", owner_user_id: "owner_1" },
  base: { release_id: "arl_1", release_sequence: 1, revision_id: "arv_1" },
  candidate: {
    revision_id: "arv_2",
    change_kind: "authored",
    listing: { display_name: "Finance Helper", description: "Explains monthly performance." },
    behavior: {
      model: null,
      max_iterations: 8,
      has_output_contract: false,
      tool_count: 2,
      skill_count: 1,
      requested_primary_capability_ref: "cap_report@1",
      primary_capability_ref: "cap_report@1",
      primary_capability_name: "撰写报告",
      primary_capability_name_en: "Write reports",
      verification_state: "reviewed",
      eligible_role_ids: ["knowledge"],
      output_contract_refs: ["report.document.v1"],
    },
    audience: { mode: "teamwide", user_count: 0, group_count: 0 },
    risk: { level: "high", reasons: ["external_write"] },
    safety: {
      status: "review_required",
      deterministic: { status: "pass", reason_codes: [] },
      semantic: { status: "warning", reason_codes: ["semantic_scanner_unavailable"] },
      summary: { status: "review_required", reason_codes: ["semantic_scanner_unavailable"] },
    },
  },
  diff: {
    listing_fields: ["description"],
    instructions_changed: true,
    model_changed: false,
    output_contract_changed: false,
    tool_set_changed: true,
    skill_set_changed: false,
    primary_capability_changed: true,
    behavior_changed: true,
    dependency_changed: true,
    capability_changed: true,
    risk_changed: true,
    audience: {
      before: { mode: "private", user_count: 0, group_count: 0 },
      after: { mode: "teamwide", user_count: 0, group_count: 0 },
      changed: true,
      expanded: true,
    },
  },
  release_note: "Adds approved CRM write access.",
  submitted_by: "owner_1",
  submitted_at: "2026-09-03T00:00:00Z",
  decision: null,
  actions: { approve: true, reject: true, withdraw: false },
};

describe("AgentReleaseReview", () => {
  beforeEach(() => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue(CANDIDATE);
    vi.mocked(approveAgentReleaseCandidate).mockResolvedValue({
      kind: "agent_publication_result",
      schema_version: 1,
      state: "activated",
      agent_id: "osa_1",
      candidate_id: "arc_1",
      revision_id: "arv_2",
      release_id: "arl_2",
      candidate_row_version: 2,
      aggregate_row_version: 5,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows a safe behavioral diff and approves the exact immutable candidate", async () => {
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);

    expect(await screen.findByRole("heading", { level: 1, name: "Finance Helper" })).toBeTruthy();
    expect(screen.getByText("智能体工作指令已修改")).toBeTruthy();
    expect(screen.getByText("Tool 集合已修改")).toBeTruthy();
    expect(screen.getByText("对使用者承诺的主职责已修改")).toBeTruthy();
    fireEvent.click(screen.getByText("查看运行配置详情"));
    expect(screen.getByText("撰写报告")).toBeTruthy();
    expect(screen.getByText("cap_report@1")).toBeTruthy();
    expect(screen.queryByText(/system_prompt/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "批准并发布" }));

    await waitFor(() => expect(approveAgentReleaseCandidate).toHaveBeenCalledWith(CANDIDATE));
    expect(await screen.findByText("候选已批准并发布")).toBeTruthy();
  });

  it("asks for a rejection reason only after declining and cancels without submitting", async () => {
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);
    const decline = await screen.findByRole("button", { name: "不批准此次发布…" });
    expect(screen.queryByRole("combobox", { name: "拒绝原因" })).toBeNull();
    expect(screen.queryByText("指定成员 0 人 · 群组 0 个")).toBeNull();
    expect(screen.getByText("查看运行配置详情").closest("details")?.open).toBe(false);
    fireEvent.click(decline);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("combobox", { name: "拒绝原因" })).toBeTruthy();
    expect(rejectAgentReleaseCandidate).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "返回继续审核" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(rejectAgentReleaseCandidate).not.toHaveBeenCalled();
  });

  it("submits the selected reason for the exact candidate and closes on success", async () => {
    vi.mocked(rejectAgentReleaseCandidate).mockResolvedValue({
      kind: "agent_publication_result", schema_version: 1, state: "rejected",
      agent_id: "osa_1", revision_id: "arv_2", release_id: null, aggregate_row_version: 5,
      candidate_id: "arc_1", candidate_row_version: 2,
    });
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "不批准此次发布…" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("combobox", { name: "拒绝原因" }), { target: { value: "insufficient_evidence" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认不批准" }));
    await waitFor(() => expect(rejectAgentReleaseCandidate).toHaveBeenCalledWith(CANDIDATE, "insufficient_evidence"));
    expect(await screen.findByText("候选已拒绝")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("button", { name: "不批准此次发布…" })).toBeNull();
  });

  it("keeps the reason and shows the failure inside the rejection dialog", async () => {
    vi.mocked(rejectAgentReleaseCandidate).mockRejectedValueOnce(new Error("The review service is unavailable. Try again later."));
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "不批准此次发布…" }));
    const dialog = await screen.findByRole("dialog");
    const reason = within(dialog).getByRole("combobox", { name: "拒绝原因" }) as HTMLSelectElement;
    fireEvent.change(reason, { target: { value: "audience_too_broad" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认不批准" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("The review service is unavailable.");
    expect(reason.value).toBe("audience_too_broad");
    expect((within(dialog).getByRole("button", { name: "确认不批准" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it.each(["zh-CN", "en-US", "de-DE", "fr-FR"] as const)("translates release readiness and preserves decision availability in %s", async (locale) => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue({ ...CANDIDATE, evaluation: {
      schema_version: 2, actor_overlap: false, candidate_hash: CANDIDATE.candidate_hash,
      candidate_id: "arc_1", candidate_label: "Candidate", decision_availability: { approve: true, reject: false, disabled_reason_codes: [] },
      override: null, plan_hash: "plan-hash", plan_id: "aep_1", readiness: "ready_with_risks",
      reviewer_evidence: {}, snapshot_created_at: CANDIDATE.submitted_at, snapshot_hash: "snapshot-hash",
      snapshot_id: "aes_1", submitted_at: CANDIDATE.submitted_at,
    } });
    const { container } = renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />, { locale });
    await screen.findByRole("heading", { level: 1, name: "Finance Helper" });
    const gate = container.querySelector(".am-eval-gate");
    expect(gate?.textContent).not.toContain("ready_with_risks");
    expect(gate?.getAttribute("data-ready")).toBe("neutral");
    expect((container.querySelector(".am-danger-action") as HTMLButtonElement).disabled).toBe(true);
    expect((container.querySelector(".am-action-primary") as HTMLButtonElement).disabled).toBe(false);
  });

  it("lists concrete resource changes and Plugin updates", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue({
      ...CANDIDATE,
      plugin_updates: [{ kind: "team", id: "tcap_1", name: "门店经营分析包", release_version: 2, current_version: 3, changed_at: null }],
      diff: {
        ...CANDIDATE.diff,
        plugin_items: [
          {
            kind: "builtin",
            id: "builtin:reports-canvas",
            name: "reports-canvas",
            listing: { "zh-CN": { name: "报告与画布", description: "" } },
            version: 1,
            tools: ["write_report", "add_chart"],
            change: "added",
          },
        ],
        tool_changes: { added: [], removed: ["web_search"] },
        skill_changes: { added: ["kpi-method"], removed: [] },
      },
    });
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);
    expect(await screen.findByText("报告与画布")).toBeTruthy();
    expect(screen.getByText("内置插件 · v1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "带来 2 个工具" }));
    expect(screen.getByText("写入报告、添加图表")).toBeTruthy();
    expect(screen.getByText("kpi-method")).toBeTruthy();
    expect(screen.getByText(/所用插件“门店经营分析包”已从 v2 更新到 v3。/)).toBeTruthy();
  });

  it("lists Skills updated since the candidate was prepared next to Plugin updates", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue({
      ...CANDIDATE,
      skill_updates: [
        { name: "Sales report", identity: "team:sk_1", release_revision: `sha256:${"1".repeat(64)}`, current_revision: `sha256:${"2".repeat(64)}`, changed_at: null },
        { name: "Forecast", identity: "team:sk_2", release_revision: `sha256:${"3".repeat(64)}`, current_revision: `sha256:${"4".repeat(64)}`, changed_at: "2026-10-16T00:00:00Z" },
      ],
    });
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);
    const notice = await screen.findByText(/所用技能已更新：/);
    expect(notice.textContent).toContain("Sales report");
    expect(notice.textContent).toContain("Forecast");
    expect(notice.textContent).toContain("建议重新评测。");
  });

  it("names the blocked Skill when approval is refused", async () => {
    vi.mocked(approveAgentReleaseCandidate).mockRejectedValueOnce(new ApiError(422, "Blocked", {
      code: "agent_dependency_blocked",
      details: { blocked_items: [{ kind: "skill", name: "Sales report", identity: "team:sk_1", reason_codes: ["skill_not_approved"] }] },
    }));
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "批准并发布" }));
    expect((await screen.findByRole("alert")).textContent).toContain("技能「Sales report」：尚未通过审核。");
  });

  it("reports optional Skill scope changes separately from default Skill grants", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue({ ...CANDIDATE,
      diff: { ...CANDIDATE.diff, runtime_extensions_changed: true, skill_set_changed: false } });
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);
    expect(await screen.findByText("临时只读 Skill 使用范围已修改")).toBeTruthy();
    expect(screen.queryByText("Skill 集合或精确版本已修改")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "批准并发布" }));
    await waitFor(() => expect(approveAgentReleaseCandidate).toHaveBeenCalledWith(expect.objectContaining({
      candidate_hash: CANDIDATE.candidate_hash, candidate_id: CANDIDATE.candidate_id,
    })));
  });

  it("shows source broadening in the original release review", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue({ ...CANDIDATE,
      diff: { ...CANDIDATE.diff, knowledge_access_changed: true, knowledge_access_expanded: true } });
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);
    expect(await screen.findByText("知识来源已变更")).toBeTruthy();
    expect(screen.getByText("知识来源范围已扩大；每位使用者仍须拥有文件访问权限")).toBeTruthy();
  });

  it("shows an already-decided candidate without offering a second decision", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue({
      ...CANDIDATE,
      state: "rejected",
      actions: { approve: false, reject: false, withdraw: false },
    });

    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);

    expect(await screen.findByText("候选已拒绝")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "批准并发布" })).toBeNull();
  });

  it("does not offer approval while this candidate's contract evidence is missing", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue({
      ...CANDIDATE,
      evaluation: {
        release_gate: { required: true, ready: false, reason: "platform_contract_not_current" },
        suites: [],
      },
    });

    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);

    expect(await screen.findByText("审批前需要完成评测")).toBeTruthy();
    // Only the Agent's owner can run an eval, so telling the reviewer "an evaluation
    // is required" without saying whose move it is leaves them with nothing to do.
    expect(screen.getByText(/评测只能由该智能体的负责人运行/)).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "批准并发布" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it.each([
    { code: "agent_eval_required", text: /只有该智能体的负责人能运行评测/ },
    { code: "agent_eval_critical_failure", text: /出现了致命失败/ },
    { code: "agent_eval_platform_suite_missing", text: /套件会自动装好/ },
    { code: "qualified_reviewer_unavailable", text: /这个团队目前没有可审批的人/ },
  ])("names the remedy when the server refuses the approval with $code", async ({ code, text }) => {
    // The approve button is disabled from `evaluation.release_gate.ready`, read at
    // load time — and it is absent entirely while Agent Eval is off. Either way the
    // server's refusal is all the reviewer has, and it must not degrade to
    // "the state changed, refresh the queue".
    vi.mocked(approveAgentReleaseCandidate).mockRejectedValue(
      new ApiError(422, code, { code }),
    );

    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "批准并发布" }));

    expect(await screen.findByText(text)).toBeTruthy();
  });

  it("shows the ready gate without the owner hand-off once evidence is current", async () => {
    // Default-on Agent Eval makes this the *normal* reviewer view, and nothing
    // covered it: the whole `ready === true` branch could be deleted and the suite
    // stayed green.
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue({
      ...CANDIDATE,
      evaluation: {
        release_gate: { required: true, ready: true, reason: null },
        suites: [],
      },
    });

    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);

    // With no Owner cases the reviewer is told so instead of "evidence is ready".
    expect(await screen.findByText("Owner 没有提供用例")).toBeTruthy();
    expect(screen.queryByText("发布证据已就绪")).toBeNull();
    expect(screen.queryByText(/评测只能由该智能体的负责人运行/)).toBeNull();
    expect(
      (screen.getByRole("button", { name: "批准并发布" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });
});
