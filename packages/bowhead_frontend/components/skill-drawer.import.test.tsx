/**
 * SkillDrawer · pending-import status area (skill-import batch 3).
 *
 * Enforce three things: pending explains Not Effective and offers two exits; Enable Anyway calls override endpoint with confirmation;
 * overridden uses different copy and no longer prompts for action.
 */

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { ApiError } from "@/lib/api-client";
import { promoteSkill } from "@/lib/workflow-solidify-api";

import { SkillDrawer, type SkillDrawerTarget } from "./skill-drawer";

const { fetchSkillDetail, overrideSkillImport, requestSkillCoachDraft } = vi.hoisted(() => ({
  fetchSkillDetail: vi.fn(),
  overrideSkillImport: vi.fn(),
  requestSkillCoachDraft: vi.fn(),
}));

vi.mock("@/lib/skill-coach-api", async (orig) => ({
  ...(await orig<typeof import("@/lib/skill-coach-api")>()),
  requestSkillCoachDraft,
}));

vi.mock("@/lib/api", () => ({
  changeSkillFile: vi.fn(),
  fetchSkillDetail,
  fetchSkillFile: vi.fn(),
  putSkillFile: vi.fn(),
  putSkillBody: vi.fn(),
  overrideSkillImport,
}));
vi.mock("@/lib/workflow-solidify-api", () => ({
  getTeamSkill: vi.fn(),
  getTeamSkillFile: vi.fn(),
  putTeamSkillFile: vi.fn(),
  putTeamSkillBody: vi.fn(),
  copyTeamSkillToPersonal: vi.fn(),
  promoteSkill: vi.fn(),
  withdrawTeamSkill: vi.fn(),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn() }) }));

const REPORT = {
  grade: "red" as const,
  red_count: 1,
  findings: [
    {
      id: "f_1",
      code: "file_ref_dangling",
      severity: "red" as const,
      path: "SKILL.md",
      line: 12,
      subject: "references/x.md",
      resolved_against: "bundle_files",
      message_zh: "引用的文件 references/x.md 没有一起上传",
      message_en: "Referenced file was not included",
    },
  ],
};

function detail(importStatus: string | null) {
  return {
    name: "onc-qbr",
    source_type: "user_upload",
    enabled: true,
    size_bytes: 4200,
    body_md: "# 正文",
    body_raw: "---\nname: onc-qbr\n---\n# 正文",
    allowed_tools: [],
    scripts: [],
    trigger_examples: [],
    references: [],
    evals_count: 0,
    has_evals: false,
    import_status: importStatus,
    import_report: importStatus ? REPORT : null,
  };
}

const target: SkillDrawerTarget = {
  kind: "skill",
  skill: { name: "onc-qbr", source_type: "user_upload", enabled: true, size_bytes: 4200 } as never,
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

/** In-app confirmation: find dialog by title, click its button. */
async function answerConfirm(title: RegExp | string, button: string) {
  const dialog = await screen.findByRole("dialog", { name: title });
  fireEvent.click(within(dialog).getByRole("button", { name: button }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: title })).toBeNull());
}

