/** scheduled-recipe-runs S2 · frontend entry for solidifying an entire conversation (116 dogfood P1-C).
 *
 * Backend `POST /workflow-solidify/distill {session_id}` has long worked; distillation quality was verified on 116
 * (three trial-and-error turns → steps retain only the final criteria), but **nowhere in the frontend can send session_id**: deployed
 * artifacts have only one distill call site across the site, using `{turn_id, form_hint}`. Users can only solidify turns individually,
 * receiving refuted approaches or partial workflows missing data-fetch steps—the feature is unreachable overall. These tests lock that leg.
 */
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const distillWorkflowMock = vi.fn();
const distillSessionMock = vi.fn();

vi.mock("@/lib/workflow-solidify-api", () => ({
  distillWorkflow: (...a: unknown[]) => distillWorkflowMock(...a),
  distillSession: (...a: unknown[]) => distillSessionMock(...a),
  createPersonalSkill: vi.fn(),
  createTeamSkill: vi.fn(),
  dryRunRecipe: vi.fn(),
}));

import { CreateWorkflowCard } from "./create-workflow-card";

const SESSION_DISTILL = {
  ok: true,
  suggested_form: "skill" as const,
  name: "store-revenue-top10-natural-month",
  display_name: "门店自然月营业额Top10与集中度",
  description: "按自然月统计门店营业额 Top10 并给出集中度结论",
  body_md: "1. 确定当前自然月\n2. 查营业额取前十\n3. 算第一名占比",
  subagent_spec: {} as never,
  tool_refs: ["bi_query"],
  slots: [],
  checkpoints: [],
  scripts: [],
  truncated_steps: [],
  source: "session" as const,
  turn_count: 3,
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("sessionId 在场 → 走 session 腿，而不是单轮腿", async () => {
  distillSessionMock.mockResolvedValue(SESSION_DISTILL);

  renderIntl(<CreateWorkflowCard turnId={null} sessionId="s_1" hint={null} onClose={vi.fn()} />);

  await waitFor(() => expect(distillSessionMock).toHaveBeenCalledWith("s_1"));
  expect(distillWorkflowMock).not.toHaveBeenCalled();
});

it("整段固化要如实说合并了几轮（用户点的是「整段」，得看见它确实吃进了多轮）", async () => {
  distillSessionMock.mockResolvedValue(SESSION_DISTILL);

  renderIntl(<CreateWorkflowCard turnId={null} sessionId="s_1" hint={null} onClose={vi.fn()} />);

  // Copy combines pre + <b>turn count</b> + post; assert the complete readable text (not just that "3":
  // the stepper also contains numbers, so that assertion could pass without proving what the user reads).
  await waitFor(() =>
    expect(document.body.textContent).toContain("已合并整段对话的 3 轮做过事的轮次"),
  );
});

it("会话被收窗时明说只覆盖了最近若干轮，不假装固化了整段", async () => {
  distillSessionMock.mockResolvedValue({
    ...SESSION_DISTILL,
    truncated_notice_code: "workflow_session_too_large",
  });

  renderIntl(<CreateWorkflowCard turnId={null} sessionId="s_1" hint={null} onClose={vi.fn()} />);

  expect(await screen.findByText(/只覆盖了最近的若干轮/)).toBeTruthy();
});

it("没有 sessionId 时维持单轮腿（既有入口零行为变化）", async () => {
  distillWorkflowMock.mockResolvedValue({ ...SESSION_DISTILL, source: undefined, turn_count: undefined });

  renderIntl(<CreateWorkflowCard turnId="t_1" hint={null} onClose={vi.fn()} />);

  await waitFor(() => expect(distillWorkflowMock).toHaveBeenCalledWith("t_1"));
  expect(distillSessionMock).not.toHaveBeenCalled();
  expect(screen.queryByText(/已合并整段对话的/)).toBeNull();
});

it("两个目标都没有 → 报错而不是发一个空请求", async () => {
  renderIntl(<CreateWorkflowCard turnId={null} hint={null} onClose={vi.fn()} />);

  expect(await screen.findByText(/没有可固化的轮次/)).toBeTruthy();
  expect(distillSessionMock).not.toHaveBeenCalled();
  expect(distillWorkflowMock).not.toHaveBeenCalled();
});
