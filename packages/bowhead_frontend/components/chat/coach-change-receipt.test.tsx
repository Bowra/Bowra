import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { projectCoachOperationOpen, readCoachOperation } from "@/lib/coach-operations";
import { requestCoachOperationOpen } from "@/lib/coach-operation-requests";
import { CoachChangeReceipts } from "./coach-change-receipt";
import { CoachReceiptScopeContext } from "@/lib/coach-receipt-scope";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => navigation }));

vi.mock("@/lib/coach-operations", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/coach-operations")>()),
  readCoachOperation: vi.fn(),
}));
vi.mock("@/lib/coach-operation-requests", () => ({ requestCoachOperationOpen: vi.fn() }));

function open(turnId: string, requestId: string) {
  projectCoachOperationOpen({
    reference: { kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_1" },
    request_id: requestId, turn_id: turnId, user_id: "u_1", team_id: "team_1", workspace_id: "ws_1", session_id: "s_1",
  });
}

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("CoachChangeReceipts", () => {

  it("states a written draft change from the Turn's own receipt and opens the Agent in the canvas", async () => {
    vi.mocked(readCoachOperation).mockResolvedValue({
      operation_ref: { kind: "agent_change", agent_id: "osa_1", proposal_id: "acp_1" },
      domain_result: {
        effect: "draft_updated",
        changed_paths: ["/definition/system_prompt", "/definition/tool_refs"],
        proposal: { base_document: { listing: { display_name: "业务增长分析师" } } },
      },
    });
    open("t_written", "a".repeat(32));
    renderIntl(<CoachChangeReceipts turnId="t_written" sessionId="s_1" />);
    expect(await screen.findByText("已改到草稿 · 2 处")).toBeTruthy();
    expect(screen.getByText("业务增长分析师 · 还没发布，可以逐处撤销")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "在画布查看" }));
    expect(requestCoachOperationOpen).toHaveBeenCalledWith({ turnId: "t_written", requestId: "a".repeat(32) });
  });

  it("shows nothing for another session's Turn and a neutral row when the receipt can't be read", async () => {
    vi.mocked(readCoachOperation).mockRejectedValue(new Error("unavailable"));
    open("t_unread", "b".repeat(32));
    const view = renderIntl(<CoachChangeReceipts turnId="t_unread" sessionId="s_other" />);
    expect(view.container.textContent).toBe("");
    cleanup();
    renderIntl(<CoachChangeReceipts turnId="t_unread" sessionId="s_1" />);
    expect(await screen.findByText("修改智能体草稿")).toBeTruthy();
  });
});

it("shows an original saved-case entry without an open event and rechecks access before review", async () => {
  const reference = { kind: "eval_cases" as const, agent_id: "osa_1", proposal_id: "acp_case" };
  dispatchToTurnStore("tool_result", { result_meta: { coach_operations: { schema_version: 1, truncated: false, receipts: [{ call_id: "call_case", tool_name: "agent_eval_case_propose", operation_ref: reference, reported_ok: true }] } } }, "live", "t_case_chat");
  vi.mocked(readCoachOperation).mockResolvedValue({ operation_ref: reference, domain_result: { proposal: { status: "open" } } });
  renderIntl(<CoachReceiptScopeContext.Provider value={{ userId: "u_1", teamId: "team_1", workspaceId: "ws_1", sessionId: "s_1" }}>
    <CoachChangeReceipts turnId="t_case_chat" sessionId="s_1" current />
  </CoachReceiptScopeContext.Provider>);
  expect(await screen.findByText("用例建议待审阅")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "审阅建议" }));
  await vi.waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/agents/manage/created/?agent_id=osa_1&section=config#agent-pending-changes"));
  navigation.push.mockClear();
  vi.mocked(readCoachOperation).mockRejectedValue(new Error("Access revoked"));
  fireEvent.click(screen.getByRole("button", { name: "审阅建议" }));
  expect(await screen.findByText("暂时无法读取这条建议")).toBeVisible();
  expect(navigation.push).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "审阅建议" })).not.toBeInTheDocument();
});

it("does not eagerly read historical case receipts and keeps other-session references hidden", async () => {
  const reference = { kind: "eval_cases" as const, agent_id: "osa_1", proposal_id: "acp_old_case" };
  dispatchToTurnStore("tool_result", { result_meta: { coach_operations: { schema_version: 1, truncated: false, receipts: [{ call_id: "old_call", tool_name: "agent_eval_case_propose", operation_ref: reference, reported_ok: false }] } } }, "replay", "t_old_case");
  vi.mocked(readCoachOperation).mockResolvedValue({ operation_ref: reference, domain_result: { proposal: { status: "accepted" } } });
  renderIntl(<CoachReceiptScopeContext.Provider value={{ userId: "u_1", teamId: "team_1", workspaceId: "ws_1", sessionId: "s_1" }}>
    <CoachChangeReceipts turnId="t_old_case" sessionId="s_other" />
    <CoachChangeReceipts turnId="t_old_case" sessionId="s_1" />
  </CoachReceiptScopeContext.Provider>);
  expect(screen.getAllByText("用例建议结果")).toHaveLength(1);
  expect(readCoachOperation).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "查看建议" }));
  await vi.waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/agents/manage/created/?agent_id=osa_1&section=evaluate"));
});
