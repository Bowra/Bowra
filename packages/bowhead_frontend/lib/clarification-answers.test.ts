import { describe, expect, it } from "vitest";

import {
  QUESTION_SKIP_VALUE,
  canonicalizeAnswers,
  projectAnswerValues,
} from "./clarification-answers";
import {
  buildOptionValueByLabel,
  relabelQuestions,
} from "./clarification-option-labels";
import { parseThetaQuestions } from "./clarification-questions";
import type { ClarificationAnswer } from "./types";

describe("canonicalizeAnswers", () => {
  const table = { Brand: "一级品牌名称英文", SKU: "品规" };

  it("candidate 答案换回规范值", () => {
    const answers: ClarificationAnswer[] = [
      { question: "按哪个维度拆?", source: "candidate", answer: "Brand", option_index: 0 },
    ];
    expect(canonicalizeAnswers(answers, table)).toEqual([
      { question: "按哪个维度拆?", source: "candidate", answer: "一级品牌名称英文", option_index: 0 },
    ]);
  });

  it("custom 是用户手打的自由文本，绝不查表改写", () => {
    const answers: ClarificationAnswer[] = [
      { question: "q", source: "custom", answer: "Brand" },
    ];
    expect(canonicalizeAnswers(answers, table)).toBe(answers);
  });

  it("skip 不动", () => {
    const answers: ClarificationAnswer[] = [
      { question: "q", source: "skip", skipped: true },
    ];
    expect(canonicalizeAnswers(answers, table)).toBe(answers);
  });

  it("无换名表 / 表里没有这一项 → 同一个引用", () => {
    const answers: ClarificationAnswer[] = [
      { question: "q", source: "candidate", answer: "线上" },
    ];
    expect(canonicalizeAnswers(answers, null)).toBe(answers);
    expect(canonicalizeAnswers(answers, table)).toBe(answers);
  });
});

describe("projectAnswerValues", () => {
  it("单问题 → 裸答案", () => {
    expect(
      projectAnswerValues(
        [{ question: "按哪个维度拆?", source: "candidate", answer: "品规" }],
        { multiQuestion: false },
      ),
    ).toEqual(["品规"]);
  });

  it("多问题 → 带问题前缀", () => {
    expect(
      projectAnswerValues(
        [
          { question: "维度?", source: "candidate", answer: "品规" },
          { question: "时间?", source: "custom", answer: "今年至今" },
        ],
        { multiQuestion: true },
      ),
    ).toEqual(["维度?: 品规", "时间?: 今年至今"]);
  });

  it("跳过的题恒带问题前缀——后端要靠它拆回 (question, skip)", () => {
    expect(
      projectAnswerValues(
        [{ question: "重点看哪些指标?", source: "skip", skipped: true }],
        { multiQuestion: false },
      ),
    ).toEqual([`重点看哪些指标?: ${QUESTION_SKIP_VALUE}`]);
  });
});

// Wiring regression: SSE payload → parse → relabel → submit. Previously broken at the **parser** (option_labels
// was dropped by the whitelist), while each pure-function unit test passed. Starting from raw payload means any segment
// breaking again fails this test (2026-09-04 HITL audit §4).
describe("多选卡的展示名/规范值分离（端到端）", () => {
  const payload = [
    {
      question: "Which dimensions?",
      options: ["一级品牌名称英文", "品规"],
      option_labels: ["Brand", "SKU"],
      type: "multi_select",
    },
  ];

  it("渲染用展示名，提交用规范值", () => {
    const parsed = parseThetaQuestions(payload)!;
    const rendered = relabelQuestions(parsed);
    // Users see English.
    expect(rendered[0]!.options).toEqual(["Brand", "SKU"]);

    // The card constructs answers from rendered strings (simulate selecting two options).
    const submitted: ClarificationAnswer[] = rendered[0]!.options.map((label, i) => ({
      question: rendered[0]!.question,
      source: "candidate" as const,
      answer: label,
      option_index: i,
    }));

    const canonical = canonicalizeAnswers(
      submitted,
      buildOptionValueByLabel(parsed),
    );
    expect(canonical.map((a) => (a.source === "candidate" ? a.answer : ""))).toEqual([
      "一级品牌名称英文",
      "品规",
    ]);
    expect(projectAnswerValues(canonical, { multiQuestion: false })).toEqual([
      "一级品牌名称英文",
      "品规",
    ]);
  });

  it("展示名撞车 → 整张卡退回规范名，提交值仍然对", () => {
    const parsed = parseThetaQuestions([
      { question: "q1", options: ["销售额"], option_labels: ["Value"] },
      { question: "q2", options: ["销售金额"], option_labels: ["Value"] },
    ])!;
    expect(relabelQuestions(parsed)).toBe(parsed);
    expect(buildOptionValueByLabel(parsed)).toBeNull();
  });
});
