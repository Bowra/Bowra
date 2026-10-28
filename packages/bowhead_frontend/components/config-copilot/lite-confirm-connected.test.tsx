/** LiteConfirmConnected state-machine + localization tests (tech-design-batch1-draft-store.md §9, component half of tests 15-18).
 *
 * Batch 1-a: hydrate terminal state from the server (GET); apply/undo/ignore forward POSTs. Lock wiring:
 * propose→apply→applied (undo visible)→undo→reverted; failure+retry; ignore; hydrate 404→stale,
 * non-interactive; remount (F5) rehydrates terminal state; localized rows (unmapped fallback + workspace name).
 */

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => navigation }));

vi.mock("@/lib/api-client", () => {
  class ApiError extends Error {
    status: number;
    body?: unknown;
    constructor(status: number, message: string, options: { body?: unknown } = {}) {
      super(message);
      this.status = status;
      this.body = options.body;
    }
  }
  return { apiJson: vi.fn(), ApiError };
});
vi.mock("@/lib/use-theme-config", () => ({
  refreshEffectiveTheme: vi.fn(async () => true),
}));

import { ApiError, apiJson } from "@/lib/api-client";
import { _dropLiteStateCacheForTests, applyLiteProposal, refreshLiteState, resetLiteForTests } from "@/lib/config-copilot-lite";
import type { ConfigLiteProposePayload } from "@/lib/types";
import { refreshEffectiveTheme } from "@/lib/use-theme-config";
import { WORKSPACE_INSTRUCTIONS_SAVED_EVENT } from "@/lib/workspace-settings-notifications";

import { LiteConfirmConnected } from "./lite-confirm-connected";

const apiMock = vi.mocked(apiJson);

const PROPOSAL: ConfigLiteProposePayload = {
  proposal_id: "ccd_p1",
  turn_id: "t_1",
  form_id: "user_appearance",
  workspace_id: null,
  workspace_name: null,
  rows: [
    { field: "mode", from: "light", to: "dark", from_unset: true },
    {
      field: "chartPalette",
      from: "default",
      to: "colorblind-safe",
      from_unset: true,
    },
  ],
  notes: {},
};

function dto(status: string, proposalId = "ccd_p1") {
  return {
    proposal_id: proposalId,
    form_id: "user_appearance",
    status,
    workspace_id: null,
  };
}

/** Default mount-time hydrate response (proposed). */
function mockHydrate(status = "proposed", proposalId = "ccd_p1") {
  apiMock.mockResolvedValueOnce(dto(status, proposalId));
}

beforeEach(() => {
  resetLiteForTests();
  apiMock.mockReset();
  vi.mocked(refreshEffectiveTheme).mockClear();
});

afterEach(() => {
  cleanup();
  resetLiteForTests();
});