describe("待处理导入状态区", () => {
  it("pending：说清未生效、列出阻断项、给出两条出路", async () => {
    fetchSkillDetail.mockResolvedValue(detail("pending"));
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("有阻断项，未生效")).toBeTruthy());
    expect(screen.getByText(/不能勾进智能体/)).toBeTruthy();
    expect(screen.getByText(/引用的文件 references\/x.md/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "去编辑" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "仍然启用" })).toBeTruthy();
  });

  it("「仍然启用」要二次确认；确认后打 override 端点并重取详情", async () => {
    fetchSkillDetail.mockResolvedValue(detail("pending"));
    overrideSkillImport.mockResolvedValue({ name: "onc-qbr", import_status: "overridden" });
    const onTeamStateChanged = vi.fn();
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} onTeamStateChanged={onTeamStateChanged} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "仍然启用" })).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "仍然启用" }));
    await answerConfirm("仍然启用这个技能？", "取消");
    expect(overrideSkillImport).not.toHaveBeenCalled(); // Cancel = no change.

    fetchSkillDetail.mockResolvedValue(detail("overridden"));
    fireEvent.click(screen.getByRole("button", { name: "仍然启用" }));
    await answerConfirm("仍然启用这个技能？", "仍然启用");
    await waitFor(() => expect(overrideSkillImport).toHaveBeenCalledWith("onc-qbr"));
    await waitFor(() => expect(screen.getByText("已启用（明知有阻断项）")).toBeTruthy());
    expect(onTeamStateChanged).toHaveBeenCalled(); // Parent list refreshes too.
  });

  it("overridden：换一句话，不再给「仍然启用」", async () => {
    fetchSkillDetail.mockResolvedValue(detail("overridden"));
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("已启用（明知有阻断项）")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "仍然启用" })).toBeNull();
  });

  it("团队 skill 永远不出这一区（后端也没有这个字段）", async () => {
    const teamTarget = {
      kind: "team",
      skill: { id: "oskill_1", team_id: "t", name: "onc-qbr", display_name: "onc", approval: "approved", import_status: "pending" },
      canManage: false,
      canEdit: false,
    } as unknown as SkillDrawerTarget;
    renderIntl(<SkillDrawer target={teamTarget} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("onc")).toBeTruthy());
    expect(screen.queryByText("有阻断项，未生效")).toBeNull();
  });

  it("详情没回来之前「去编辑」不可点——否则草稿是空串，保存等于清空正文", async () => {
    let release: (v: unknown) => void = () => {};
    fetchSkillDetail.mockReturnValue(new Promise((r) => (release = r)));
    renderIntl(<SkillDrawer target={{ ...target, skill: { ...target.skill, import_status: "pending" } } as never} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "去编辑" })).toBeTruthy());
    expect(screen.getByRole("button", { name: "去编辑" })).toHaveProperty("disabled", true);
    release(detail("pending"));
    await waitFor(() => expect(screen.getByRole("button", { name: "去编辑" })).toHaveProperty("disabled", false));
  });

  it("服务端说它已经不是 pending（409）时重取详情，不把死按钮摆在那", async () => {
    const { ApiError } = await import("@/lib/api-client");
    fetchSkillDetail.mockResolvedValue(detail("pending"));
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "仍然启用" })).toBeTruthy());
    overrideSkillImport.mockRejectedValue(new ApiError(409, "not pending", { code: "skill_import_not_pending" }));
    fetchSkillDetail.mockResolvedValue(detail("overridden"));
    fireEvent.click(screen.getByRole("button", { name: "仍然启用" }));
    await answerConfirm("仍然启用这个技能？", "仍然启用");
    await waitFor(() => expect(screen.getByText("已启用（明知有阻断项）")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "仍然启用" })).toBeNull();
  });

  it("没有清点状态的 skill 不出这一区", async () => {
    fetchSkillDetail.mockResolvedValue(detail(null));
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} />);
    await waitFor(() => expect(fetchSkillDetail).toHaveBeenCalled());
    expect(screen.queryByText("有阻断项，未生效")).toBeNull();
  });
});

describe("教练改稿入口（skill-import 批4）", () => {
  it("能力位关着时不渲染——服务端也 404，界面不能替执行侧承诺", async () => {
    fetchSkillDetail.mockResolvedValue(detail("pending"));
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("有阻断项，未生效")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "让教练改" })).toBeNull();
  });

  it("取到改稿后开面板；应用即灌进编辑器草稿，面板自己不保存", async () => {
    fetchSkillDetail.mockResolvedValue(detail("pending"));
    // Baseline fingerprint must match drawer body: fake fingerprint marks draft stale and disables Use This Draft;
    // previous passing result only clicked before fingerprint calculation finished.
    const { bodySha256 } = await import("@/lib/skill-coach-api");
    requestSkillCoachDraft.mockResolvedValue({
      prefill: { kind: "user_skill_body", edited_content: "---\nname: onc-qbr\n---\n改后的正文" },
      base: { body_sha256: await bodySha256(detail("pending").body_raw) },
      report: { findings_addressed: ["f_1"] },
    });
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} coachEnabled />);
    await waitFor(() => expect(screen.getByRole("button", { name: "让教练改" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "让教练改" }));
    await waitFor(() => expect(screen.getByTestId("skill-coach-panel")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox", { name: /逐行审阅/ }));
    fireEvent.click(screen.getByRole("button", { name: "用这份改稿" }));
    // Enters editor draft: editing area contains new body; no save request.
    await waitFor(() => expect(screen.getByDisplayValue(/改后的正文/)).toBeTruthy());
    expect(screen.queryByTestId("skill-coach-panel")).toBeNull();
  });

  it("限流与未配置模型分开说，不混成「稍后重试」", async () => {
    const { ApiError } = await import("@/lib/api-client");
    fetchSkillDetail.mockResolvedValue(detail("pending"));
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} coachEnabled />);
    await waitFor(() => expect(screen.getByRole("button", { name: "让教练改" })).toBeTruthy());

    requestSkillCoachDraft.mockRejectedValue(new ApiError(429, "throttled", { code: "feedback_redraft_throttled" }));
    fireEvent.click(screen.getByRole("button", { name: "让教练改" }));
    await waitFor(() => expect(screen.getByText(/每天 3 次/)).toBeTruthy());

    requestSkillCoachDraft.mockRejectedValue(new ApiError(409, "no model", {}));
    fireEvent.click(screen.getByRole("button", { name: "让教练改" }));
    await waitFor(() => expect(screen.getByText(/没有配置可用的模型/)).toBeTruthy());
  });
});

