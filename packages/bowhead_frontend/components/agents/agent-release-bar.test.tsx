import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState, type ComponentProps } from "react";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import {
  getAgentComparisonEstimate,
  getAgentEvalManager,
  getAgentVersionCaseChanges,
  getAgentVersionWorkbench,
  testAgentChange,
  type AgentVersionWorkbench,
} from "@/lib/agents-api";
import { AgentReleaseBar } from "./agent-release-bar";
import { AgentTrialLayoutContext } from "./agent-trial-layout";
import type { ReleaseBarHost } from "./agent-version-validation";
import { AgentVersionWorkbenchPanel } from "./agent-version-workbench";
import { releaseBarState } from "./release-bar-state";

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({
    activeId: "ws_1",
    items: [{ id: "ws_1", name: "华东经营分析" }],
    loading: false,
    error: null,
    setActiveId: vi.fn(),
    refresh: vi.fn(),
  }),
}));
vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));
vi.mock("@/lib/use-can-configure-models", () => ({
  JUDGE_MODEL_SETTINGS_PATH: "/team/settings/models/assignments/",
  useCanConfigureModels: () => false,
}));
vi.mock("@/lib/coach-ci-api", () => ({
  listCoachCI: vi.fn(async () => {
    throw Object.assign(new Error("off"), { status: 404 });
  }),
}));
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

const PRIVATE_DRAFT: AgentVersionWorkbench = {
  schema_version: 1,
  current_eval_policy_revision_id: "aepr_1",
  live_identity: {
    release_id: "arl_3",
    release_sequence: 3,
    revision_id: "arv_3",
  },
  candidate_identity: null,
  next_draft_identity: {
    base_release_id: "arl_3",
    id: "awc_1",
    row_version: 2,
  },
  active_plan_identity: null,
  active_snapshot_identity: null,
  active_override_identity: null,
  active_execution_summary: null,
  control_row_version: null,
  display_state: "draft",
  draft_rebase_state: "current",
  readiness: null,
  reason_codes: [],
  stale_subject: null,
  primary_action: {
    kind: "prepare_candidate",
    enabled: true,
    disabled_reason_codes: [],
  },
  secondary_actions: [],
  change_proposals: [],
  private_release: {
    available: true,
    blocked_reason_code: null,
    candidate_covers_draft: false,
    release_sequence: 4,
    test_state: "not_tested",
    warning: false,
    reason_codes: [],
  },
};

function suites(caseCount: number) {
  return {
    kind: "agent_eval_manager",
    schema_version: 1,
    agent_id: "osa_1",
    target: { kind: "live" },
    suites: [
      {
        suite_id: "aes_1",
        suite_kind: "owner_quality",
        name: "门店周报",
        visibility: "owner",
        state: "active",
        row_version: 1,
        platform_managed: false,
        revision: {
          suite_revision_id: "aev_1",
          sequence: 1,
          case_count: caseCount,
          created_at: "2026-11-24T08:00:00Z",
          cases: [],
        },
        evidence: {
          state: "missing",
          stale_reason: null,
          updated_at: null,
          run: null,
        },
      },
    ],
    runs: [],
    trigger_policy: null,
    release_gate: { required: false, ready: false, reason: "missing" },
  } as never;
}

let slot: HTMLDivElement;

function host(overrides: Partial<ReleaseBarHost> = {}): ReleaseBarHost {
  return {
    slot,
    shared: false,
    liveAudience: "private",
    dirty: false,
    saveBlocked: false,
    saving: false,
    onSave: vi.fn(),
    onDiscard: vi.fn(),
    onCopyLink: vi.fn(),
    ...overrides,
  };
}

function renderPanel(
  props: Partial<ComponentProps<typeof AgentVersionWorkbenchPanel>> = {},
) {
  return renderIntl(
    <AgentVersionWorkbenchPanel
      agentName="门店周报"
      agentId="osa_1"
      view="validation"
      workingCopy={null}
      onAvailability={vi.fn()}
      onChanged={vi.fn().mockResolvedValue(undefined)}
      onError={vi.fn()}
      {...props}
    />,
    { locale: "zh-CN" },
  );
}

const bar = () => within(screen.getByRole("region", { name: "发布进度" }));

beforeEach(() => {
  slot = document.createElement("div");
  document.body.appendChild(slot);
  vi.mocked(getAgentVersionWorkbench).mockResolvedValue(PRIVATE_DRAFT);
  vi.mocked(getAgentComparisonEstimate).mockRejectedValue(
    new ApiError(404, "disabled"),
  );
  vi.mocked(getAgentVersionCaseChanges).mockRejectedValue(
    new ApiError(404, "disabled"),
  );
  vi.mocked(getAgentEvalManager).mockResolvedValue(suites(2));
});

