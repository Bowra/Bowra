// Feedback inbox (FE batch A) contracts: no loading empty-state flash, server-directed actions, four adoption states, error-code wording.
// Stub all APIs; server owns permission decisions; enforce only frontend response behavior.

import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { saveSpaceInstructions } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import {
  adoptFeedbackCandidate,
  decideFeedbackCandidate,
  getFeedbackCandidate,
  listFeedbackCandidates,
  rerouteFeedbackCandidate,
  setFeedbackCaseworkEnabled,
  type FeedbackCandidateRow,
} from "@/lib/feedback-candidates-api";

import { FeedbackInboxContent } from "./feedback-inbox-content";

vi.mock("@/lib/feedback-candidates-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/feedback-candidates-api")>("@/lib/feedback-candidates-api");
  return {
    ...actual,
    adoptFeedbackCandidate: vi.fn(),
    decideFeedbackCandidate: vi.fn(),
    getFeedbackCandidate: vi.fn(),
    listFeedbackCandidates: vi.fn(),
    rerouteFeedbackCandidate: vi.fn(),
    setFeedbackCaseworkEnabled: vi.fn(),
    withdrawFeedbackCandidate: vi.fn(),
  };
});

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getSpaceInstructions: vi.fn(async (workspaceId: string) => ({
      workspace_id: workspaceId,
      version: 1,
      content: "回答写给区域销售经理。",
      updated_by_name: "Ada",
      updated_at: "2026-10-15T08:00:00+00:00",
      max_chars: 6000,
      can_edit: true,
      archived: false,
      administrators: ["Ada"],
    })),
    listSpaceInstructionVersions: vi.fn(async () => []),
    saveSpaceInstructions: vi.fn(),
  };
});

const push = vi.fn();
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push, replace: vi.fn(), back: vi.fn() }) }));

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const ROW: FeedbackCandidateRow = {
  id: "fbc_5c21",
  target_kind: "agent_def",
  target_ref: "ag_example",
  proposed: { text: "报告固定结构：第一段直接给结论" },
  evidence_count: 3,
  state: "open",
  created_at: "2026-09-06T00:00:00+00:00",
  expires_at: "2026-09-20T00:00:00+00:00",
  mine: false,
  for_my_agent: true,
  proposer_id: "u_wang",
};

const BANNER = { candidate_id: "fbc_5c21", proposer_id: "u_wang", evidence_count: 3 };