describe("LiteConfirmConnected", () => {
  it.each(["proposed", "applied"])("preserves the server's %s state after a baseline conflict and asks for a new suggestion", async (status) => {
    mockHydrate(status);
    applyLiteProposal({ ...PROPOSAL, form_id: "workspace_i18n" });
    renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    if (status === "applied") await screen.findByTestId("lite-applied");
    else await waitFor(() => expect(apiMock).toHaveBeenCalled());
    apiMock.mockRejectedValueOnce(
      new ApiError(409, "conflict", {
        body: {
          detail: { error: { code: "configuration_baseline_changed" } },
        },
      }),
    );
    fireEvent.click(screen.getByText(status === "applied" ? "撤销" : "应用"));
    const conflict = await screen.findByTestId("lite-conflict");
    expect(conflict.textContent).toContain("再在对话中重新提出");
    expect(screen.queryByText("重试")).toBeNull();
    expect(refreshEffectiveTheme).not.toHaveBeenCalled();
  });
  it("propose 渲染本地化 rows（设计画面 C 措辞：外观模式 浅色→深色 / 图表配色 →色觉友好）", () => {
    mockHydrate();
    applyLiteProposal(PROPOSAL);
    renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    expect(screen.getByTestId("lite-propose")).toBeTruthy();
    expect(screen.getByText("外观模式")).toBeTruthy();
    expect(screen.getByText("浅色")).toBeTruthy();
    expect(screen.getByText("深色")).toBeTruthy();
    expect(screen.getByText("图表配色")).toBeTruthy();
    expect(screen.getByText("色觉友好")).toBeTruthy();
  });

  it("apply 成功 → POST /apply → applied + 撤销可见 + 主题即刻刷新；undo → reverted", async () => {
    mockHydrate();
    applyLiteProposal(PROPOSAL);
    renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    apiMock.mockResolvedValueOnce(dto("applied"));
    fireEvent.click(screen.getByText("应用"));
    await waitFor(() => expect(screen.queryByTestId("lite-applied")).toBeTruthy());
    expect(apiMock.mock.calls.some((c) => c[0] === "/api/v1/config-proposals/ccd_p1/apply")).toBe(true);
    expect(refreshEffectiveTheme).toHaveBeenCalled();

    apiMock.mockResolvedValueOnce(dto("reverted"));
    fireEvent.click(screen.getByText("撤销"));
    await waitFor(() => expect(screen.queryByText("已恢复原设置")).toBeTruthy());
    expect(apiMock.mock.calls.some((c) => c[0] === "/api/v1/config-proposals/ccd_p1/undo")).toBe(true);
  });

  it("apply 失败 → fail 态 + 重试可恢复", async () => {
    mockHydrate();
    applyLiteProposal(PROPOSAL);
    renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    apiMock.mockRejectedValueOnce(new ApiError(503, "apply_failed"));
    fireEvent.click(screen.getByText("应用"));
    await waitFor(() => expect(screen.queryByText("没能应用")).toBeTruthy());
    expect(screen.getByText("设置没有改动。")).toBeTruthy();
    // Ignore on the left, Retry in the primary slot on the right.
    const footer = screen.getByTestId("lite-fail").querySelectorAll("button");
    expect([...footer].map((button) => button.textContent)).toEqual(["忽略", "重试"]);

    apiMock.mockResolvedValueOnce(dto("applied"));
    fireEvent.click(screen.getByText("重试"));
    await waitFor(() => expect(screen.queryByTestId("lite-applied")).toBeTruthy());
  });

  it("409 state_conflict → 按服务端现态自愈渲染（跨设备另一端已 undo）", async () => {
    mockHydrate();
    applyLiteProposal(PROPOSAL);
    renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    apiMock.mockRejectedValueOnce(
      new ApiError(409, "conflict", {
        body: {
          detail: {
            error: {
              code: "proposal_state_conflict",
              details: { status: "reverted" },
            },
          },
        },
      }),
    );
    fireEvent.click(screen.getByText("应用"));
    await waitFor(() => expect(screen.queryByText("已恢复原设置")).toBeTruthy());
  });

  it("忽略 → POST /ignore → ignored 塌缩", async () => {
    mockHydrate();
    applyLiteProposal(PROPOSAL);
    renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    apiMock.mockResolvedValueOnce(dto("ignored"));
    fireEvent.click(screen.getByText("忽略"));
    await waitFor(() => expect(screen.queryByText("已忽略")).toBeTruthy());
    expect(apiMock.mock.calls.some((c) => c[0] === "/api/v1/config-proposals/ccd_p1/ignore")).toBe(true);
  });

  it("重挂载（模拟 F5）重新 hydrate 恢复服务端终态", async () => {
    mockHydrate("applied");
    applyLiteProposal(PROPOSAL);
    const first = renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    await waitFor(() => expect(screen.queryByTestId("lite-applied")).toBeTruthy());
    first.unmount();

    _dropLiteStateCacheForTests();
    mockHydrate("applied");
    renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    await waitFor(() => expect(screen.queryByTestId("lite-applied")).toBeTruthy());
  });

  it("hydrate 结构化 404 → stale 不可交互（历史孤儿卡不渲染成注定失败的 propose 卡）", async () => {
    applyLiteProposal(PROPOSAL);
    apiMock.mockRejectedValueOnce(
      new ApiError(404, "not found", {
        body: { detail: { error: { code: "config_proposal_not_found" } } },
      }),
    );
    renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    await waitFor(() => expect(screen.queryByTestId("lite-stale")).toBeTruthy());
    expect(screen.queryByText("应用")).toBeNull();
    expect(screen.getByText("这条建议已失效")).toBeTruthy();
  });

  it("names the target Space in each row (review P2-9)", () => {
    mockHydrate("proposed", "ccd_named");
    applyLiteProposal({
      proposal_id: "ccd_named",
      turn_id: "t_1",
      form_id: "workspace_i18n",
      workspace_id: "ws_1",
      workspace_name: "商超零售",
      rows: [{ field: "week_start_day", from: "monday", to: "sunday", from_unset: true }],
      notes: {},
    });
    renderIntl(<LiteConfirmConnected proposalId="ccd_named" />);
    expect(screen.getByText("周起始日（商超零售）")).toBeTruthy();
    expect(screen.getByText("周一")).toBeTruthy();
    expect(screen.getByText("周日")).toBeTruthy();
  });

  it("never applies a suggestion that has a field without a display name", () => {
    mockHydrate("proposed", "ccd_ws");
    applyLiteProposal({
      proposal_id: "ccd_ws",
      turn_id: "t_1",
      form_id: "workspace_i18n",
      workspace_id: "ws_1",
      workspace_name: "商超零售",
      rows: [
        {
          field: "week_start_day",
          from: "monday",
          to: "sunday",
          from_unset: true,
        },
        { field: "future_field", from: "a", to: "b", from_unset: false },
      ],
      notes: {},
    });
    renderIntl(<LiteConfirmConnected proposalId="ccd_ws" />);
    // The unknown field can be neither shown nor silently applied: the card reads as no longer available.
    expect(screen.getByTestId("lite-stale")).toBeTruthy();
    expect(screen.queryByText(/future_field/)).toBeNull();
    expect(screen.queryByRole("button", { name: "应用" })).toBeNull();
  });

  it("hides Undo for applied name changes and explains that they can be edited back (D11)", async () => {
    mockHydrate("applied", "ccd_meta");
    applyLiteProposal({
      proposal_id: "ccd_meta",
      turn_id: "t_1",
      form_id: "workspace_metadata",
      workspace_id: null,
      workspace_name: null,
      rows: [{ field: "name", from: "华东零售", to: "华东零售分析空间", from_unset: false }],
      notes: {},
    });
    renderIntl(<LiteConfirmConnected proposalId="ccd_meta" />);
    const card = await screen.findByTestId("lite-applied");
    expect(card.textContent).toContain("Space 名称改为华东零售分析空间。名称和描述的修改不能撤销，可以直接改回。");
    expect(screen.queryByRole("button", { name: "撤销" })).toBeNull();
  });

  it("shows a suggested Space instructions text as a line diff and confirms it into a new version without Undo", async () => {
    mockHydrate("proposed", "ccd_si");
    applyLiteProposal({
      proposal_id: "ccd_si",
      turn_id: "t_1",
      form_id: "space_instructions",
      workspace_id: "ws_1",
      workspace_name: "医药",
      rows: [{ field: "content", from: "面向品牌经理回答。", to: "面向品牌经理回答。\n\n新品指上市未满 24 个月的产品。", from_unset: false }],
      notes: {},
    });
    renderIntl(<LiteConfirmConnected proposalId="ccd_si" />);
    const card = await screen.findByTestId("lite-propose");
    expect(card.textContent).toContain("更新「医药」的空间说明？");
    expect(card.textContent).toContain("新品指上市未满 24 个月的产品。");
    expect(card.querySelectorAll('[data-kind="add"]')).toHaveLength(1);
    expect(card.querySelector('[data-kind="del"]')).toBeNull();
    const saved = vi.fn();
    window.addEventListener(WORKSPACE_INSTRUCTIONS_SAVED_EVENT, saved);
    apiMock.mockResolvedValueOnce({ ...dto("applied", "ccd_si"), form_id: "space_instructions", workspace_id: "ws_1", team_id: "team_1" });
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    const applied = await screen.findByTestId("lite-applied");
    window.removeEventListener(WORKSPACE_INSTRUCTIONS_SAVED_EVENT, saved);
    expect(applied.textContent).toContain("「医药」的空间说明已更新为新版本。要撤回，可以在空间说明的版本历史里恢复上一版。");
    expect(applied.textContent).not.toContain("24 个月");
    expect(screen.queryByRole("button", { name: "撤销" })).toBeNull();
    // An open instructions page learns that a new version exists.
    expect(saved).toHaveBeenCalledTimes(1);
    expect((saved.mock.calls[0][0] as CustomEvent).detail).toMatchObject({ teamId: "team_1", workspaceId: "ws_1" });
  });

  it("explains a refused Space instructions confirmation instead of offering a retry", async () => {
    mockHydrate("proposed", "ccd_si403");
    applyLiteProposal({
      proposal_id: "ccd_si403",
      turn_id: "t_1",
      form_id: "space_instructions",
      workspace_id: "ws_1",
      workspace_name: "医药",
      rows: [{ field: "content", from: "", to: "新品指上市未满 24 个月的产品。", from_unset: false }],
      notes: {},
    });
    renderIntl(<LiteConfirmConnected proposalId="ccd_si403" />);
    await screen.findByTestId("lite-propose");
    apiMock.mockRejectedValueOnce(new ApiError(403, "forbidden"));
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    const failed = await screen.findByTestId("lite-fail");
    expect(failed.textContent).toContain("只有空间管理员和团队管理员可以修改空间说明");
    expect([...failed.querySelectorAll("button")].map((button) => button.textContent)).toEqual(["忽略"]);
  });

  it("proposal 缺失（store 未 hydrate）→ 零渲染不崩", () => {
    apiMock.mockRejectedValueOnce(new TypeError("net"));
    const { container } = renderIntl(<LiteConfirmConnected proposalId="nope" />);
    expect(container.firstChild).toBeNull();
  });
});


