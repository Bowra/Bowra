/**
 * SkillDrawer · Files tab for a team-shared skill.
 *
 * User reported “skill 发布到团队后关联的 skill 没了”: team copies always carried references on disk,
 * but the team drawer previously rendered only SKILL.md body, without a file view. Lock the repaired user experience:
 * team targets also show Files (N); clicking entries opens a secondary content view, using **team endpoints** addressed by row ID,
 * not skill name—personal endpoints always return 404 for team rows.
 */

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { SkillDrawer, type SkillDrawerTarget } from "./skill-drawer";

const { getTeamSkill, getTeamSkillFile, fetchSkillDetail, fetchSkillFile } = vi.hoisted(() => ({
  getTeamSkill: vi.fn(),
  getTeamSkillFile: vi.fn(),
  fetchSkillDetail: vi.fn(),
  fetchSkillFile: vi.fn(),
}));

vi.mock("@/lib/workflow-solidify-api", () => ({
  getTeamSkill,
  getTeamSkillFile,
  copyTeamSkillToPersonal: vi.fn(),
  promoteSkill: vi.fn(),
  withdrawTeamSkill: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ fetchSkillDetail, fetchSkillFile }));

vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn() }) }));

const TEAM_ROW = {
  id: "oskill_1",
  team_id: "team_a",
  name: "channel-gm-recipe",
  display_name: "渠道毛利核对",
  description: "渠道毛利核对",
  visibility: "team_wide",
  status: "enabled",
  verdict: "pass",
  size_bytes: 53849,
  created_by: "u_b",
  created_by_name: "同事",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  approval: "approved",
};

const TARGET: SkillDrawerTarget = { kind: "team", skill: TEAM_ROW as never, canManage: false, canEdit: false };

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SkillDrawer · 团队 skill 文件面", () => {
  it("列出 references/scripts 并从团队端点读取单文件", async () => {
    getTeamSkill.mockResolvedValue({
      ...TEAM_ROW,
      body_md: "# 渠道毛利\n步骤...",
      references: [{ name: "metrics.md", size: 29960 }],
      scripts: [{ name: "step_1.py", size: 120 }],
      evals_count: 0,
      has_evals: false,
    });
    getTeamSkillFile.mockResolvedValue({
      path: "references/metrics.md",
      content: "# 口径\n毛利=收入-成本",
      size: 29960,
      mime: "text/markdown",
    });

    renderIntl(<SkillDrawer target={TARGET} onClose={() => {}} />);

    const descriptionTab = await screen.findByRole("tab", { name: "说明" });
    const filesTab = await screen.findByRole("tab", { name: /文件/ });
    descriptionTab.focus();
    fireEvent.keyDown(descriptionTab, { key: "ArrowRight" });
    expect(filesTab.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(filesTab);
    expect(await screen.findByText("metrics.md")).toBeTruthy();
    expect(screen.getByText("step_1.py")).toBeTruthy();

    fireEvent.click(screen.getByText("metrics.md"));
    await waitFor(() => expect(screen.getByText(/毛利=收入-成本/)).toBeTruthy());
    // Address by team row ID; never call the personal endpoint (by skill name), which always returns 404 for team rows.
    expect(getTeamSkillFile).toHaveBeenCalledWith("oskill_1", "references", "metrics.md");
    expect(fetchSkillFile).not.toHaveBeenCalled();
  });

  it("没有附属文件时不出「文件」tab（老形态不回退）", async () => {
    getTeamSkill.mockResolvedValue({ ...TEAM_ROW, body_md: "# 渠道毛利", references: [], scripts: [] });

    renderIntl(<SkillDrawer target={TARGET} onClose={() => {}} />);

    await screen.findByText(/渠道毛利/);
    expect(screen.queryByRole("tab", { name: /文件/ })).toBeNull();
  });

  it("老后端不下发 bundle 字段时按空处理，不崩", async () => {
    getTeamSkill.mockResolvedValue({ ...TEAM_ROW, body_md: "# 渠道毛利" });

    renderIntl(<SkillDrawer target={TARGET} onClose={() => {}} />);

    await screen.findByText(/渠道毛利/);
    expect(screen.queryByRole("tab", { name: /文件/ })).toBeNull();
  });

  /* Negative control: personal skills must behave exactly as before—the same tabs/file view, still using personal endpoints. */
  it("个人 skill 仍走个人端点（按 skill 名寻址）", async () => {
    fetchSkillDetail.mockResolvedValue({
      name: "dupont",
      title: "杜邦分析",
      source_type: "user_upload",
      enabled: true,
      size_bytes: 4200,
      body_md: "# 杜邦",
      allowed_tools: [],
      scripts: [],
      trigger_examples: [],
      references: [{ name: "dupont-5step.md", size: 4200 }],
      evals_count: 0,
      has_evals: false,
      source_turn_id: null,
    });
    fetchSkillFile.mockResolvedValue({
      path: "references/dupont-5step.md",
      content: "五步拆解",
      size: 4200,
      mime: "text/markdown",
    });

    renderIntl(
      <SkillDrawer
        target={{ kind: "skill", skill: { name: "dupont", title: "杜邦分析", source_type: "user_upload", enabled: true, size_bytes: 4200 } as never }}
        onClose={() => {}}
      />,
    );

    fireEvent.click(await screen.findByRole("tab", { name: /文件/ }));
    fireEvent.click(await screen.findByText("dupont-5step.md"));
    await waitFor(() => expect(screen.getByText("五步拆解")).toBeTruthy());
    expect(fetchSkillFile).toHaveBeenCalledWith("dupont", "references", "dupont-5step.md");
    expect(getTeamSkillFile).not.toHaveBeenCalled();
  });
});
