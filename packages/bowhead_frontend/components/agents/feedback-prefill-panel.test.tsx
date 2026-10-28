// Prefill review panel contract: exact banner and diff text; cannot apply while unchecked; discarding leaves the draft unchanged.

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { FeedbackPrefillHandoff } from "@/lib/feedback-candidates-api";

import { FeedbackPrefillPanel } from "./feedback-prefill-panel";

const HANDOFF: FeedbackPrefillHandoff = {
  candidate_id: "fbc_5c21",
  target_kind: "agent_def",
  target_ref: "ag_example",
  prefill: { kind: "agent_system_prompt", edited_content: "你是数据分析师。\n报告先给结论。", rationale: "落点在 prompt" },
  base: { definition_sha256: "abc" },
  banner: { candidate_id: "fbc_5c21", proposer_id: "u_wang", evidence_count: 3 },
};

afterEach(cleanup);

describe("FeedbackPrefillPanel", () => {
  it("shows the verbatim banner and a line diff, and gates apply on explicit review", () => {
    const onApply = vi.fn();
    const onDiscard = vi.fn();
    renderIntl(<FeedbackPrefillPanel handoff={HANDOFF} baseline={"你是数据分析师。\n报告以摘要形式给出。"} onApply={onApply} onDiscard={onDiscard} />);
    expect(screen.getByText("此内容由成员反馈起草，来源不可信，保存前逐行审阅")).toBeTruthy();
    expect(screen.getByText(/u_wang · 3 人联署 · 来自建议 fbc_5c21/)).toBeTruthy();
    const diff = screen.getByTestId("feedback-prefill-diff");
    expect(diff.querySelector('[data-kind="del"]')?.textContent).toContain("报告以摘要形式给出。");
    expect(diff.querySelector('[data-kind="add"]')?.textContent).toContain("报告先给结论。");
    const apply = screen.getByRole("button", { name: "应用到编辑器" }) as HTMLButtonElement;
    expect(apply.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: "我已逐行审阅预填内容" }));
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);
    expect(onApply).toHaveBeenCalledWith("你是数据分析师。\n报告先给结论。");
    fireEvent.click(screen.getByRole("button", { name: "放弃预填，保留当前版本" }));
    expect(onDiscard).toHaveBeenCalled();
  });
});
