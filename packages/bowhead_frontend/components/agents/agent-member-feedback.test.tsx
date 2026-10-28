/**
 * Members' shared thumbs-downs on the Owner's Pending list: who, when, the question preview, reason
 * chips and the comment (or "no comment"); "Use this to improve the Agent" marks the item handled and
 * opens Coach with the Turn staged; dismissing goes through the caller's Undo flow.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "@/messages/zh-CN.json";

vi.mock("@/lib/agents-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agents-api")>()),
  handleAgentMemberFeedback: vi.fn(),
}));
vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));

import { handleAgentMemberFeedback, type AgentMemberFeedback } from "@/lib/agents-api";
import { toast } from "@/lib/use-toast";

import { MemberFeedbackView, memberFeedbackKey } from "./agent-member-feedback";

const ITEM: AgentMemberFeedback = {
  turn_id: "t_1",
  shared_by: "Lin Wei",
  question: "上个季度华东的销售额是多少？",
  answered_at: "2026-10-19T08:00:00+00:00",
  feedback: { score: -1, reason_codes: ["fact_wrong", "too_brief"], comment: null, given_at: "2026-10-19T09:00:00+00:00" },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function mount(props: Partial<Parameters<typeof MemberFeedbackView>[0]> = {}) {
  const onDismiss = vi.fn();
  const onImprove = vi.fn(async (_text: string, markHandled: () => Promise<void>) => { await markHandled(); });
  const reload = vi.fn(async () => {});
  render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <MemberFeedbackView agentId="osa_1" agentName="销售分析" items={[ITEM]} hidden={new Set()} onDismiss={onDismiss} onImprove={onImprove} reload={reload} {...props} />
    </NextIntlClientProvider>,
  );
  return { onDismiss, onImprove, reload };
}

describe("MemberFeedbackView", () => {
  it("shows one row: a neutral type label, the question, who, when, the reasons and 'no comment'", () => {
    mount();
    expect(screen.getByRole("heading", { name: "1 条点踩待处理" })).toBeTruthy();
    const row = screen.getByTestId("member-feedback-item");
    expect(row.textContent).toContain("点踩上个季度华东的销售额是多少？");
    expect(row.textContent).toMatch(/Lin Wei · .+ · 事实或数字不对 · 太简略 · 没有补充说明/);
    expect(screen.getByRole("button", { name: "用它来改进智能体" })).toBeTruthy();
  });

  it("marks the item handled and opens Coach with the Turn staged", async () => {
    vi.mocked(handleAgentMemberFeedback).mockResolvedValueOnce({ kind: "agent_member_feedback_handled", schema_version: 1, turn_id: "t_1", handled: "coach" });
    const { onImprove } = mount();
    fireEvent.click(screen.getByRole("button", { name: "用它来改进智能体" }));
    await waitFor(() => expect(handleAgentMemberFeedback).toHaveBeenCalledWith("osa_1", "t_1", "coach"));
    const text = onImprove.mock.calls[0][0];
    expect(text).toContain("销售分析");
    expect(text).toContain("上个季度华东的销售额是多少？");
    expect(text).toContain("事实或数字不对 · 太简略");
    expect(text).toContain("没有补充说明");
    expect(text).toContain("t_1");
  });

  it("says why when the item can't be handled and refreshes the list", async () => {
    vi.mocked(handleAgentMemberFeedback).mockRejectedValueOnce(new Error("404"));
    const { reload } = mount();
    fireEvent.click(screen.getByRole("button", { name: "用它来改进智能体" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith({ kind: "error", text: expect.stringContaining("成员可能已取消分享") }));
    expect(reload).toHaveBeenCalled();
  });

  it("dismisses through the caller and hides items waiting on their Undo toast", () => {
    const { onDismiss } = mount();
    fireEvent.click(screen.getByRole("button", { name: "忽略" }));
    expect(onDismiss).toHaveBeenCalledWith(ITEM);
    cleanup();
    mount({ hidden: new Set([memberFeedbackKey("osa_1", "t_1")]) });
    expect(screen.queryByTestId("member-feedback-item")).toBeNull();
  });
});
