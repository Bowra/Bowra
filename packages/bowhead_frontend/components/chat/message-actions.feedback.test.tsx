/**
 * Thumbs-down on a team Agent's answer (2026-10-19 decision, Claude Design L0-a…L0-c): only team-Agent
 * answers get it; the dialog's consent checkbox starts unchecked, appears only when someone else owns
 * the Agent, and shares through the same route as the "⋯" menu; the receipt toast offers Undo, and
 * pressing the button again removes the thumbs-down together with the share.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "@/messages/zh-CN.json";

vi.mock("@/lib/agents-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agents-api")>()),
  listTurnEvalShares: vi.fn(),
  setTurnEvalShare: vi.fn(),
  setTurnAnswerFeedback: vi.fn(),
}));
vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));

import { listTurnEvalShares, setTurnAnswerFeedback, setTurnEvalShare } from "@/lib/agents-api";
import { toast } from "@/lib/use-toast";
import { _resetTurnEvalShares } from "@/lib/use-turn-eval-share";

import { MessageActions } from "./message-actions";

const SOMEONE_ELSES = { turn_id: "t_1", agent_id: "osa_1", agent_name: "销售分析", shared: false, shareable: true, owner_name: "刘一鑫", feedback: null };
const MY_AGENT = { turn_id: "t_2", agent_id: "osa_2", agent_name: "我的助手", shared: false, shareable: false, owner_name: "我", feedback: null };
const FEEDBACK = { score: -1 as const, reason_codes: ["fact_wrong"], comment: "季度错了", given_at: "2026-10-19T08:00:00+00:00" };

beforeEach(() => {
  _resetTurnEvalShares();
  vi.mocked(listTurnEvalShares).mockResolvedValue({ kind: "turn_eval_shares", schema_version: 1, items: [SOMEONE_ELSES, MY_AGENT] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function mount(turnId: string) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <MessageActions answerText="答案" sessionId="s_1" turnId={turnId} />
    </NextIntlClientProvider>,
  );
}

const thumbs = () => screen.findByRole("button", { name: "这条回答不好" });
const lastToast = () => vi.mocked(toast).mock.calls.at(-1)![0] as { kind: string; text: string; undo?: boolean; action?: { label: string; onAction: () => void } };

describe("MessageActions — thumbs-down", () => {
  it("is offered only on team-Agent answers", async () => {
    mount("t_lead");
    await waitFor(() => expect(listTurnEvalShares).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(screen.queryByTestId("msg-action-thumbs-down")).toBeNull();
  });

  it("records reasons and a comment without sharing, then shows the pressed state; Undo takes it back", async () => {
    vi.mocked(setTurnAnswerFeedback).mockResolvedValueOnce({ kind: "turn_answer_feedback", schema_version: 1, turn_id: "t_1", feedback: FEEDBACK });
    mount("t_1");
    fireEvent.click(await thumbs());
    const dialog = await screen.findByRole("dialog", { name: "这条回答哪里不好？" });
    expect(within(dialog).getByText("都可以不填，直接提交也会记下。")).toBeTruthy();
    const consent = within(dialog).getByRole("checkbox", { name: /允许智能体负责人用这一轮做评测用例（负责人会看到问题、回答和你的反馈）/ }) as HTMLInputElement;
    expect(consent.checked).toBe(false);
    expect(within(dialog).getByText("负责人：刘一鑫。之后可以在这条回答的「⋯」里撤回。")).toBeTruthy();
    // Reasons are checkboxes and go out in display order.
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "太简略" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "事实或数字不对" }));
    fireEvent.change(within(dialog).getByRole("textbox", { name: /补充说明/ }), { target: { value: " 季度错了 " } });
    fireEvent.click(within(dialog).getByRole("button", { name: "提交" }));

    await waitFor(() => expect(setTurnAnswerFeedback).toHaveBeenCalledWith("t_1", { reason_codes: ["fact_wrong", "too_brief"], comment: "季度错了" }));
    expect(setTurnEvalShare).not.toHaveBeenCalled();
    await waitFor(() => expect(lastToast()).toMatchObject({ kind: "info", undo: true, text: "已记下。", action: { label: "撤销" } }));
    expect((await screen.findByRole("button", { name: "已标记为不好，再点一次撤销" })).getAttribute("aria-pressed")).toBe("true");

    vi.mocked(setTurnAnswerFeedback).mockResolvedValueOnce({ kind: "turn_answer_feedback", schema_version: 1, turn_id: "t_1", feedback: null });
    await act(async () => lastToast().action!.onAction());
    await waitFor(() => expect(setTurnAnswerFeedback).toHaveBeenLastCalledWith("t_1", null));
    expect(setTurnEvalShare).not.toHaveBeenCalled();
    expect((await thumbs()).getAttribute("aria-pressed")).toBe("false");
  });

  it("shares through the menu's route when ticked; pressing the button again removes both", async () => {
    vi.mocked(setTurnAnswerFeedback).mockResolvedValueOnce({ kind: "turn_answer_feedback", schema_version: 1, turn_id: "t_1", feedback: { ...FEEDBACK, reason_codes: [], comment: null } });
    vi.mocked(setTurnEvalShare).mockResolvedValueOnce({ kind: "turn_eval_share", schema_version: 1, turn_id: "t_1", shared: true });
    mount("t_1");
    fireEvent.click(await thumbs());
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /允许智能体负责人/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "提交" }));

    await waitFor(() => expect(setTurnEvalShare).toHaveBeenCalledWith("t_1", true));
    expect(setTurnAnswerFeedback).toHaveBeenCalledWith("t_1", { reason_codes: [], comment: null });
    await waitFor(() => expect(lastToast()).toMatchObject({ text: "已记下。负责人可以用这一轮改进智能体。", undo: true }));
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(await screen.findByRole("menuitem", { name: "取消分享给负责人" })).toBeTruthy();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    vi.mocked(setTurnAnswerFeedback).mockResolvedValueOnce({ kind: "turn_answer_feedback", schema_version: 1, turn_id: "t_1", feedback: null });
    vi.mocked(setTurnEvalShare).mockResolvedValueOnce({ kind: "turn_eval_share", schema_version: 1, turn_id: "t_1", shared: false });
    fireEvent.click(await screen.findByRole("button", { name: "已标记为不好，再点一次撤销" }));
    await waitFor(() => expect(setTurnEvalShare).toHaveBeenLastCalledWith("t_1", false));
    expect(setTurnAnswerFeedback).toHaveBeenLastCalledWith("t_1", null);
    await waitFor(() => expect(lastToast()).toMatchObject({ text: "已撤销点踩，并撤回了给负责人的分享。" }));
  });

  it("asks no consent and shows no share menu on the member's own Agent", async () => {
    mount("t_2");
    fireEvent.click(await thumbs());
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("checkbox", { name: /允许智能体负责人/ })).toBeNull();
    expect(within(dialog).getAllByRole("checkbox")).toHaveLength(7);
    expect(screen.queryByRole("button", { name: "更多操作" })).toBeNull();
  });
});
