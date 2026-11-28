import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import { projectCoachOperationOpen, readCoachOperation } from "@/lib/coach-operations";
import { requestCoachOperationOpen } from "@/lib/coach-operation-requests";
import { CoachChangeReceipts } from "./coach-change-receipt";
import { CoachReceiptScopeContext } from "@/lib/coach-receipt-scope";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { setAgentReleaseBarCapabilityForTest } from "@/lib/use-agent-release-bar-capability";
import { CoachOpenAgentEditorsContext } from "@/lib/use-coach-resource-editors";

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

describe("Coach draft receipts read from the run record (release bar switch on)", () => {
  const SCOPE = { userId: "u_1", teamId: "team_1", workspaceId: "ws_1", sessionId: "s_1" };
  const reference = (proposalId: string) => ({ kind: "agent_change" as const, agent_id: "osa_1", proposal_id: proposalId });
  const recordWrites = (turnId: string, proposalIds: string[]) => dispatchToTurnStore("tool_result", {
    result_meta: { coach_operations: { schema_version: 1, truncated: false, receipts: proposalIds.map((proposalId, index) => ({
      call_id: `call_${index}`, tool_name: "agent_change_propose", operation_ref: reference(proposalId), reported_ok: true,
    })) } },
  }, "live", turnId);
  const written = (proposalId: string, paths: string[]) => ({
    operation_ref: reference(proposalId),
    domain_result: { effect: "draft_updated", released: false, changed_paths: paths, proposal: { base_document: { listing: { display_name: "门店周报" } } } },
  });
  const render = (turnId: string, open: ReadonlySet<string> = new Set()) => renderIntl(
    <CoachReceiptScopeContext.Provider value={SCOPE}>
      <CoachOpenAgentEditorsContext.Provider value={open}>
        <CoachChangeReceipts turnId={turnId} sessionId="s_1" current />
      </CoachOpenAgentEditorsContext.Provider>
    </CoachReceiptScopeContext.Provider>,
  );

  afterEach(() => setAgentReleaseBarCapabilityForTest(null));

  it("names the Agent and every field Coach wrote in the Turn, without an open event or 还没发布", async () => {
    setAgentReleaseBarCapabilityForTest(true);
    recordWrites("t_record", ["acp_r1", "acp_r2"]);
    vi.mocked(readCoachOperation).mockImplementation(async (ref) => ref.kind === "agent_change" && ref.proposal_id === "acp_r1"
      ? written("acp_r1", ["/definition/system_prompt"])
      : written("acp_r2", ["/definition/system_prompt", "/definition/tool_refs"]));
    render("t_record");

    expect(await screen.findByText("已写进草稿 · 门店周报 · 工作说明、工具 2 项")).toBeVisible();
    expect(screen.getByRole("group", { name: "Coach 写进草稿的修改" })).toBeVisible();
    expect(screen.queryByText(/还没发布/)).toBeNull();
    expect(readCoachOperation).toHaveBeenCalledWith(reference("acp_r1"), "ws_1", "s_1", expect.any(AbortSignal), "team_1");
    fireEvent.click(screen.getByRole("button", { name: "在画布查看" }));
    expect(requestCoachOperationOpen).toHaveBeenCalledWith({ turnId: "t_record", reference: reference("acp_r2") });
    const manage = screen.getByRole("link", { name: "去管理页测试和发布" });
    expect(manage.getAttribute("href")).toContain("/agents/manage/?agent_id=osa_1");
    fireEvent.click(manage);
    expect(navigation.push).toHaveBeenCalledWith("/agents/manage/?agent_id=osa_1");
  });

  it("only says the editor is open on the right while it is", async () => {
    setAgentReleaseBarCapabilityForTest(true);
    recordWrites("t_open", ["acp_o1"]);
    vi.mocked(readCoachOperation).mockResolvedValue(written("acp_o1", ["/definition/system_prompt"]));
    render("t_open", new Set(["osa_1"]));

    expect(await screen.findByText("已写进草稿 · 门店周报 · 工作说明 1 项 · 已在右侧打开")).toBeVisible();
    expect(screen.queryByRole("button", { name: "在画布查看" })).toBeNull();
    expect(screen.queryByRole("link", { name: "去管理页测试和发布" })).toBeNull();
  });

  it("keeps only the first words when the read answers coach_operation_unavailable", async () => {
    setAgentReleaseBarCapabilityForTest(true);
    recordWrites("t_gone", ["acp_g1"]);
    vi.mocked(readCoachOperation).mockRejectedValue(new ApiError(404, "unavailable", { code: "coach_operation_unavailable" }));
    render("t_gone");

    await vi.waitFor(() => expect(readCoachOperation).toHaveBeenCalled());
    await vi.waitFor(() => expect(screen.queryByRole("button", { name: "在画布查看" })).toBeNull());
    expect(screen.getByText("修改智能体草稿")).toBeVisible();
    expect(screen.queryByRole("link", { name: "去管理页测试和发布" })).toBeNull();
  });

  it("shows one row when an open event repeats a recorded write, and keeps an older Turn's open-only row", async () => {
    setAgentReleaseBarCapabilityForTest(true);
    recordWrites("t_both", ["acp_1"]);
    open("t_both", "c".repeat(32));
    open("t_old_open", "d".repeat(32));
    vi.mocked(readCoachOperation).mockResolvedValue(written("acp_1", ["/definition/system_prompt"]));
    render("t_both");
    expect(await screen.findByText("已写进草稿 · 门店周报 · 工作说明 1 项")).toBeVisible();
    expect(screen.queryByText("已改到草稿 · 1 处")).toBeNull();
    cleanup();
    render("t_old_open");
    expect(await screen.findByText("已改到草稿 · 1 处")).toBeVisible();
  });

  it("renders no recorded row with the switch off", async () => {
    setAgentReleaseBarCapabilityForTest(false);
    recordWrites("t_off", ["acp_f1"]);
    render("t_off");
    expect(screen.queryByRole("group", { name: "Coach 写进草稿的修改" })).toBeNull();
    expect(readCoachOperation).not.toHaveBeenCalled();
  });
});