afterEach(() => {
  cleanup();
  slot.remove();
  vi.clearAllMocks();
});

describe("AgentReleaseBar", () => {
  it("shows one primary action, marks the current step and announces only status changes", () => {
    const state = releaseBarState({
      workbench: PRIVATE_DRAFT,
      kind: "prepare",
      test: "not_tested",
      hasOwnerCases: true,
      blockedByRegression: false,
      dirty: false,
      archived: false,
      shared: false,
    })!;
    const onRelease = vi.fn();
    function Harness() {
      const [status, setStatus] = useState("草稿有修改，可以发布为 v4");
      return (
        <>
          <button
            type="button"
            onClick={() => setStatus("正在测试：1/2 条用例")}
          >
            change
          </button>
          <AgentReleaseBar
            state={state}
            status={status}
            meta="测试可选"
            actions={{
              release: { label: "发布为 v4…", onSelect: onRelease },
              test: { label: "测试这次修改", onSelect: vi.fn() },
            }}
          />
        </>
      );
    }
    renderIntl(<Harness />, { locale: "zh-CN" });
    const region = screen.getByRole("region", { name: "发布进度" });
    expect(region.dataset.sticky).toBe("true");
    expect(within(region).getByRole("status").textContent).toBe("");
    expect(region.querySelectorAll(".settings-button--primary")).toHaveLength(
      1,
    );
    fireEvent.click(within(region).getByRole("button", { name: "发布为 v4…" }));
    expect(onRelease).toHaveBeenCalledTimes(1);
    const current = region.querySelectorAll("[aria-current='step']");
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toContain("发布");
    fireEvent.click(screen.getByRole("button", { name: "change" }));
    expect(
      within(screen.getByRole("region", { name: "发布进度" })).getByRole(
        "status",
      ).textContent,
    ).toBe("正在测试：1/2 条用例");
  });

  it("lets narrow screens open and close the steps", () => {
    const state = releaseBarState({ workbench: PRIVATE_DRAFT, kind: "prepare", test: "not_tested", hasOwnerCases: true, blockedByRegression: false, dirty: false, archived: false, shared: false })!;
    renderIntl(<AgentReleaseBar state={state} status="草稿有修改，可以发布为 v4" meta={null} actions={{}} />, { locale: "zh-CN" });
    const toggle = screen.getByRole("button", { name: "查看全部步骤" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const steps = document.getElementById(toggle.getAttribute("aria-controls")!)!;
    expect(steps.dataset.open).toBeUndefined();
    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: "收起步骤" }).getAttribute("aria-expanded")).toBe("true");
    expect(steps.dataset.open).toBe("true");
  });

  it("leaves saving to the Canvas save bar in the hosted band", async () => {
    renderPanel({ view: "canvas", releaseBar: host({ dirty: true, variant: "canvas" }) });
    await waitFor(() => expect(bar().getByText("有修改还没保存")).toBeTruthy());
    expect(screen.getByRole("region", { name: "发布进度" }).dataset.variant).toBe("canvas");
    expect(bar().queryByRole("button", { name: "保存草稿" })).toBeNull();
  });

  it("is a calm, non-sticky line without steps when nothing changed", () => {
    const state = releaseBarState({
      workbench: { ...PRIVATE_DRAFT, next_draft_identity: null },
      kind: "prepare",
      test: "nothing",
      hasOwnerCases: true,
      blockedByRegression: false,
      dirty: false,
      archived: false,
      shared: false,
    })!;
    renderIntl(
      <AgentReleaseBar
        state={state}
        status="已发布 v3 · 仅自己可用 · 没有未发布的修改"
        meta={null}
        actions={{}}
      />,
      { locale: "zh-CN" },
    );
    const region = screen.getByRole("region", { name: "发布进度" });
    expect(region.dataset.sticky).toBeUndefined();
    expect(region.querySelector("ol")).toBeNull();
    expect(within(region).queryAllByRole("button")).toHaveLength(0);
  });
});