describe("Typed configuration review", () => {
  it("preserves zero, explicit null, reset intent and nested false values", async () => {
    mockHydrate();
    applyLiteProposal({ ...PROPOSAL, form_id: "team_run_limits", rows: [
      { field: "step_limit", from: 10, to: null, from_unset: false },
      { field: "max_retries", from: 3, to: 0, from_unset: false },
      { field: "tool_call_limit", from: 500, to: 300, operation: "reset", from_unset: false },
      { field: "providers", from: {}, to: { main: { enabled: false, nested: [0, null] } }, from_unset: false },
    ] });
    const view = renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(screen.getByText("不限制")).toBeTruthy();
    expect(screen.getByText("0")).toBeTruthy();
    expect(view.container.textContent).toContain("300");
    expect(view.container.textContent).toContain('"enabled": false');
    expect(view.container.textContent).toContain('null');
    expect(view.container.textContent).not.toContain("[object Object]");
  });

  it("opens original model review and refreshes the confirmed receipt", async () => {
    mockHydrate();
    applyLiteProposal({ ...PROPOSAL, form_id: "team_model_config", rows: [{ field: "tiers", from: {}, to: { fast: { auto: true } }, from_unset: false }] });
    renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    fireEvent.click(screen.getByText("在模型设置中审阅"));
    expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/?configuration_proposal=ccd_p1");
    expect(apiMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(false);
    apiMock.mockResolvedValueOnce({ ...dto("applied"), form_id: "team_model_config" });
    await refreshLiteState("ccd_p1");
    await screen.findByTestId("lite-applied");
    expect(screen.queryByText("撤销")).toBeNull();
  });
});

it("explains closed Team admission without offering an ineffective retry", async () => {
  mockHydrate();
  applyLiteProposal({ ...PROPOSAL, form_id: "team_model_retry", rows: [{ field: "max_retries", from: 3, to: 0, from_unset: false }] });
  renderIntl(<LiteConfirmConnected proposalId="ccd_p1" />);
  await waitFor(() => expect(apiMock).toHaveBeenCalled());
  apiMock.mockRejectedValueOnce(new ApiError(409, "unavailable", { body: { detail: { error: { code: "form_unavailable" } } } }));
  fireEvent.click(screen.getByText("应用"));
  expect((await screen.findByTestId("lite-fail")).textContent).toContain("请打开原设置页");
  expect(screen.queryByText("重试")).toBeNull();
});