beforeEach(() => {
  vi.mocked(listFeedbackCandidates).mockResolvedValue({ items: [ROW], viewer_is_admin: true });
  push.mockReset();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("FeedbackInboxContent", () => {
  it("shows the loading status without flashing the empty state", async () => {
    const pending = deferred<{ items: FeedbackCandidateRow[]; viewer_is_admin: boolean }>();
    vi.mocked(listFeedbackCandidates).mockReturnValue(pending.promise);
    renderIntl(<FeedbackInboxContent />);
    expect(screen.getByRole("status", { name: "正在加载建议…" })).toBeTruthy();
    expect(screen.queryByText(/还没有待审批的建议/)).toBeNull();
    await act(async () => {
      pending.resolve({ items: [], viewer_is_admin: true });
    });
    expect(screen.getByText(/还没有待审批的建议/)).toBeTruthy();
  });

  it("renders rows from server signals and loads detail on demand", async () => {
    vi.mocked(getFeedbackCandidate).mockResolvedValue({
      ...ROW,
      viewer_role: "admin",
      rubric_text: "报告第一段是结论而不是背景铺垫",
      evidence: { entries: [{ contributor_id: "u_wang", rubric_text: "报告第一段是结论", disclosure_ts: null, source_count: 1 }], others_count: 0 },
      sources: { total: 3, viewable: 2, viewable_indexes: [0, 1] },
      decided_by_me: false,
      decide_note: null,
    });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    expect(screen.getByText("3 人联署")).toBeTruthy();
    expect(screen.getByText("提议：u_wang")).toBeTruthy();
    expect(screen.getByRole("button", { name: "采纳并预填" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看判据与联署" }));
    await screen.findByText("报告第一段是结论而不是背景铺垫");
    expect(screen.getByText("来源对话：按你的权限可查看 2 / 3 段")).toBeTruthy();
  });

  it("adopt with a prefill stashes the handoff and offers the editor", async () => {
    vi.mocked(adoptFeedbackCandidate).mockResolvedValue({
      id: "fbc_5c21",
      state: "accepted",
      prefill: { kind: "agent_system_prompt", edited_content: "You are the example team data analyst. Start reports with the conclusion.", rationale: "ok" },
      base: { definition_sha256: "abc" },
      banner: BANNER,
    });
    // Server lists only open items: refetch after adoption removes row and prefill button together; stub simulates this.
    vi.mocked(listFeedbackCandidates).mockResolvedValueOnce({ items: [ROW], viewer_is_admin: true }).mockResolvedValue({ items: [], viewer_is_admin: true });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    fireEvent.click(screen.getByRole("button", { name: "采纳并预填" }));
    await screen.findByText(/已生成智能体指令预填/);
    expect(screen.getByTestId("fbi-row-fbc_5c21")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开编辑页审阅" }));
    const stashed = JSON.parse(sessionStorage.getItem("bowhead:feedback-prefill") ?? "null");
    expect(stashed.target_ref).toBe("ag_example");
    expect(stashed.prefill.edited_content).toContain("Start reports with the conclusion.");
    expect(push).toHaveBeenCalledWith("/agents/manage/?agent_id=ag_example&feedback_prefill=1");
  });

  it("reroute keeps the row actionable and shows the coach's reason", async () => {
    vi.mocked(adoptFeedbackCandidate).mockResolvedValue({
      id: "fbc_5c21",
      state: "open",
      reroute: { reason: "图表规范 skill：形态约定", rationale: "落点不在 prompt" },
      banner: BANNER,
    });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    fireEvent.click(screen.getByRole("button", { name: "采纳并预填" }));
    await screen.findByText("教练判断这条不该改在这里");
    expect(screen.getByText("图表规范 skill：形态约定")).toBeTruthy();
    expect(screen.getByRole("button", { name: "拒绝…" })).toBeTruthy();
  });

  it("adopting a wording suggestion lists each source Space, reviews the wording, and marks the Space once saved", async () => {
    vi.mocked(listFeedbackCandidates).mockResolvedValue({
      items: [{ ...ROW, id: "fbc_sem", target_kind: "semantic", target_ref: "net_profit", for_my_agent: false }],
      viewer_is_admin: true,
    });
    const target = (workspace_id: string, name: string, conversation_count: number, instructions_version: number) => ({
      workspace_id,
      name,
      conversation_count,
      instructions_version,
      added: null,
    });
    vi.mocked(adoptFeedbackCandidate).mockResolvedValue({
      id: "fbc_sem",
      state: "accepted",
      prefill: {
        kind: "space_instructions",
        text: "净利润统一使用合并报表口径",
        spaces: [target("ws_sales", "销售", 5, 1), target("ws_access", "准入", 1, 0)],
      },
      banner: { ...BANNER, candidate_id: "fbc_sem" },
    });
    vi.mocked(saveSpaceInstructions).mockResolvedValue({
      workspace_id: "ws_sales",
      version: 2,
      content: "回答写给区域销售经理。\n\n净利润统一使用合并报表口径",
      updated_by_name: "Ada",
      updated_at: "2026-10-17T08:00:00+00:00",
      max_chars: 6000,
      can_edit: true,
      archived: false,
      administrators: ["Ada"],
    });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_sem");
    fireEvent.click(screen.getByRole("button", { name: "采纳并预填" }));
    expect(await screen.findByText("5 段对话 · 说明当前第 1 版")).toBeTruthy();
    expect(screen.getByText("1 段对话 · 还没有说明")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "加到空间说明" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "「销售」的空间说明" });
    expect(await within(dialog).findByText("此内容由成员反馈起草，来源不可信，保存前逐行审阅")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "我已逐行审阅预填内容" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "应用到编辑器" }));
    const editor = (await within(dialog).findByRole("textbox")) as HTMLTextAreaElement;
    expect(editor.value).toBe("回答写给区域销售经理。\n\n净利润统一使用合并报表口径");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(saveSpaceInstructions).toHaveBeenCalledWith("ws_sales", {
        content: "回答写给区域销售经理。\n\n净利润统一使用合并报表口径",
        expected_version: 1,
        source_candidate_id: "fbc_sem",
      }),
    );
    expect(await screen.findByText(/已加到第 2 版/)).toBeTruthy();
    expect(screen.getByText("已写入 1/2 个空间")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("the processed view brings back an adopted wording suggestion and where it already landed", async () => {
    vi.mocked(listFeedbackCandidates).mockImplementation(async (view) =>
      view === "decided"
        ? {
            items: [
              {
                ...ROW,
                id: "fbc_sem",
                target_kind: "semantic",
                target_ref: "net_profit",
                state: "accepted",
                for_my_agent: false,
                decided_at: "2026-10-16T08:00:00+00:00",
                space_targets: {
                  text: "净利润统一使用合并报表口径",
                  spaces: [
                    { workspace_id: "ws_sales", name: "销售", conversation_count: 3, instructions_version: 5, added: { version: 5, by_name: "Ada", at: "2026-10-16T09:00:00+00:00" } },
                    { workspace_id: "ws_access", name: "准入", conversation_count: 1, instructions_version: 0, added: null },
                  ],
                },
              },
            ],
            viewer_is_admin: true,
          }
        : { items: [ROW], viewer_is_admin: true },
    );
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    expect(screen.getByRole("radio", { name: "待审批 1" })).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "已处理" }));
    await screen.findByTestId("fbi-row-fbc_sem");
    expect(listFeedbackCandidates).toHaveBeenLastCalledWith("decided");
    expect(screen.getByText(/已加到第 5 版/)).toBeTruthy();
    expect(screen.getByText("已写入 1/2 个空间")).toBeTruthy();
    expect(screen.getByRole("button", { name: "加到空间说明" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "采纳并预填" })).toBeNull();
  });

  it("asks the adopter to choose a Space when the suggestion has no active source Space", async () => {
    vi.mocked(listFeedbackCandidates).mockResolvedValue({
      items: [{ ...ROW, id: "fbc_sem", target_kind: "semantic", target_ref: "net_profit", for_my_agent: false }],
      viewer_is_admin: true,
    });
    vi.mocked(adoptFeedbackCandidate).mockResolvedValue({
      id: "fbc_sem",
      state: "accepted",
      prefill: { kind: "space_instructions", text: "净利润统一使用合并报表口径", spaces: [] },
      banner: { ...BANNER, candidate_id: "fbc_sem" },
    });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_sem");
    fireEvent.click(screen.getByRole("button", { name: "采纳并预填" }));
    fireEvent.click(await screen.findByRole("button", { name: "打开空间管理" }));
    expect(push).toHaveBeenCalledWith("/team/settings/spaces/");
  });

  it("degraded adopt reports the reason without a prefill", async () => {
    vi.mocked(adoptFeedbackCandidate).mockResolvedValue({
      id: "fbc_5c21",
      state: "accepted",
      prefill: null,
      degraded: true,
      degrade_reason: "surface_too_large",
      banner: BANNER,
    });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    fireEvent.click(screen.getByRole("button", { name: "采纳并预填" }));
    await screen.findByText(/目标配置过长，未生成预填/);
  });

  it("gates adopt on for_my_agent for agent kinds and lets admin proposers withdraw", async () => {
    vi.mocked(listFeedbackCandidates).mockResolvedValue({
      items: [
        { ...ROW, id: "fbc_a", for_my_agent: false },
        { ...ROW, id: "fbc_b", target_kind: "semantic", target_ref: "net_profit", for_my_agent: false },
        { ...ROW, id: "fbc_c", mine: true, for_my_agent: false },
      ],
      viewer_is_admin: true,
    });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_c");
    const row = (id: string) => screen.getByTestId(`fbi-row-${id}`);
    expect(row("fbc_a").textContent).not.toContain("采纳并预填"); // Admin is not this agent's owner.
    expect(row("fbc_b").textContent).toContain("采纳并预填"); // Admin owns business definitions.
    expect(row("fbc_c").textContent).toContain("撤回"); // Admin's own proposal: withdrawal only.
    expect(row("fbc_c").textContent).not.toContain("拒绝");
  });

  it("routes skill and new-asset prefills to their native entries with a stashed handoff", async () => {
    vi.mocked(listFeedbackCandidates).mockResolvedValue({
      items: [
        { ...ROW, id: "fbc_sk", target_kind: "team_skill", target_ref: "oskill_1", for_my_agent: false },
        { ...ROW, id: "fbc_new", target_kind: "new_asset", target_ref: "new", for_my_agent: false },
      ],
      viewer_is_admin: true,
    });
    vi.mocked(adoptFeedbackCandidate)
      .mockResolvedValueOnce({ id: "fbc_sk", state: "accepted", prefill: { kind: "team_skill_body", edited_content: "body v2", rationale: "" }, base: { body_sha256: "x" }, banner: { ...BANNER, candidate_id: "fbc_sk" } })
      .mockResolvedValueOnce({ id: "fbc_new", state: "accepted", prefill: { kind: "cca_intent", purpose: "做一个周报 agent", locale: "zh-CN" }, banner: { ...BANNER, candidate_id: "fbc_new" } });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_new");
    const row = (id: string) => screen.getByTestId(`fbi-row-${id}`);
    fireEvent.click(row("fbc_sk").querySelector("button.settings-button--primary") as HTMLButtonElement);
    await screen.findByText(/已生成技能正文预填/);
    fireEvent.click(screen.getByRole("button", { name: "打开编辑页审阅" }));
    expect(JSON.parse(sessionStorage.getItem("bowhead:feedback-prefill")!).target_ref).toBe("oskill_1");
    expect(push).toHaveBeenCalledWith("/skills/?skill=oskill_1&feedback_prefill=1");
    fireEvent.click(row("fbc_new").querySelector("button.settings-button--primary") as HTMLButtonElement);
    await screen.findByText(/新资产意图/);
    fireEvent.click(screen.getByRole("button", { name: "去创建向导" }));
    expect(JSON.parse(sessionStorage.getItem("bowhead:feedback-prefill")!).prefill.purpose).toBe("做一个周报 agent");
    expect(push).toHaveBeenCalledWith("/agents/new/?feedback_prefill=1&ref=new");
  });

  it("maps server error codes to copy", async () => {
    vi.mocked(decideFeedbackCandidate).mockRejectedValue(
      new ApiError(403, "forbidden", { code: "self_approval_forbidden", body: { detail: { code: "self_approval_forbidden" } } }),
    );
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    fireEvent.click(screen.getByRole("button", { name: "拒绝…" }));
    fireEvent.click(screen.getByRole("button", { name: "确认拒绝" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("不能裁决自己参与的建议"));
  });

  // ---- Batch 3b: casework chip / transfer / usage and switches ----

  it("shows the coach casework chip with role-aware blocked copy", async () => {
    const blocked: FeedbackCandidateRow = { ...ROW, id: "fbc_b", casework: { status: "blocked", verdict: null, reason_code: "team_model_unconfigured", reroute_target: null, evidence_used: null, drafted_at: null } };
    const drafted: FeedbackCandidateRow = { ...ROW, id: "fbc_d", casework: { status: "done", verdict: "edit", reason_code: null, reroute_target: null, evidence_used: 2, drafted_at: "2026-09-09T00:00:00+00:00" } };
    vi.mocked(listFeedbackCandidates).mockResolvedValue({ items: [blocked, drafted], viewer_is_admin: false });
    renderIntl(<FeedbackInboxContent />);
    expect((await screen.findByTestId("fbi-casework-fbc_b")).textContent).toBe("教练未起草：等待管理员配置模型");
    expect(screen.getByTestId("fbi-casework-fbc_d").textContent).toBe("教练已起草");
  });

  it("offers reroute on a pre-drafted coach verdict and replaces the row with the new candidate", async () => {
    const row: FeedbackCandidateRow = {
      ...ROW,
      casework: { status: "done", verdict: "reroute", reason_code: null, reroute_target: { target_kind: "team_skill", target_ref: "chart-style", label: "图表规范", reason: "形态约定" }, evidence_used: 1, drafted_at: "2026-09-09T00:00:00+00:00" },
    };
    vi.mocked(listFeedbackCandidates).mockResolvedValue({ items: [row], viewer_is_admin: true });
    const newRow: FeedbackCandidateRow = { ...ROW, id: "fbc_new", target_kind: "team_skill", target_ref: "chart-style", target_label: "图表规范", for_my_agent: false, casework: null };
    vi.mocked(rerouteFeedbackCandidate).mockResolvedValue({ superseded_id: "fbc_5c21", outcome: "created", candidate_id: "fbc_new", candidate: newRow });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    expect(screen.getByTestId("fbi-casework-fbc_5c21").textContent).toBe("教练建议改到「图表规范」");
    fireEvent.click(screen.getByRole("button", { name: "转案到「图表规范」" }));
    await screen.findByText("已转案到「图表规范」，将由新目标的审批人处理");
    expect(vi.mocked(rerouteFeedbackCandidate)).toHaveBeenCalledWith("fbc_5c21", { target_kind: "team_skill", target_ref: "chart-style" });
    const fresh = screen.getByTestId("fbi-row-fbc_new");
    expect(fresh.textContent).toContain("图表规范");
    // Transferred original row hides action bar, keeping hint; new target (skill, admin may adopt) derives fresh actions.
    expect(within(screen.getByTestId("fbi-row-fbc_5c21")).queryByRole("button", { name: "采纳并预填" })).toBeNull();
    expect(within(fresh).getByRole("button", { name: "采纳并预填" })).toBeTruthy();
  });

  it("live reroute card carries the server-resolved target and a reroute button", async () => {
    vi.mocked(adoptFeedbackCandidate).mockResolvedValue({
      id: "fbc_5c21",
      state: "open",
      reroute: { reason: "图表规范 skill：形态约定", rationale: "落点不在 prompt", target: { target_kind: "team_skill", target_ref: "chart-style", label: "图表规范" } },
      banner: BANNER,
    });
    vi.mocked(rerouteFeedbackCandidate).mockResolvedValue({ superseded_id: "fbc_5c21", outcome: "merged", candidate_id: "fbc_x", candidate: null });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    fireEvent.click(screen.getByRole("button", { name: "采纳并预填" }));
    await screen.findByText("教练判断这条不该改在这里");
    fireEvent.click(screen.getByRole("button", { name: "转案到「图表规范」" }));
    await screen.findByText("已转案到「图表规范」，将由新目标的审批人处理");
    expect(screen.queryByTestId("fbi-row-fbc_x")).toBeNull(); // Never invent a row when server returns none (caller has no lane on new target).
  });

  it("renders the admin coach usage line and toggles background drafting with rollback on failure", async () => {
    vi.mocked(listFeedbackCandidates).mockResolvedValue({ items: [ROW], viewer_is_admin: true, coach: { month_drafts: 4, casework_enabled: true } });
    vi.mocked(setFeedbackCaseworkEnabled).mockResolvedValueOnce({ enabled: false }).mockRejectedValueOnce(new ApiError(403, "forbidden", { code: "feedback_settings_forbidden", body: { detail: { code: "feedback_settings_forbidden" } } }));
    renderIntl(<FeedbackInboxContent />);
    const bar = await screen.findByTestId("fbi-coach-bar");
    expect(bar.textContent).toContain("本月后台教练起草 4 次");
    const sw = screen.getByRole("switch", { name: "后台自动起草" });
    expect(sw.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(sw);
    await waitFor(() => expect(sw.getAttribute("aria-checked")).toBe("false"));
    fireEvent.click(sw); // Second failure → roll back to false.
    await waitFor(() => expect(bar.textContent).toContain("只有负责人或管理员可以修改此设置"));
    expect(sw.getAttribute("aria-checked")).toBe("false");
  });

  it("hides the coach bar for members (no coach payload)", async () => {
    vi.mocked(listFeedbackCandidates).mockResolvedValue({ items: [ROW], viewer_is_admin: false });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    expect(screen.queryByTestId("fbi-coach-bar")).toBeNull();
  });

  it("flags a recurrence so the reviewer knows this was approved before", async () => {
    const row: FeedbackCandidateRow = {
      ...ROW,
      recurrence: { candidate_id: "fbc_old", decided_at: "2026-09-02T00:00:00+00:00", decided_by: "u_admin", decided_by_display: "李涛", days_ago: 7, prior_verdict: "edit" },
    };
    vi.mocked(listFeedbackCandidates).mockResolvedValue({ items: [row], viewer_is_admin: true });
    renderIntl(<FeedbackInboxContent />);
    const badge = await screen.findByTestId("fbi-recurrence-fbc_5c21");
    expect(badge.textContent).toBe("7 天前批过措辞相同的建议");
    expect(badge.getAttribute("title")).toContain("措辞相同的建议此前已被采纳过");
    expect(badge.getAttribute("title")).toContain("上次由 李涛 采纳");
    expect(badge.getAttribute("title")).toContain("上次教练判定改在该资产本身");
  });

  it("proposer view keeps the badge but carries no approver or coach judgment", async () => {
    const row: FeedbackCandidateRow = {
      ...ROW,
      mine: true,
      recurrence: { candidate_id: null, decided_at: null, decided_by: null, decided_by_display: null, days_ago: 3, prior_verdict: null },
    };
    vi.mocked(listFeedbackCandidates).mockResolvedValue({ items: [row], viewer_is_admin: false });
    renderIntl(<FeedbackInboxContent />);
    const badge = await screen.findByTestId("fbi-recurrence-fbc_5c21");
    expect(badge.textContent).toBe("3 天前批过措辞相同的建议");
    const title = badge.getAttribute("title") || "";
    expect(title).toContain("措辞相同的建议此前已被采纳过");
    expect(title).toContain("调换主宾");  // Signature is lexical; disclose possible false positives to approver.
    expect(title).not.toContain("上次由");
    expect(title).not.toContain("上次教练判定");
  });

  it("shows no recurrence badge for a first-time suggestion", async () => {
    vi.mocked(listFeedbackCandidates).mockResolvedValue({ items: [ROW], viewer_is_admin: true });
    renderIntl(<FeedbackInboxContent />);
    await screen.findByTestId("fbi-row-fbc_5c21");
    expect(screen.queryByTestId("fbi-recurrence-fbc_5c21")).toBeNull();
  });

  // ---- Page acceptance A: loading skeleton, full-page error retry ----
  describe("loading skeleton and error retry", () => {
    it("加载中是骨架（带「正在加载建议」名称），不闪空态", async () => {
      const pending = deferred<{ items: FeedbackCandidateRow[]; viewer_is_admin: boolean }>();
      vi.mocked(listFeedbackCandidates).mockReturnValue(pending.promise);
      const { container } = renderIntl(<FeedbackInboxContent />);
      const status = screen.getByRole("status", { name: "正在加载建议…" });
      expect(status.classList.contains("ui-skeleton")).toBe(true);
      expect(container.querySelector(".legacy-fbi-empty")).toBeNull();
      await act(async () => {
        pending.resolve({ items: [], viewer_is_admin: true });
      });
      expect(screen.getByText(/还没有待审批的建议/)).toBeTruthy();
    });

    it("列表加载失败：提示 + 重试，重试重新拉取", async () => {
      vi.mocked(listFeedbackCandidates).mockRejectedValueOnce(
        new ApiError(503, "unavailable", { code: "feedback_store_unavailable", body: { detail: { code: "feedback_store_unavailable" } } }),
      );
      renderIntl(<FeedbackInboxContent />);
      const retry = await screen.findByRole("button", { name: "重试" });
      fireEvent.click(retry);
      await screen.findByTestId("fbi-row-fbc_5c21");
      expect(listFeedbackCandidates).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
    });
  });
});