describe("教练入口的三条边界（re-review）", () => {
  it("停在别的 tab 上点，先切回说明页再发请求——否则请求发了、面板不出现，还白烧一次日限", async () => {
    fetchSkillDetail.mockResolvedValue({ ...detail("pending"), trigger_examples: ["帮我做 QBR"] });
    requestSkillCoachDraft.mockResolvedValue({
      prefill: { kind: "user_skill_body", edited_content: "---\nname: onc-qbr\n---\n改后" },
      base: { body_sha256: "x" },
      report: { findings_addressed: [] },
    });
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} coachEnabled />);
    await waitFor(() => expect(screen.getByRole("tab", { name: "触发示例" })).toBeTruthy());
    fireEvent.click(screen.getByRole("tab", { name: "触发示例" }));
    fireEvent.click(screen.getByRole("button", { name: "让教练改" }));
    await waitFor(() => expect(screen.getByTestId("skill-coach-panel")).toBeTruthy());
  });

  it("404 不说「稍后重试」——那是永不恢复的路", async () => {
    const { ApiError } = await import("@/lib/api-client");
    fetchSkillDetail.mockResolvedValue(detail("pending"));
    requestSkillCoachDraft.mockRejectedValue(new ApiError(404, "not found", {}));
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} coachEnabled />);
    await waitFor(() => expect(screen.getByRole("button", { name: "让教练改" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "让教练改" }));
    await waitFor(() => expect(screen.getByText(/这条路现在不通/)).toBeTruthy());
    expect(screen.queryByText(/稍后重试/)).toBeNull();
  });

  it("overridden 也能请教练——服务端本就受理，这条放宽还是本线自己要的", async () => {
    fetchSkillDetail.mockResolvedValue(detail("overridden"));
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} coachEnabled />);
    await waitFor(() => expect(screen.getByText("已启用（明知有阻断项）")).toBeTruthy());
    expect(screen.getByRole("button", { name: "让教练改" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "仍然启用" })).toBeNull();
  });
});

describe("离开编辑器时取消 ⇒ 不发请求（re-review：突变去掉 leaveEditor 全绿）", () => {
  it("有未保存改动且用户点了取消，教练请求不能发出去——每天只有 3 次", async () => {
    fetchSkillDetail.mockResolvedValue(detail("pending"));
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} coachEnabled />);
    await waitFor(() => expect(screen.getByRole("button", { name: "编辑" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    const box = await screen.findByRole("textbox");
    fireEvent.change(box, { target: { value: "我手写的重要修改" } });

    fireEvent.click(screen.getByRole("button", { name: "让教练改" }));
    await answerConfirm("放弃未保存的修改？", "继续编辑");
    expect(requestSkillCoachDraft).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue("我手写的重要修改")).toBeTruthy();

    // Positive control: send only after confirmation.
    requestSkillCoachDraft.mockResolvedValue({
      prefill: { kind: "user_skill_body", edited_content: "---\nname: onc-qbr\n---\n改后" },
      base: { body_sha256: "x" },
      report: { findings_addressed: [] },
    });
    fireEvent.click(screen.getByRole("button", { name: "让教练改" }));
    await answerConfirm("放弃未保存的修改？", "放弃修改");
    await waitFor(() => expect(requestSkillCoachDraft).toHaveBeenCalledOnce());
  });

  it("待处理导入不给可点的「提交到团队」——服务端必回 409，按钮说清卡在哪", async () => {
    fetchSkillDetail.mockResolvedValue({ ...detail("pending"), content_sha256: "a".repeat(64) });
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} workflowCreatorEnabled meId="usr_me" teamRow={null} />);
    const submit = await screen.findByRole("button", { name: "提交到团队" });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    expect(submit.getAttribute("title")).toMatch(/导入还待处理/);
  });

  it("overridden 仍可提交到团队", async () => {
    fetchSkillDetail.mockResolvedValue({ ...detail("overridden"), content_sha256: "a".repeat(64) });
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} workflowCreatorEnabled meId="usr_me" teamRow={null} />);
    const submit = await screen.findByRole("button", { name: "提交到团队" });
    expect((submit as HTMLButtonElement).disabled).toBe(false);
  });

  it("提交时内容已过期（409 stale）：自动重取详情，再点提交带上最新正文", async () => {
    fetchSkillDetail
      .mockResolvedValueOnce({ ...detail(null), body_md: "# 旧正文", content_sha256: "a".repeat(64) })
      .mockResolvedValue({ ...detail(null), body_md: "# 新正文", content_sha256: "b".repeat(64) });
    vi.mocked(promoteSkill)
      .mockRejectedValueOnce(new ApiError(409, "stale", { code: "skill_promote_content_stale" }))
      .mockResolvedValue({} as never);
    renderIntl(<SkillDrawer target={target} onClose={vi.fn()} workflowCreatorEnabled meId="usr_me" teamRow={null} />);

    fireEvent.click(await screen.findByRole("button", { name: "提交到团队" }));
    await waitFor(() => expect(fetchSkillDetail).toHaveBeenCalledTimes(2));
    await waitFor(() => expect((screen.getByRole("button", { name: "提交到团队" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交到团队" }));
    await waitFor(() => expect(promoteSkill).toHaveBeenCalledTimes(2));
    expect(vi.mocked(promoteSkill).mock.calls.map((call) => call[0].content)).toEqual(["# 旧正文", "# 新正文"]);
  });
});