describe("AgentVersionWorkbenchPanel with the release bar", () => {
  it("puts the release actions in the bar and leaves them out of the test card", async () => {
    renderPanel({ releaseBar: host() });
    await waitFor(() =>
      expect(bar().getByText("草稿有修改，可以发布为 v4")).toBeTruthy(),
    );
    expect(
      bar().getByRole("button", { name: "发布为 v4…" }).className,
    ).toContain("settings-button--primary");
    expect(bar().getByRole("button", { name: "测试这次修改" })).toBeTruthy();
    // The bar is the only place offering the test and the release.
    expect(
      screen.getAllByRole("button", { name: /测试这次修改|发布为 v4/ }),
    ).toHaveLength(2);
  });

  it("names what the release changes in the release dialog", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...PRIVATE_DRAFT,
      draft_changes: [{ path: "/definition/system_prompt", live: "旧的工作说明", draft: "新的工作说明", source: "coach" }],
    });
    renderPanel({ releaseBar: host() });
    fireEvent.click(await waitFor(() => bar().getByRole("button", { name: "发布为 v4…" })));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("这次发布包含的改动")).toBeTruthy();
    expect(within(dialog).getByRole("listitem").textContent).toMatch(/1/);
  });

  it("offers adding cases before a private test that would only run the safety check", async () => {
    vi.mocked(getAgentEvalManager).mockResolvedValue(suites(0));
    const onOpenEvaluate = vi.fn();
    renderPanel({ releaseBar: host(), onOpenEvaluate });
    await waitFor(() =>
      expect(bar().getByText(/还没有用例覆盖这次修改/)).toBeTruthy(),
    );
    fireEvent.click(bar().getByRole("button", { name: "补几条用例" }));
    expect(onOpenEvaluate).toHaveBeenCalledTimes(1);
    expect(
      bar().getByRole("button", { name: "只跑基础安全检查" }),
    ).toBeTruthy();
  });

  it("shows the bar on Configure without a test card", async () => {
    renderPanel({ view: "proposals", releaseBar: host() });
    await waitFor(() =>
      expect(bar().getByText("草稿有修改，可以发布为 v4")).toBeTruthy(),
    );
    expect(document.querySelector(".am-ci-test")).toBeNull();
  });

  it("asks to save unsaved edits first, without amber", async () => {
    const onSave = vi.fn();
    renderPanel({
      view: "proposals",
      releaseBar: host({ dirty: true, onSave }),
    });
    await waitFor(() => expect(bar().getByText("有修改还没保存")).toBeTruthy());
    expect(screen.getByRole("region", { name: "发布进度" }).dataset.tone).toBe(
      "neutral",
    );
    fireEvent.click(bar().getByRole("button", { name: "保存草稿" }));
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("asks before 测试这次修改 while cases are unsaved: 保存并测试 focused, or the saved cases only", async () => {
    vi.mocked(testAgentChange).mockResolvedValue({} as never);
    const save = vi.fn(async () => true);
    renderPanel({ view: "proposals", releaseBar: host({ cases: { dirtyCount: 2, save } }) });
    fireEvent.click(await waitFor(() => bar().getByRole("button", { name: "测试这次修改" })));
    const dialog = await screen.findByRole("alertdialog", { name: "用例有 2 项修改还没保存" });
    await waitFor(() => expect(document.activeElement?.textContent).toBe("保存并测试"));
    fireEvent.click(within(dialog).getByRole("button", { name: "只用已保存的用例测试" }));
    await waitFor(() => expect(testAgentChange).toHaveBeenCalledTimes(1));
    expect(save).not.toHaveBeenCalled();

    fireEvent.click(await waitFor(() => bar().getByRole("button", { name: "测试这次修改" })));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "保存并测试" }));
    await waitFor(() => expect(testAgentChange).toHaveBeenCalledTimes(2));
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("sums the latest test on 测试用例 over quality cases, with the safety check on its own line", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...PRIVATE_DRAFT,
      candidate_identity: { audience_mode: "private", candidate_hash: `sha256:${"1".repeat(64)}`, candidate_id: "arc_1", revision_id: "arv_c", row_version: 1, state: "prepared", base_release_id: "arl_3" },
      active_snapshot_identity: { id: "asn_1", hash: `sha256:${"2".repeat(64)}`, created_at: "2026-11-25T09:50:00Z", execution_id: "aex_1" },
      primary_action: { kind: "activate_candidate", enabled: true, disabled_reason_codes: [] },
      readiness: "ready",
    });
    const manager = suites(4) as unknown as { suites: Array<Record<string, unknown> & { revision: Record<string, unknown> }> };
    vi.mocked(getAgentEvalManager).mockResolvedValue({
      ...manager,
      suites: [...manager.suites, { ...manager.suites[0], suite_id: "aes_p", suite_kind: "platform_contract", revision: { ...manager.suites[0].revision, suite_revision_id: "aev_p" } }],
    } as never);
    vi.mocked(getAgentVersionCaseChanges).mockResolvedValue({
      kind: "agent_version_case_changes", schema_version: 1, execution_id: "aex_1",
      snapshot: { id: "asn_1", hash: "h", created_at: "2026-11-25T09:50:00Z" },
      baseline: { release_id: "arl_3", release_sequence: 3 }, candidate: { candidate_id: "arc_1", release_sequence: 4 },
      summary: { case_count: 5, worse: 1, unstable: 0, better: 1, not_comparable: 0, unchanged_required_failures: 0 },
      items: [
        { suite_revision_id: "aev_1", suite_name: "门店周报", case_revision_id: "c1", case_key: "q1", case_label: "a", change: "better", classification: "x", reason_codes: [], key_case: false, baseline: null, candidate: { passed: 3, runs: 3 } },
        { suite_revision_id: "aev_1", suite_name: "门店周报", case_revision_id: "c2", case_key: "q2", case_label: "b", change: "worse", classification: "x", reason_codes: [], key_case: false, baseline: null, candidate: { passed: 0, runs: 3 } },
      ],
      next_offset: null,
    });
    const onOpenRelease = vi.fn();
    renderPanel({ view: "evaluate", releaseBar: host(), onOpenRelease });
    const card = await screen.findByRole("region", { name: "最近一次测试" });
    expect(await within(card).findByText("变好 1")).toBeTruthy();
    expect(within(card).getByText("持平 2")).toBeTruthy();
    expect(within(card).getByText("变差 1")).toBeTruthy();
    expect(within(card).getByText("基础安全检查通过")).toBeTruthy();
    expect(within(card).getByText(/草稿对比已发布的 v3/)).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: "看全部结果" }));
    expect(onOpenRelease).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".am-ci-test")).toBeNull();
  });

  it("points at the version history once the tested draft was released", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...PRIVATE_DRAFT, next_draft_identity: null, private_release: null });
    const onOpenHistory = vi.fn();
    renderPanel({ view: "evaluate", releaseBar: host(), lastReleaseTest: { sequence: 3 }, onOpenHistory });
    const card = await screen.findByRole("region", { name: "最近一次测试" });
    expect(within(card).getByText("上次的测试随 v3 发布了，见版本历史")).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: "版本历史" }));
    expect(onOpenHistory).toHaveBeenCalledTimes(1);
  });

  it("starts nothing when saving the cases fails", async () => {
    const save = vi.fn(async () => false);
    renderPanel({ view: "proposals", releaseBar: host({ cases: { dirtyCount: 1, save } }) });
    fireEvent.click(await waitFor(() => bar().getByRole("button", { name: "测试这次修改" })));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "保存并测试" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(testAgentChange).not.toHaveBeenCalled();
  });

  it("names what blocks saving and links to the section that fixes it", async () => {
    const onSelect = vi.fn();
    renderPanel({
      view: "proposals",
      releaseBar: host({ dirty: true, saveBlocked: true, saveBlockedLink: { problem: "至少选择一个插件、工具或技能", label: "去「它能用什么」修改", onSelect } }),
    });
    await waitFor(() => expect(bar().getByText(/还不能保存：至少选择一个插件、工具或技能/)).toBeTruthy());
    fireEvent.click(bar().getByRole("button", { name: "去「它能用什么」修改" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(bar().getByRole("button", { name: "保存草稿" })).toHaveProperty("disabled", true);
  });

  it("keeps a decline visible with the reviewer's reason and the next step it calls for", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...PRIVATE_DRAFT,
      private_release: null,
      latest_decision: {
        candidate_id: "arc_9",
        decided_at: "2026-11-24T10:00:00Z",
        reason_code: "insufficient_evidence",
        state: "rejected",
      },
    });
    const onOpenEvaluate = vi.fn();
    renderPanel({
      releaseBar: host({ shared: true, liveAudience: "custom" }),
      onOpenEvaluate,
    });
    await waitFor(() =>
      expect(bar().getByText("没有批准：安全或效果证据不足")).toBeTruthy(),
    );
    expect(screen.getByRole("region", { name: "发布进度" }).dataset.tone).toBe(
      "bad",
    );
    fireEvent.click(bar().getByRole("button", { name: "补几条用例" }));
    expect(onOpenEvaluate).toHaveBeenCalledTimes(1);
  });

  it("offers withdrawing a submission that waits for review", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...PRIVATE_DRAFT,
      private_release: null,
      candidate_identity: {
        audience_mode: "custom",
        candidate_hash: `sha256:${"1".repeat(64)}`,
        candidate_id: "arc_1",
        revision_id: "arv_4",
        row_version: 2,
        state: "pending",
      },
      display_state: "pending_review",
      readiness: "ready",
      primary_action: {
        kind: "wait_for_review",
        enabled: false,
        disabled_reason_codes: [],
      },
    });
    renderPanel({ releaseBar: host({ shared: true, liveAudience: "custom" }) });
    await waitFor(() =>
      expect(
        bar().getByText("已提交审批，等团队负责人或管理员处理"),
      ).toBeTruthy(),
    );
    expect(bar().queryByRole("button", { name: /发布为|提交审批/ })).toBeNull();
    fireEvent.click(bar().getByRole("button", { name: "撤回提交…" }));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
  });
});

