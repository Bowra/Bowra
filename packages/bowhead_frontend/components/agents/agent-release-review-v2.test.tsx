import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  approveAgentReleaseCandidate,
  getAgentReleaseCandidate,
  withdrawAgentReleaseCandidate,
  type AgentReleaseCandidateDetail,
} from "@/lib/agents-api";
import { setAgentReleaseBarCapabilityForTest } from "@/lib/use-agent-release-bar-capability";
import { AgentReleaseReview } from "./agent-release-review";
import { isFirstShare, nextReleaseVersion, reviewChangeLines } from "./agent-release-review-v2";

vi.mock("@/lib/agents-api", () => ({
  approveAgentReleaseCandidate: vi.fn(),
  getAgentReleaseCandidate: vi.fn(),
  rejectAgentReleaseCandidate: vi.fn(),
  withdrawAgentReleaseCandidate: vi.fn(),
}));
const toast = vi.hoisted(() => vi.fn());
vi.mock("@/lib/use-toast", () => ({ toast }));

const PRIVATE = { mode: "private" as const, user_count: 0, group_count: 0 };
const ONE_MEMBER = { mode: "custom" as const, user_count: 1, group_count: 0 };

function candidate(overrides: Partial<AgentReleaseCandidateDetail> = {}, diff: Partial<AgentReleaseCandidateDetail["diff"]> = {}): AgentReleaseCandidateDetail {
  return {
    kind: "agent_release_candidate_detail",
    schema_version: 1,
    candidate_id: "arc_1",
    candidate_hash: `sha256:${"1".repeat(64)}`,
    candidate_row_version: 1,
    state: "pending",
    agent: { id: "osa_1", display_name: "门店周报", owner_user_id: "owner_1" },
    base: { release_id: "arl_2", release_sequence: 2, revision_id: "arv_2" },
    candidate: {
      revision_id: "arv_3",
      change_kind: "authored",
      listing: { display_name: "门店周报", description: "查看门店的周销售。" },
      behavior: {
        model: null, max_iterations: 8, has_output_contract: false, tool_count: 4, skill_count: 0,
        requested_primary_capability_ref: null, primary_capability_ref: null, primary_capability_name: null, primary_capability_name_en: null,
        verification_state: "reviewed", eligible_role_ids: [], output_contract_refs: [],
      },
      audience: ONE_MEMBER,
      risk: { level: "low", reasons: [] },
      safety: {
        status: "pass",
        deterministic: { status: "pass", reason_codes: [] },
        semantic: { status: "unavailable", reason_codes: ["semantic_scanner_unavailable"] },
        summary: { status: "pass", reason_codes: [] },
      },
    },
    diff: {
      listing_fields: [],
      instructions_changed: false,
      model_changed: false,
      output_contract_changed: false,
      tool_set_changed: false,
      skill_set_changed: false,
      primary_capability_changed: false,
      behavior_changed: false,
      dependency_changed: false,
      capability_changed: false,
      risk_changed: false,
      audience: { before: PRIVATE, after: ONE_MEMBER, changed: true, expanded: true },
      ...diff,
    },
    release_note: "共享给 dogfood 测试账号",
    submitted_by: "owner_1",
    submitted_at: "2026-11-24T09:47:00Z",
    decision: null,
    actions: { approve: true, reject: true, withdraw: true },
    ...overrides,
  };
}

describe("reviewer page helpers", () => {
  it("detects a first share from a missing base or a private one", () => {
    expect(isFirstShare(candidate())).toBe(true);
    expect(isFirstShare(candidate({ base: null }))).toBe(true);
    expect(isFirstShare(candidate({}, { audience: { before: ONE_MEMBER, after: { ...ONE_MEMBER, user_count: 2 }, changed: true, expanded: true } }))).toBe(false);
    expect(isFirstShare(candidate({}, { audience: { before: PRIVATE, after: PRIVATE, changed: false, expanded: false } }))).toBe(false);
    expect(nextReleaseVersion(candidate())).toBe(3);
    expect(nextReleaseVersion(candidate({ base: null }))).toBe(1);
  });

  it("states one line per served diff item and marks wider scopes", () => {
    const lines = reviewChangeLines(candidate({}, {
      listing_fields: ["description", "tags"], instructions_changed: true, plugin_set_changed: true, knowledge_access_changed: true, knowledge_access_expanded: true,
    }).diff);
    expect(lines.filter((line) => line.changed).map((line) => line.key)).toEqual(["audience", "description", "listing", "instructions", "uses", "knowledge"]);
    expect(lines.filter((line) => line.expanded).map((line) => line.key)).toEqual(["audience", "knowledge"]);
  });
});

