import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { projectCoachOperationOpen, readCoachOperation } from "@/lib/coach-operations";
import { requestCoachOperationOpen } from "@/lib/coach-operation-requests";
import { CoachChangeReceipts } from "./coach-change-receipt";

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

describe("CoachChangeReceipts", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

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
