/** scheduled-recipe-runs S1 · Set as Scheduled Task entry on the solidification success card (PRD FR-3.1).
 *
 * Lock **preservation of structured references**: save() formerly retained only a name string,
 * discarding the id returned by createTeamSkill in team mode. team_skill binds by id; without it, this entry
 * can only guess by name, and a wrong guess binds someone else's slot.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const distillWorkflowMock = vi.fn();
const createPersonalSkillMock = vi.fn();
const createTeamSkillMock = vi.fn();
const dryRunRecipeMock = vi.fn();

vi.mock("@/lib/workflow-solidify-api", () => ({
  distillWorkflow: (...a: unknown[]) => distillWorkflowMock(...a),
  createPersonalSkill: (...a: unknown[]) => createPersonalSkillMock(...a),
  createTeamSkill: (...a: unknown[]) => createTeamSkillMock(...a),
  dryRunRecipe: (...a: unknown[]) => dryRunRecipeMock(...a),
}));

import { CreateWorkflowCard } from "./create-workflow-card";

const DISTILL = {
  ok: true,
  suggested_form: "skill" as const,
  name: "weekly-report",
  display_name: "周报生成",
  description: "汇总本周经营数据",
  body_md: "1. 取数\n2. 出报告",
  subagent_spec: {} as never,
  tool_refs: [],
  slots: [],
  checkpoints: ["报告写出毛利率"],
  scripts: [],
  truncated_steps: [],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function solidify(form: "personal" | "team") {
  distillWorkflowMock.mockResolvedValue(DISTILL);
  renderIntl(
    <CreateWorkflowCard turnId="t_1" hint={null} onClose={vi.fn()} />,
  );
  // Preview → form → save (no parameter slots in this case; Preview Next goes directly to the form page)
  fireEvent.click(await screen.findByText("下一步 · 形态"));
  if (form === "team") {
    fireEvent.click(await screen.findByText("团队共享技能"));
  }
  fireEvent.click(await screen.findByText("保存固化"));
}

function scheduleLink(): HTMLAnchorElement | null {
  return document.querySelector<HTMLAnchorElement>("a[data-testid='solidify-schedule']");
}

describe("CreateWorkflowCard 「设为定时任务」入口", () => {
  it("个人形态：入口带 personal_skill:<name> 引用", async () => {
    createPersonalSkillMock.mockResolvedValue({ name: "weekly-report-2" });
    await solidify("personal");

    await waitFor(() => expect(scheduleLink()).toBeTruthy());
    const href = scheduleLink()!.getAttribute("href") ?? "";
    expect(href).toContain("/team/settings/scheduled/");
    expect(decodeURIComponent(href)).toContain("bind=personal_skill:weekly-report-2");
  });

  it("团队形态：入口带 team_skill:<返回的 id>（不是名字）", async () => {
    createTeamSkillMock.mockResolvedValue({ id: "sk_abc", name: "weekly-report" });
    await solidify("team");

    await waitFor(() => expect(scheduleLink()).toBeTruthy());
    const href = decodeURIComponent(scheduleLink()!.getAttribute("href") ?? "");
    expect(href).toContain("bind=team_skill:sk_abc");
    expect(href).not.toContain("weekly-report");
  });

  it("保存前不出这个入口（没引用可带）", async () => {
    distillWorkflowMock.mockResolvedValue(DISTILL);
    renderIntl(
      <CreateWorkflowCard turnId="t_1" hint={null} onClose={vi.fn()} />,
    );
    expect(await screen.findByText("下一步 · 形态")).toBeTruthy();
    expect(scheduleLink()).toBeNull();
  });
});
