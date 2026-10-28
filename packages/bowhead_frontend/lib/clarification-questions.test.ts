import { describe, expect, it } from "vitest";

import { parseThetaQuestions } from "./clarification-questions";

describe("parseThetaQuestions (shared)", () => {
  it("keeps all valid B5 questions, trims, defaults type", () => {
    expect(
      parseThetaQuestions([
        { question: " 看哪个区域? ", options: [" 全国 ", "华东"], type: "single_select" },
        { question: "看哪些渠道?", options: ["线上", "线下"], type: "multi_select" },
      ]),
    ).toEqual([
      { question: "看哪个区域?", options: ["全国", "华东"], type: "single_select" },
      { question: "看哪些渠道?", options: ["线上", "线下"], type: "multi_select" },
    ]);
  });

  it("defaults missing/non-string type to single_select", () => {
    expect(parseThetaQuestions([{ question: "q", options: ["a"] }])).toEqual([
      { question: "q", options: ["a"], type: "single_select" },
    ]);
  });

  it("retains free_text with no options in live and replay parsing", () => {
    expect(parseThetaQuestions([{ question: "平均所有者权益是多少？", options: [], type: "free_text" }])).toEqual([
      { question: "平均所有者权益是多少？", options: [], type: "free_text" },
    ]);
  });

  it("drops malformed entries while preserving empty-option free-text questions", () => {
    expect(
      parseThetaQuestions([
        { question: "  ", options: ["a"] }, // blank question
        { question: "q2", options: [] }, // free-text/skip question
        { question: "q3", options: ["", "  "] }, // free-text/skip after blank options filtered
        { question: "q4", options: [1, "ok", null] }, // non-string filtered
        "nope", // non-object
        null,
      ]),
    ).toEqual([
      { question: "q2", options: [], type: "single_select" },
      { question: "q3", options: [], type: "single_select" },
      { question: "q4", options: ["ok"], type: "single_select" },
    ]);
  });

  it("returns undefined for non-array or all-invalid input", () => {
    expect(parseThetaQuestions(undefined)).toBeUndefined();
    expect(parseThetaQuestions("x")).toBeUndefined();
    expect(parseThetaQuestions([{ question: "", options: [] }])).toBeUndefined();
  });
});

// 2026-09-04 HITL audit §4: this layer previously discarded all of `option_labels`, so
// `relabelQuestions` always received undefined and production renaming never happened, despite green pure-function unit tests.
describe("parseThetaQuestions · option_labels 接线", () => {
  it("等长且全非空 → 保留", () => {
    expect(
      parseThetaQuestions([
        {
          question: "按哪个维度拆?",
          options: ["一级品牌名称英文", "品规"],
          option_labels: [" Brand ", "SKU"],
          type: "single_select",
        },
      ]),
    ).toEqual([
      {
        question: "按哪个维度拆?",
        options: ["一级品牌名称英文", "品规"],
        type: "single_select",
        option_labels: ["Brand", "SKU"],
      },
    ]);
  });

  it("长度对不上 → 整个丢掉（错位配对比看中文更糟）", () => {
    const [q] = parseThetaQuestions([
      { question: "q", options: ["甲", "乙", "丙"], option_labels: ["A", "B"] },
    ])!;
    expect(q.option_labels).toBeUndefined();
    expect(q.options).toEqual(["甲", "乙", "丙"]);
  });

  it("含空串 → 整个丢掉", () => {
    const [q] = parseThetaQuestions([
      { question: "q", options: ["甲", "乙"], option_labels: ["A", "  "] },
    ])!;
    expect(q.option_labels).toBeUndefined();
  });

  it("options 被过滤过 → 丢掉（index 已错位）", () => {
    const [q] = parseThetaQuestions([
      { question: "q", options: ["甲", "", "丙"], option_labels: ["A", "C"] },
    ])!;
    expect(q.options).toEqual(["甲", "丙"]);
    expect(q.option_labels).toBeUndefined();
  });

  it("非数组 / 缺省 → 不加这个键", () => {
    const [q] = parseThetaQuestions([
      { question: "q", options: ["甲"], option_labels: "A" },
    ])!;
    expect("option_labels" in q).toBe(false);
  });
});