describe("AgentReleaseReview with the release bar switch on", () => {
  beforeEach(() => {
    setAgentReleaseBarCapabilityForTest(true);
    vi.mocked(approveAgentReleaseCandidate).mockResolvedValue({
      kind: "agent_publication_result", schema_version: 1, state: "activated", agent_id: "osa_1", candidate_id: "arc_1",
      revision_id: "arv_3", release_id: "arl_3", candidate_row_version: 2, aggregate_row_version: 5,
    });
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    setAgentReleaseBarCapabilityForTest(null);
  });

  it("asks for a review of the whole configuration on a first share and lists only served facts", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue(candidate());
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);

    expect(await screen.findByRole("heading", { level: 1, name: "审批：门店周报" })).toBeTruthy();
    expect(screen.getByRole("note").textContent).toContain("第一次共享：请审全部配置。");
    expect(screen.getByRole("link", { name: "团队审批" })).toBeTruthy();
    const what = screen.getByRole("region", { name: "这次改了什么" });
    expect(within(what).getByText("提交说明：「共享给 dogfood 测试账号」")).toBeTruthy();
    expect(within(what).getByText(/仅自己 → 指定成员 1 人/)).toBeTruthy();
    expect(within(what).getByText("范围扩大")).toBeTruthy();
    expect(within(what).getByText(/和 v2 一样（名称、一句话介绍、工作说明、模型、主职责、能用什么、知识来源、结构化输出都没改）/)).toBeTruthy();
    const full = screen.getByText("完整配置（v3）").closest("details")!;
    expect(full.open).toBe(true);
    expect(within(full).getByText("和 v2 一样 · 审批页不显示原文")).toBeTruthy();
    expect(within(full).getByText("4 个工具 · 0 个技能")).toBeTruthy();
    expect(screen.queryByText(/system_prompt/)).toBeNull();
  });

  it("orders the decision panel by severity and offers withdraw as a link for the Owner", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue(candidate());
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);

    const panel = await screen.findByRole("region", { name: "你的决定" });
    const labels = within(panel).getAllByRole("term").map((term) => term.textContent);
    expect(labels).toEqual(["要你看的", "回答质量", "风险判断", "自动语义复核"]);
    expect(within(panel).getByText("第一次共享 · 范围扩大")).toBeTruthy();
    expect(within(panel).getByText("没有测试结果")).toBeTruthy();
    expect(within(panel).getByText("第一次共享：按完整配置判断")).toBeTruthy();
    expect(within(panel).getByText("这次没有，只能靠你判断")).toBeTruthy();
    const buttons = within(panel).getAllByRole("button").map((button) => button.textContent);
    expect(buttons).toEqual(["批准并发布 v3", "不批准…", "撤回这次提交"]);
    expect(screen.getByText(/你也是这个智能体的所有者/)).toBeTruthy();

    vi.mocked(withdrawAgentReleaseCandidate).mockResolvedValue({
      kind: "agent_publication_result", schema_version: 1, state: "withdrawn", agent_id: "osa_1", candidate_id: "arc_1",
      revision_id: "arv_3", release_id: null, candidate_row_version: 2, aggregate_row_version: 5,
    });
    fireEvent.click(within(panel).getByRole("button", { name: "撤回这次提交" }));
    // Withdrawing ends the review and needs a new test, so it asks first.
    const confirmDialog = await screen.findByRole("dialog", { name: "撤回这次提交？" });
    expect(withdrawAgentReleaseCandidate).not.toHaveBeenCalled();
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "撤回提交" }));
    expect(await within(panel).findByText("已撤回这次提交。草稿留在所有者那里，要再提交得先重新测试。")).toBeTruthy();
  });

  it("shows the approval result in the panel, moves focus there and copies the Agent link", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue(candidate({ actions: { approve: true, reject: true, withdraw: false } }));
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "批准并发布 v3" }));
    await waitFor(() => expect(approveAgentReleaseCandidate).toHaveBeenCalled());
    const result = await screen.findByText(/已批准，v3 现在对 1 位成员可见/);
    await waitFor(() => expect(document.activeElement).toBe(result.closest("[role=status]")));
    expect(result.textContent).toContain("还没聘用的人收不到通知，要在「分享给我的」里聘用");
    expect(screen.queryByRole("button", { name: "批准并发布 v3" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(expect.stringContaining("/agents/detail/?id=osa_1")));
    expect(toast).toHaveBeenCalledWith({ kind: "success", text: "已复制链接" });
  });

  it("folds the full configuration for a later shared release and states what changed", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue(candidate({}, {
      instructions_changed: true,
      audience: { before: ONE_MEMBER, after: ONE_MEMBER, changed: false, expanded: false },
    }));
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);

    const what = await screen.findByRole("region", { name: "这次改了什么" });
    expect(screen.queryByRole("note")).toBeNull();
    expect(within(what).getByText("工作说明").nextElementSibling?.textContent).toBe("改了");
    expect(screen.getByText("完整配置（v3）").closest("details")!.open).toBe(false);
    expect(within(screen.getByRole("region", { name: "你的决定" })).getByText("风险：低")).toBeTruthy();
  });

  it("says the member list changed when a member was swapped and the counts match", async () => {
    vi.mocked(getAgentReleaseCandidate).mockResolvedValue(candidate({}, {
      audience: { before: ONE_MEMBER, after: ONE_MEMBER, changed: true, expanded: true },
    }));
    renderIntl(<AgentReleaseReview agentId="osa_1" candidateId="arc_1" />);
    const what = await screen.findByRole("region", { name: "这次改了什么" });
    expect(within(what).getByText(/指定成员 1 人，名单有变化/)).toBeTruthy();
    expect(within(what).queryByText(/指定成员 1 人 → 指定成员 1 人/)).toBeNull();
  });
});
