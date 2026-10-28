/**
 * Sharing a Turn with the answering Agent's Owner as an evaluation-case source (2026-10-19 decision).
 *
 * No always-visible icon: the "⋯" menu appears only on answers the server lists as shareable (a team
 * Agent the member doesn't own), and its single entry shares or withdraws.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "@/messages/zh-CN.json";

vi.mock("@/lib/agents-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agents-api")>()),
  listTurnEvalShares: vi.fn(),
  setTurnEvalShare: vi.fn(),
  setTurnAnswerFeedback: vi.fn(),
}));
vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));

import { listTurnEvalShares, setTurnEvalShare } from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { toast } from "@/lib/use-toast";
import { _resetTurnEvalShares } from "@/lib/use-turn-eval-share";

import { MessageActions } from "./message-actions";

const SHAREABLE = { turn_id: "t_1", agent_id: "osa_1", agent_name: "销售分析", shared: false };

beforeEach(() => {
  _resetTurnEvalShares();
  vi.mocked(listTurnEvalShares).mockResolvedValue({ kind: "turn_eval_shares", schema_version: 1, items: [SHAREABLE] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function mount(turnId: string, sessionId = "s_1") {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <MessageActions answerText="答案" sessionId={sessionId} turnId={turnId} />
    </NextIntlClientProvider>,
  );
}

async function openMenu() {
  fireEvent.click(await screen.findByRole("button", { name: "更多操作" }));
}

describe("MessageActions — share with the owner as an eval case", () => {
  it("shows no menu on an answer that can't be shared, and one request serves every answer", async () => {
    render(
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <MessageActions answerText="答案" sessionId="s_1" turnId="t_2" />
        <MessageActions answerText="答案" sessionId="s_1" turnId="t_3" />
      </NextIntlClientProvider>,
    );
    await waitFor(() => expect(listTurnEvalShares).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "更多操作" })).toBeNull();
    expect(screen.getAllByTestId("msg-action-copy")).toHaveLength(2);
  });

  it("shares, then offers withdrawing, with a receipt that says what happens to drafted cases", async () => {
    vi.mocked(setTurnEvalShare).mockResolvedValueOnce({ kind: "turn_eval_share", schema_version: 1, turn_id: "t_1", shared: true });
    mount("t_1");
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "分享给负责人做评测用例" }));
    await waitFor(() => expect(setTurnEvalShare).toHaveBeenCalledWith("t_1", true));
    await waitFor(() => expect(toast).toHaveBeenCalledWith({ kind: "success", text: expect.stringContaining("销售分析 的负责人") }));

    vi.mocked(setTurnEvalShare).mockResolvedValueOnce({ kind: "turn_eval_share", schema_version: 1, turn_id: "t_1", shared: false });
    await openMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "取消分享给负责人" }));
    await waitFor(() => expect(setTurnEvalShare).toHaveBeenLastCalledWith("t_1", false));
    await waitFor(() => expect(toast).toHaveBeenLastCalledWith({ kind: "success", text: expect.stringContaining("已经用它起草的用例会留在") }));
  });

  it("explains a Turn that can no longer be shared and removes the entry", async () => {
    vi.mocked(setTurnEvalShare).mockRejectedValueOnce(new ApiError(404, "turn_eval_share_unavailable", { code: "turn_eval_share_unavailable" }));
    mount("t_1");
    await openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "分享给负责人做评测用例" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith({ kind: "error", text: expect.stringContaining("对话已删除") }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "更多操作" })).toBeNull());
  });
});
