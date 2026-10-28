import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import {
  deleteSkill,
  fetchSkills,
  patchSkill,
  patchSkillsBulk,
  previewSkillImpact,
  type SkillSummaryDTO,
} from "@/lib/api";
import { deleteTeamSkill, listTeamSkills } from "@/lib/workflow-solidify-api";
import { SkillsSettingsContent } from "./skills-settings-content";

vi.mock("@/lib/api", () => ({
  deleteSkill: vi.fn(),
  fetchSkills: vi.fn(),
  patchSkill: vi.fn(),
  patchSkillsBulk: vi.fn(),
  previewSkillImpact: vi.fn(),
}));
vi.mock("@/lib/workflow-solidify-api", () => ({
  deleteTeamSkill: vi.fn(),
  listTeamSkills: vi.fn(),
  patchTeamSkill: vi.fn(),
  previewTeamSkillImpact: vi.fn(),
}));
vi.mock("@/lib/auth-api", () => ({
  fetchMe: vi.fn().mockResolvedValue({
    id: "user_1",
    email: "user@example.com",
    display_name: "User",
    is_team_admin: false,
  }),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({
    push: vi.fn(),
  }),
}));
vi.mock("@/components/skill-upload-modal", () => ({
  SkillUploadModal: () => null,
}));
vi.mock("@/components/skill-drawer", () => ({
  SkillDrawer: ({ target }: { target: { skill: { name: string }; prefill?: { candidate_id: string } | null } }) => (
    <div data-testid="skill-drawer" data-prefill={target.prefill?.candidate_id ?? ""}>{target.skill.name}</div>
  ),
}));
vi.mock("@/components/share/share-dialog", () => ({
  ShareDialog: ({ resourceId }: { resourceId: string }) => <div data-testid="share-dialog">{resourceId}</div>,
}));

const sampleSkill: SkillSummaryDTO = {
  name: "sample-analysis",
  title: "示例分析技能",
  description: "用于验证技能列表交互",
  version: "1.0.0",
  source_type: "builtin",
  enabled: true,
  size_bytes: 128,
  created_at: "2026-07-24T09:00:00Z",
  category: "analysis",
};

