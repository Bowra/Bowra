/**
 * ClarificationCard (Chat Cards Spec §1, D2): selection only selects, "Continue" (or Enter) submits.
 *
 * · single question: arrows / Home / End select within one Tab stop, nothing is sent until Continue
 * · "Other, type your own" is a field; typing replaces a single-choice selection
 * · several questions: per-question type hint, answered progress, Continue counts answers
 * · tool_context_select: "Remember my choice" defaults to checked and travels with the answer
 * · a failed submission comes back with the same choice and the error inside the card
 * · a document task still saving its pause locks the card and says why
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

import { ClarificationAnsweredSummary, ClarificationCard } from "./ClarificationCard";

const QUESTIONS = [{
  question: "你在北极九章有 2 个可访问的工作区，这次查询用哪个？",
  options: ["零售集团", "华东大区"],
  type: "single_select" as const,
}];

const MIXED_QUESTIONS = [
  { question: "需要覆盖哪些地区？", options: ["华东", "华南"], type: "multi_select" as const },
  { question: "结果按哪个口径展示？", options: ["销售额", "销量"], type: "single_select" as const },
];

const continueButton = () => screen.getByRole("button", { name: /^继续/ });

describe("ClarificationCard · one question (D2)", () => {
  it("is a needs-your-input card named by its status word and question", () => {
    renderIntl(<ClarificationCard questions={QUESTIONS} onSubmitSingle={vi.fn()} onSubmitMulti={vi.fn()} />);
    const card = screen.getByRole("group", { name: `需要你回答 ${QUESTIONS[0].question}` });
    expect(card.className).toContain("ui-card--input");
    expect(within(card).getByText("单选")).toBeTruthy();
  });

  it("selects on click and submits only on Continue", () => {
    const onSingle = vi.fn();
    renderIntl(<ClarificationCard questions={QUESTIONS} onSubmitSingle={onSingle} onSubmitMulti={vi.fn()} />);
    expect((continueButton() as HTMLButtonElement).disabled).toBe(true);
    const radios = screen.getAllByRole("radio");
    fireEvent.click(radios[1]);
    expect(onSingle).not.toHaveBeenCalled();
    expect(radios[1].getAttribute("aria-checked")).toBe("true");
    // A mis-click can be corrected before anything is sent.
    fireEvent.click(radios[0]);
    expect(radios[0].getAttribute("aria-checked")).toBe("true");
    expect(radios[1].getAttribute("aria-checked")).toBe("false");
    fireEvent.click(continueButton());
    expect(onSingle).toHaveBeenCalledWith(
      { question: QUESTIONS[0].question, source: "candidate", answer: "零售集团", option_index: 0 },
      undefined,
    );
  });

  it("moves and selects with arrows / Home / End in one Tab stop, and Enter submits", () => {
    const THREE = [{ question: "用哪个工作区？", options: ["甲", "乙", "丙"], type: "single_select" as const }];
    const onSingle = vi.fn();
    renderIntl(<ClarificationCard questions={THREE} onSubmitSingle={onSingle} onSubmitMulti={vi.fn()} />);
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => r.tabIndex)).toEqual([0, -1, -1]);
    radios[0].focus();
    fireEvent.keyDown(radios[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(radios[1]);
    expect(radios[1].getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(radios[1], { key: "End" });
    expect(radios[2].getAttribute("aria-checked")).toBe("true");
    expect(onSingle).not.toHaveBeenCalled();
    fireEvent.keyDown(radios[2], { key: "Enter" });
    expect(onSingle).toHaveBeenCalledWith(
      { question: THREE[0].question, source: "candidate", answer: "丙", option_index: 2 },
      undefined,
    );
  });

  it("treats typed text as the answer and drops the option selection", () => {
    const onSingle = vi.fn();
    renderIntl(<ClarificationCard questions={QUESTIONS} onSubmitSingle={onSingle} onSubmitMulti={vi.fn()} />);
    fireEvent.click(screen.getAllByRole("radio")[0]);
    const field = screen.getByPlaceholderText("其他，自己输入…");
    fireEvent.change(field, { target: { value: "西南大区" } });
    expect(screen.getAllByRole("radio")[0].getAttribute("aria-checked")).toBe("false");
    fireEvent.keyDown(field, { key: "Enter" });
    expect(onSingle).toHaveBeenCalledWith({ question: QUESTIONS[0].question, source: "custom", answer: "西南大区" }, undefined);
  });

  it("offers skipping to the model as the secondary action", () => {
    const onSkip = vi.fn();
    renderIntl(<ClarificationCard questions={QUESTIONS} onSubmitSingle={vi.fn()} onSubmitMulti={vi.fn()} onSkip={onSkip} />);
    fireEvent.click(screen.getByRole("button", { name: "跳过，让模型判断" }));
    expect(onSkip).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("换个问法")).toBeNull();
  });

  it("accepts a missing numeric value without showing invented options", () => {
    const onMulti = vi.fn();
    const question = "平均所有者权益是多少？";
    renderIntl(
      <ClarificationCard questions={[{ question, options: [], type: "free_text" }]} onSubmitSingle={vi.fn()} onSubmitMulti={onMulti} />,
    );
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByText("自由输入")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("其他，自己输入…"), { target: { value: "50" } });
    fireEvent.click(continueButton());
    expect(onMulti).toHaveBeenCalledWith({ values: ["50"], answers: [{ question, source: "custom", answer: "50" }] });
  });
});

describe("ClarificationCard · several questions", () => {
  it("labels each question's type, counts answers and hints that the rest may stay empty", () => {
    const onMulti = vi.fn();
    renderIntl(<ClarificationCard questions={MIXED_QUESTIONS} onSubmitSingle={vi.fn()} onSubmitMulti={onMulti} />);
    const types = Array.from(document.querySelectorAll(".ui-card-question__type")).map((el) => el.textContent);
    expect(types).toEqual(["可多选", "单选"]);
    expect(screen.getByText("已回答 0 / 2")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(screen.getByText("已回答 1 / 2")).toBeTruthy();
    expect(screen.getByText("问题 2 可以不选，模型会按上下文判断。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "继续（已回答 1 个）" }));
    expect(onMulti).toHaveBeenCalledWith({
      values: ["需要覆盖哪些地区？: 华东"],
      answers: [{ question: "需要覆盖哪些地区？", source: "candidate", answer: "华东", option_index: 0 }],
    });
  });

  it("selects grouped single-choice options with arrows; checkboxes are separate Tab stops", () => {
    const onMulti = vi.fn();
    renderIntl(<ClarificationCard questions={MIXED_QUESTIONS} onSubmitSingle={vi.fn()} onSubmitMulti={onMulti} />);
    expect(screen.getAllByRole("checkbox").map((c) => c.tabIndex)).toEqual([0, 0]);
    const radios = screen.getAllByRole("radio");
    radios[0].focus();
    fireEvent.keyDown(radios[0], { key: "ArrowRight" });
    expect(radios[1].getAttribute("aria-checked")).toBe("true");
    expect(radios[1].className).toContain("is-on");
    fireEvent.click(radios[1]);
    expect(radios[1].getAttribute("aria-checked")).toBe("true");
    fireEvent.click(continueButton());
    expect(onMulti).toHaveBeenCalledWith(
      expect.objectContaining({
        answers: [{ question: "结果按哪个口径展示？", source: "candidate", answer: "销量", option_index: 1 }],
      }),
    );
  });
});

describe("ClarificationCard · remember, errors, unavailable", () => {
  it("defaults Remember my choice to checked and sends it with the answer", () => {
    const onSingle = vi.fn();
    renderIntl(<ClarificationCard questions={QUESTIONS} showRemember onSubmitSingle={onSingle} onSubmitMulti={vi.fn()} />);
    const box = screen.getByRole("checkbox", { name: "记住我的选择（下次直接使用）" }) as HTMLInputElement;
    expect(box.checked).toBe(true);
    fireEvent.click(screen.getAllByRole("radio")[1]);
    fireEvent.click(continueButton());
    expect(onSingle).toHaveBeenCalledWith(
      { question: QUESTIONS[0].question, source: "candidate", answer: "华东大区", option_index: 1 },
      { remember: true },
    );
    fireEvent.click(box);
    fireEvent.click(continueButton());
    expect(onSingle).toHaveBeenLastCalledWith(expect.anything(), { remember: false });
  });

  it("has no remember checkbox on generic cards and shows the foot hint", () => {
    renderIntl(
      <ClarificationCard questions={QUESTIONS} footHint="之后随时说「换 XX 查」即可切换" onSubmitSingle={vi.fn()} onSubmitMulti={vi.fn()} />,
    );
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByText("之后随时说「换 XX 查」即可切换")).toBeTruthy();
  });

  it("comes back after a failed submission with the same choice, the error and Retry", () => {
    const onSingle = vi.fn();
    renderIntl(
      <ClarificationCard
        questions={QUESTIONS}
        restoredAnswers={[{ question: QUESTIONS[0].question, source: "candidate", answer: "华东大区", option_index: 1 }]}
        error="没能提交回答，你的选择已保留。"
        onSubmitSingle={onSingle}
        onSubmitMulti={vi.fn()}
      />,
    );
    expect(screen.getAllByRole("radio")[1].getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("alert").textContent).toContain("没能提交回答，你的选择已保留。");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onSingle).toHaveBeenCalledWith(expect.objectContaining({ answer: "华东大区" }), undefined);
  });

  it("spins Continue while submitting and locks the options", () => {
    renderIntl(<ClarificationCard questions={QUESTIONS} busy onSubmitSingle={vi.fn()} onSubmitMulti={vi.fn()} />);
    expect(screen.getByRole("button", { name: "正在提交…" }).getAttribute("aria-busy")).toBe("true");
    expect(screen.getAllByRole("radio").every((r) => (r as HTMLButtonElement).disabled)).toBe(true);
  });

  it("says why it cannot be answered yet and links the reason to Continue", () => {
    const onSingle = vi.fn();
    renderIntl(
      <ClarificationCard
        questions={QUESTIONS}
        unavailableReason="正在保存进度，几秒后就可以回答。"
        onSubmitSingle={onSingle}
        onSubmitMulti={vi.fn()}
      />,
    );
    expect(screen.getAllByRole("radio").every((r) => (r as HTMLButtonElement).disabled)).toBe(true);
    const button = continueButton();
    const reasonId = button.getAttribute("aria-describedby");
    expect(reasonId && document.getElementById(reasonId)?.textContent).toBe("正在保存进度，几秒后就可以回答。");
    fireEvent.click(button);
    expect(onSingle).not.toHaveBeenCalled();
  });
});

describe("ClarificationCard · document draft (spec §3) and skip wording", () => {
  it("reads as a document pause: eyebrow, meta, Continue generating and the retention note", () => {
    const onSingle = vi.fn();
    renderIntl(<ClarificationCard questions={QUESTIONS} documentDraft onSubmitSingle={onSingle} onSubmitMulti={vi.fn()} onSkip={vi.fn()} />);
    const card = screen.getByTestId("clarification-card");
    expect(card.getAttribute("data-state")).toBe("document_draft");
    expect(within(card).getByText("需要补充信息")).toBeTruthy();
    expect(within(card).getByText("文档草稿 · 单选")).toBeTruthy();
    expect(within(card).getByText("回答后会保留原需求、模板和数据，并重新检查可用性。")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("radio")[0]);
    fireEvent.click(screen.getByRole("button", { name: "继续生成" }));
    expect(onSingle).toHaveBeenCalledTimes(1);
  });

  it("says Skip all for several questions and Skip and let the model decide for one", () => {
    const { unmount } = renderIntl(<ClarificationCard questions={MIXED_QUESTIONS} onSubmitSingle={vi.fn()} onSubmitMulti={vi.fn()} onSkip={vi.fn()} />);
    expect(screen.getByRole("button", { name: "全部跳过" })).toBeTruthy();
    unmount();
    renderIntl(<ClarificationCard questions={QUESTIONS} onSubmitSingle={vi.fn()} onSubmitMulti={vi.fn()} onSkip={vi.fn()} />);
    expect(screen.getByRole("button", { name: "跳过，让模型判断" })).toBeTruthy();
  });

  it("does not submit on the Enter that ends an IME composition (keyCode 229)", () => {
    const onSingle = vi.fn();
    renderIntl(<ClarificationCard questions={QUESTIONS} onSubmitSingle={onSingle} onSubmitMulti={vi.fn()} />);
    const radios = screen.getAllByRole("radio");
    fireEvent.click(radios[0]);
    fireEvent.keyDown(radios[0], { key: "Enter", keyCode: 229 });
    expect(onSingle).not.toHaveBeenCalled();
    fireEvent.keyDown(radios[0], { key: "Enter", keyCode: 13 });
    expect(onSingle).toHaveBeenCalledTimes(1);
  });
});

describe("ClarificationCard · focus after submitting (keyboard and screen reader)", () => {
  it("the answered row takes focus when the card it replaces had it", () => {
    (document.activeElement as HTMLElement | null)?.blur();
    renderIntl(<ClarificationAnsweredSummary values={["华东"]} resuming />);
    expect(document.activeElement?.classList.contains("ui-card-turn-slot")).toBe(true);
  });

  it("never takes focus from elsewhere, such as the composer", () => {
    const input = document.createElement("textarea");
    document.body.appendChild(input);
    input.focus();
    renderIntl(<ClarificationAnsweredSummary values={["华东"]} />);
    expect(document.activeElement).toBe(input);
    input.remove();
  });

  it("a failed submission focuses the card's Retry", () => {
    (document.activeElement as HTMLElement | null)?.blur();
    renderIntl(
      <ClarificationCard
        questions={QUESTIONS}
        restoredAnswers={[{ question: QUESTIONS[0].question, source: "candidate", answer: "零售集团", option_index: 0 }]}
        error="提交失败"
        onSubmitSingle={vi.fn()}
        onSubmitMulti={vi.fn()}
      />,
    );
    expect(document.activeElement?.textContent).toContain("重试");
  });
});

describe("ClarificationAnsweredSummary", () => {
  it("settles an answer into a success row; several values join with「；」", () => {
    renderIntl(<ClarificationAnsweredSummary values={["促销活动", "品牌排名"]} />);
    const row = screen.getByTestId("clarification-answered");
    expect(row.className).toContain("ui-card--row");
    expect(row.className).toContain("ui-card--success");
    expect(row.textContent).toBe("已回答：促销活动；品牌排名");
  });

  it("settles a skip into an info row", () => {
    renderIntl(<ClarificationAnsweredSummary values={[]} skipped />);
    expect(screen.getByTestId("clarification-answered").textContent).toBe("已跳过，由模型根据上下文判断");
  });

  it("follows with a progress row while the turn continues", () => {
    const { unmount } = renderIntl(<ClarificationAnsweredSummary values={["A"]} resuming />);
    expect(screen.getByTestId("clarification-resuming").textContent).toBe("正在按你的选择继续…");
    unmount();
    renderIntl(<ClarificationAnsweredSummary values={["A"]} resuming={false} />);
    expect(screen.queryByTestId("clarification-resuming")).toBeNull();
  });

  it("offers Ask again for an expired question and Refresh when the turn could not continue", () => {
    const onAskAgain = vi.fn();
    const onRefresh = vi.fn();
    const { unmount } = renderIntl(<ClarificationAnsweredSummary values={[]} expired onAskAgain={onAskAgain} />);
    fireEvent.click(screen.getByRole("button", { name: "重新提问" }));
    expect(onAskAgain).toHaveBeenCalledTimes(1);
    unmount();
    renderIntl(<ClarificationAnsweredSummary values={["A"]} resumeFailed onRefresh={onRefresh} resuming />);
    expect(screen.getByTestId("clarification-resume-failed").textContent).toContain("已记下你的选择");
    expect(screen.queryByTestId("clarification-resuming")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});