describe("试用草稿 on the release bar (Owner draft trial)", () => {
  const trialHost = (overrides: Partial<NonNullable<ReleaseBarHost["trial"]>> = {}): NonNullable<ReleaseBarHost["trial"]> => ({
    dirtyFields: [],
    savedDraftEqualsRelease: false,
    saveDraft: vi.fn(async () => true),
    ...overrides,
  });

  it("is a quiet action next to the test and the release", async () => {
    renderPanel({ releaseBar: host({ trial: trialHost() }) });
    const tryDraft = await waitFor(() => bar().getByRole("button", { name: "试用草稿" }));
    expect(tryDraft.className).toContain("ghost");
    expect(bar().getByRole("button", { name: "发布为 v4…" }).className).toContain("primary");
  });

  it("is absent while the capability is off", async () => {
    renderPanel({ releaseBar: host() });
    await waitFor(() => expect(bar().getByRole("button", { name: "发布为 v4…" })).toBeTruthy());
    expect(bar().queryByRole("button", { name: "试用草稿" })).toBeNull();
  });

  it("reads 试用审批中的版本 while the draft is in review", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...PRIVATE_DRAFT,
      private_release: null,
      candidate_identity: {
        audience_mode: "custom",
        candidate_hash: `sha256:${"1".repeat(64)}`,
        candidate_id: "arc_1",
        revision_id: "arv_4",
        row_version: 2,
        state: "pending",
      },
      display_state: "pending_review",
      readiness: "ready",
      primary_action: { kind: "wait_for_review", enabled: false, disabled_reason_codes: [] },
    });
    renderPanel({ releaseBar: host({ shared: true, liveAudience: "custom", trial: trialHost() }) });
    await waitFor(() => expect(bar().getByRole("button", { name: "试用审批中的版本" })).toBeTruthy());
  });

  it("lists the trials below the cases on 测试用例", async () => {
    const listSlot = document.createElement("div");
    document.body.appendChild(listSlot);
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...PRIVATE_DRAFT,
      recent_trials: [{ session_id: "s_1", first_question: "本周销售情况怎么样？", last_message_at: "2026-11-25T06:41:00Z", turn_count: 1, drafts_used: [], released_as: null }],
    });
    try {
      renderPanel({ view: "evaluate", releaseBar: host({ trial: trialHost({ listSlot }) }) });
      await waitFor(() => expect(within(listSlot).getByRole("link", { name: "打开试用：本周销售情况怎么样？" })).toBeTruthy());
      expect(within(listSlot).getByRole("button", { name: "新的试用" })).toBeTruthy();
    } finally {
      listSlot.remove();
    }
  });

  it("steps the Canvas band's primary down to secondary beside a trial chat", () => {
    const state = releaseBarState({ workbench: PRIVATE_DRAFT, kind: "prepare", test: "not_tested", hasOwnerCases: true, blockedByRegression: false, dirty: false, archived: false, shared: false })!;
    renderIntl(
      <AgentTrialLayoutContext.Provider value>
        <AgentReleaseBar state={state} status="草稿有修改" meta={null} variant="canvas" actions={{ release: { label: "发布为 v4…", onSelect: vi.fn() } }} />
      </AgentTrialLayoutContext.Provider>,
      { locale: "zh-CN" },
    );
    const region = screen.getByRole("region", { name: "发布进度" });
    expect(region.querySelectorAll(".settings-button--primary")).toHaveLength(0);
    expect(within(region).getByRole("button", { name: "发布为 v4…" })).toBeTruthy();
  });
});