describe("SkillsSettingsContent interaction boundaries", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/skills/");
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  async function renderLoadedSkills({ expandAnalysis = true } = {}) {
    vi.mocked(fetchSkills).mockResolvedValue({
      builtin: [sampleSkill],
      user_upload: [],
      user_generated: [],
    });
    vi.mocked(listTeamSkills).mockResolvedValue({ items: [] });
    vi.mocked(patchSkill).mockResolvedValue({
      ...sampleSkill,
      body_md: "",
      allowed_tools: [],
      scripts: [],
      source_turn_id: null,
      trigger_examples: [],
      references: [],
      evals_count: 0,
      has_evals: false,
    });
    vi.mocked(patchSkillsBulk).mockResolvedValue({
      items: [],
      errors: [],
    });

    renderIntl(<SkillsSettingsContent />);
    const analysisGroup = (await screen.findAllByRole("button", { name: /分析方法/ }))
      .find((button) => button.hasAttribute("aria-expanded"));
    expect(analysisGroup).toBeTruthy();
    if (expandAnalysis && analysisGroup?.getAttribute("aria-expanded") === "false") {
      fireEvent.click(analysisGroup);
    }
    if (expandAnalysis) expect(await screen.findByText("示例分析技能")).toBeTruthy();
    return analysisGroup as HTMLButtonElement;
  }

  it("keeps built-in groups collapsed until the user asks for them", async () => {
    const analysisGroup = await renderLoadedSkills({ expandAnalysis: false });

    expect(analysisGroup.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("示例分析技能")).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索技能名称或描述…" }), {
      target: { value: "示例分析" },
    });
    expect(await screen.findByText("示例分析技能")).toBeTruthy();
  });

  it("separates built-in document uses while preserving filtered bulk targets", async () => {
    const documents = [
      { ...sampleSkill, name: "pptx", title: "Presentation guide", description: "Read and write presentations.", category: "documents" },
      { ...sampleSkill, name: "bowra-pptx", title: "Basic presentation", description: "Create a basic deck.", category: "documents" },
      { ...sampleSkill, name: "canvas-pptx", title: "Report presentation", description: "Export the existing report.", category: "documents" },
    ];
    vi.mocked(fetchSkills).mockResolvedValue({
      builtin: documents,
      user_upload: [{ ...documents[2], title: "Personal export", source_type: "user_upload" }],
      user_generated: [],
    });
    vi.mocked(listTeamSkills).mockResolvedValue({ items: [] });
    vi.mocked(patchSkillsBulk).mockResolvedValue({ items: [], errors: [] });

    renderIntl(<SkillsSettingsContent />);
    const toggle = (await screen.findAllByRole("button", { name: /文档与导出/ }))
      .find((button) => button.hasAttribute("aria-expanded"));
    expect(toggle).toBeTruthy();
    fireEvent.click(toggle!);

    const guides = screen.getByRole("region", { name: "方法与写作参考" });
    const creation = screen.getByRole("region", { name: "基础文档生成" });
    const exports = screen.getByRole("region", { name: "已有报告导出" });
    expect(within(guides).getByText("Presentation guide")).toBeTruthy();
    expect(within(creation).getByText("Basic presentation")).toBeTruthy();
    expect(within(exports).getByText("Report presentation")).toBeTruthy();
    expect(within(exports).queryByText("Personal export")).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索技能名称或描述…" }), {
      target: { value: "existing report" },
    });
    expect(screen.queryByRole("region", { name: "方法与写作参考" })).toBeNull();
    expect(screen.queryByRole("region", { name: "基础文档生成" })).toBeNull();
    const report = screen.getByRole("region", { name: "已有报告导出" });
    const catalogGroup = report.closest(".sgroup")! as HTMLElement;
    fireEvent.click(within(catalogGroup).getByRole("button", { name: "全部停用" }));
    await waitFor(() => expect(patchSkillsBulk).toHaveBeenCalledWith([{ name: "canvas-pptx", enabled: false }]));
  });

  it("does not open details when the per-user switch is toggled", async () => {
    await renderLoadedSkills();

    const toggle = screen.getByRole("switch", { name: "启停 示例分析技能" });
    toggle.focus();
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(patchSkill).toHaveBeenCalledWith("sample-analysis", {
        enabled: false,
      }),
    );
    expect(screen.queryByTestId("skill-drawer")).toBeNull();
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("switch", { name: "启停 示例分析技能" }),
      ),
    );
  });

  it("targets a Team Skill preference by stable id", async () => {
    vi.mocked(fetchSkills).mockResolvedValue({
      builtin: [],
      user_upload: [{ ...sampleSkill, name: "same-name", title: "个人同名", source_type: "user_upload" }],
      user_generated: [],
    });
    vi.mocked(listTeamSkills).mockResolvedValue({
      items: [{
        id: "oskill_team",
        name: "same-name",
        display_name: "团队同名",
        description: "团队 Skill",
        visibility: "team_wide",
        status: "enabled",
        verdict: null,
        size_bytes: 10,
        approval: "approved",
        my_enabled: true,
      }],
    });
    vi.mocked(patchSkill).mockResolvedValue({
      ...sampleSkill,
      name: "same-name",
      enabled: false,
      body_md: "",
      allowed_tools: [],
      scripts: [],
      source_turn_id: null,
      trigger_examples: [],
      references: [],
      evals_count: 0,
      has_evals: false,
    });

    renderIntl(<SkillsSettingsContent />);
    fireEvent.click(await screen.findByRole("switch", { name: "启停 团队同名" }));

    await waitFor(() => expect(patchSkill).toHaveBeenCalledWith("same-name", {
      enabled: false,
      team_skill_id: "oskill_team",
    }));
  });

  it("keeps the Team Skill id through guarded preview and confirmation", async () => {
    vi.mocked(fetchSkills).mockResolvedValue({
      impact_guard_enabled: true,
      builtin: [],
      user_upload: [],
      user_generated: [],
    });
    vi.mocked(listTeamSkills).mockResolvedValue({
      impact_guard_enabled: true,
      items: [{
        id: "oskill_guarded",
        name: "guarded-team",
        display_name: "受保护团队技能",
        description: "团队 Skill",
        visibility: "team_wide",
        status: "enabled",
        verdict: null,
        size_bytes: 10,
        approval: "approved",
        my_enabled: true,
      }],
    });
    vi.mocked(previewSkillImpact).mockResolvedValue({
      kind: "resource_change_impact",
      schema_version: 1,
      action: "patch",
      blocking: false,
      summary: {
        current_agent_count: 0,
        running_task_count: 0,
        draft_agent_count: 0,
        historical_agent_count: 0,
        scheduled_binding_count: 0,
        restricted_count: 0,
        completeness: "complete",
        reason_codes: [],
      },
      new_admission: "blocked_for_affected_agents",
      running_execution: "future_dispatch_blocked",
      already_sent_effect: "not_reversible",
      required_follow_up: [],
      impact_token: "team-preference-impact",
      expires_in_seconds: 300,
      command: { action: "patch", proposed_change: { enabled: false } },
      reason_codes: [],
    });

    renderIntl(<SkillsSettingsContent />);
    fireEvent.click(await screen.findByRole("switch", { name: "启停 受保护团队技能" }));

    await waitFor(() => expect(previewSkillImpact).toHaveBeenCalledWith("guarded-team", {
      action: "patch",
      proposed_change: { enabled: false },
      team_skill_id: "oskill_guarded",
    }));
    fireEvent.click(await screen.findByRole("button", { name: "确认停用" }));
    await waitFor(() => expect(patchSkill).toHaveBeenCalled());
    expect(vi.mocked(patchSkill).mock.calls[0][1]).toEqual({
      enabled: false,
      team_skill_id: "oskill_guarded",
    });
  });

  it("reviews impact before disabling when the guard is enabled", async () => {
    vi.mocked(fetchSkills).mockResolvedValue({
      impact_guard_enabled: true,
      builtin: [sampleSkill],
      user_upload: [],
      user_generated: [],
    });
    vi.mocked(listTeamSkills).mockResolvedValue({ items: [], impact_guard_enabled: true });
    vi.mocked(previewSkillImpact).mockResolvedValue({
      kind: "resource_change_impact",
      schema_version: 1,
      action: "patch",
      blocking: false,
      summary: {
        current_agent_count: 2,
        running_task_count: 1,
        draft_agent_count: 0,
        historical_agent_count: 0,
        scheduled_binding_count: 1,
        restricted_count: 0,
        completeness: "complete",
        reason_codes: [],
      },
      new_admission: "blocked_for_affected_agents",
      running_execution: "future_dispatch_blocked",
      already_sent_effect: "not_reversible",
      required_follow_up: [{ kind: "schedule_reconfirm", job_id: "job_1" }],
      impact_token: "impact-token",
      expires_in_seconds: 300,
      command: { action: "patch", proposed_change: { enabled: false } },
      reason_codes: [],
    });
    vi.mocked(patchSkill).mockResolvedValue({
      ...sampleSkill,
      enabled: false,
      body_md: "",
      allowed_tools: [],
      scripts: [],
      source_turn_id: null,
      trigger_examples: [],
      references: [],
      evals_count: 0,
      has_evals: false,
    });

    renderIntl(<SkillsSettingsContent />);
    const analysisGroup = (await screen.findAllByRole("button", { name: /分析方法/ }))
      .find((button) => button.hasAttribute("aria-expanded"));
    if (analysisGroup?.getAttribute("aria-expanded") === "false") fireEvent.click(analysisGroup);
    fireEvent.click(await screen.findByRole("switch", { name: "启停 示例分析技能" }));

    expect(await screen.findByText("2 个当前智能体")).toBeTruthy();
    expect(patchSkill).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认停用" }));
    await waitFor(() => expect(patchSkill).toHaveBeenCalled());
    const guard = vi.mocked(patchSkill).mock.calls[0][2];
    expect(guard?.impactToken).toBe("impact-token");
    expect(guard?.idempotencyKey.length).toBeGreaterThanOrEqual(16);
  });

  it("opens the impact confirmation directly for protected deletion", async () => {
    const personalSkill: SkillSummaryDTO = {
      ...sampleSkill,
      id: "sk_personal",
      source_type: "user_upload",
    };
    vi.mocked(fetchSkills).mockResolvedValue({
      impact_guard_enabled: true,
      builtin: [],
      user_upload: [personalSkill],
      user_generated: [],
    });
    vi.mocked(listTeamSkills).mockResolvedValue({ items: [], impact_guard_enabled: true });
    vi.mocked(previewSkillImpact).mockResolvedValue({
      kind: "resource_change_impact",
      schema_version: 1,
      action: "delete",
      blocking: false,
      summary: {
        current_agent_count: 0,
        running_task_count: 0,
        draft_agent_count: 0,
        historical_agent_count: 0,
        scheduled_binding_count: 0,
        restricted_count: 0,
        completeness: "complete",
        reason_codes: [],
      },
      new_admission: "blocked_for_affected_agents",
      running_execution: "future_dispatch_blocked",
      already_sent_effect: "not_reversible",
      required_follow_up: [],
      impact_token: "delete-impact-token",
      expires_in_seconds: 300,
      command: { action: "delete", proposed_change: {} },
      reason_codes: [],
    });
    vi.mocked(deleteSkill).mockResolvedValue();

    renderIntl(<SkillsSettingsContent />);
    await chooseRowAction("示例分析技能 的更多操作", /^删除/, { danger: true });

    expect(await screen.findByRole("button", { name: "确认删除" })).toBeTruthy();
    expect(screen.queryByText("技能文件将被移除，不可恢复。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(deleteSkill).toHaveBeenCalled());
    expect(vi.mocked(deleteSkill).mock.calls[0][0]).toBe("sk_personal");
    expect(vi.mocked(deleteSkill).mock.calls[0][1]?.impactToken).toBe("delete-impact-token");
  });

  it("exposes one detail entry per skill row", async () => {
    await renderLoadedSkills();

    expect(
      screen.getAllByRole("button", { name: "详情 示例分析技能" }),
    ).toHaveLength(1);
  });

  it("does not collapse the group when a bulk action is used", async () => {
    await renderLoadedSkills();

    fireEvent.click(screen.getByRole("button", { name: "全部停用" }));

    await waitFor(() =>
      expect(patchSkillsBulk).toHaveBeenCalledWith([
        { name: "sample-analysis", enabled: false },
      ]),
    );
    expect(screen.getByText("示例分析技能")).toBeTruthy();
    expect(
      screen
        .getByRole("button", { expanded: true, name: /分析方法/ })
        .getAttribute("aria-expanded"),
    ).toBe("true");
  });

  it("does not render raw exception prose when the list request fails", async () => {
    const secret = "password=hunter2 at /srv/private/config.yaml";
    vi.mocked(fetchSkills).mockRejectedValue(new Error(secret));
    vi.mocked(listTeamSkills).mockResolvedValue({ items: [] });

    renderIntl(<SkillsSettingsContent />);

    expect(
      // A thrown Error is not a browser transport failure (fe927ae3e), so the generic copy applies.
      await screen.findByText("操作未完成，请稍后重试。"),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain(secret);
  });

  it("preserves Agent repair context and applies the dependency search", async () => {
    window.history.replaceState({}, "", "/skills/?from_agent=osa_1&dependency=sample-analysis");
    await renderLoadedSkills({ expandAnalysis: false });

    expect(screen.getByText("正在筛选必要依赖“sample-analysis”相关的 Skill。")).toBeTruthy();
    expect(screen.getByRole("searchbox", { name: "搜索技能名称或描述…" }).getAttribute("value")).toBe("sample-analysis");
    expect(screen.getByText("示例分析技能")).toBeTruthy();
    expect(screen.getByRole("link", { name: /返回智能体详情/ }).getAttribute("href")).toContain("id=osa_1");
  });

  it("opens the team skill drawer with the feedback prefill from a flagged deep link, and only then", async () => {
    // agent-feedback-loop frontend batch C: peek at handoff on this unconditionally mounted page, resolving it only after me + team list both arrive.
    const teamRow = {
      id: "oskill_1", team_id: "team_a", name: "chart-style", display_name: "图表规范", description: "",
      visibility: "team_wide", approval: "approved", status: "enabled", created_by: "u_other",
    } as never;
    const stash = () => sessionStorage.setItem("bowhead:feedback-prefill", JSON.stringify({
      candidate_id: "fbc_sk", target_kind: "team_skill", target_ref: "oskill_1",
      prefill: { kind: "team_skill_body", edited_content: "x", rationale: "" }, base: {},
      banner: { candidate_id: "fbc_sk", proposer_id: "u", evidence_count: 1 },
    }));
    vi.mocked(fetchSkills).mockResolvedValue({ builtin: [], user_upload: [], user_generated: [] });
    vi.mocked(listTeamSkills).mockResolvedValue({ items: [teamRow] });

    window.history.replaceState({}, "", "/skills/?skill=oskill_1&feedback_prefill=1");
    stash();
    renderIntl(<SkillsSettingsContent />);
    const drawer = await screen.findByTestId("skill-drawer");
    expect(drawer.getAttribute("data-prefill")).toBe("fbc_sk");
    cleanup();

    // Without the marker, clear a stale key and do not open the drawer.
    window.history.replaceState({}, "", "/skills/?skill=oskill_1");
    stash();
    renderIntl(<SkillsSettingsContent />);
    await screen.findByText("图表规范");
    expect(screen.queryByTestId("skill-drawer")).toBeNull();
    expect(sessionStorage.getItem("bowhead:feedback-prefill")).toBeNull();
  });
});

describe("待处理导入徽标（skill-import 批3；re-review 指出此前零覆盖）", () => {
  // The earlier block registers cleanup only within its own describe; without our own registration here,
  // previous-case DOM survives, falsely failing assertions that no badge exists.
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  const row = (name: string, importStatus: string | null): SkillSummaryDTO =>
    ({ ...sampleSkill, name, title: name, source_type: "user_upload", import_status: importStatus }) as SkillSummaryDTO;

  async function renderRows(rows: SkillSummaryDTO[]) {
    vi.mocked(fetchSkills).mockResolvedValue({ builtin: [], user_upload: rows, user_generated: [] });
    vi.mocked(listTeamSkills).mockResolvedValue({ items: [] });
    renderIntl(<SkillsSettingsContent />);
    await screen.findByText(rows[0].title!);
  }

  it("pending 的行必须自己会说话——开关显示「开」而它其实不生效", async () => {
    await renderRows([row("pending-one", "pending")]);
    expect(screen.getByText("待处理导入")).toBeTruthy();
  });

  it("cleared / overridden / 无状态的行不挂徽标", async () => {
    await renderRows([row("a", null), row("b", "cleared"), row("c", "overridden")]);
    expect(screen.queryByText("待处理导入")).toBeNull();
  });
});

describe("team skill row “⋯” menu", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });
  const arrange = () => {
    vi.mocked(fetchSkills).mockResolvedValue({ builtin: [], user_upload: [], user_generated: [] });
    vi.mocked(listTeamSkills).mockResolvedValue({
      items: [{
        id: "oskill_mine",
        name: "weekly-review",
        display_name: "周报复盘",
        description: "团队 Skill",
        visibility: "team_wide",
        status: "enabled",
        verdict: null,
        size_bytes: 10,
        approval: "approved",
        my_enabled: true,
        created_by: "user_1",
      }],
    } as never);
    renderIntl(<SkillsSettingsContent />);
  };

  it("the menu is Share settings | Delete; Share settings opens the share dialog, not the delete confirmation", async () => {
    arrange();
    const menu = await openRowMenu("周报复盘 的更多操作");
    expect(menuItemNames(menu)).toEqual(["分享设置", "删除…"]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "分享设置" }));
    expect((await screen.findByTestId("share-dialog")).textContent).toBe("oskill_mine");
    expect(screen.queryByRole("dialog", { name: "删除「周报复盘」？" })).toBeNull();
    expect(deleteTeamSkill).not.toHaveBeenCalled();
  });

  it("Delete is a danger item that opens the confirmation first, without deleting or opening sharing", async () => {
    arrange();
    await chooseRowAction("周报复盘 的更多操作", "删除…", { danger: true });
    expect(await screen.findByRole("dialog", { name: "删除「周报复盘」？" })).toBeTruthy();
    expect(screen.queryByTestId("share-dialog")).toBeNull();
    expect(deleteTeamSkill).not.toHaveBeenCalled();
  });
});

describe("SkillsSettingsContent verified badge glyph", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders ✓ before the verified label", async () => {
    vi.mocked(fetchSkills).mockResolvedValue({ builtin: [], user_upload: [], user_generated: [] });
    vi.mocked(listTeamSkills).mockResolvedValue({
      items: [{
        id: "oskill_verified",
        name: "verified-skill",
        display_name: "已验证技能",
        description: "团队 Skill",
        visibility: "team_wide",
        status: "enabled",
        verdict: "pass",
        size_bytes: 10,
        approval: "approved",
        my_enabled: true,
      }],
    });

    renderIntl(<SkillsSettingsContent />);
    const message = catalogText("zh-CN", "skills_page.badge_verified");
    const badge = (await screen.findAllByText((_, el) => el?.classList.contains("verified") === true && el.classList.contains("sbadge")))[0];
    expectRefreshedGlyph(badge, "✓", "start", message);
  });
});
