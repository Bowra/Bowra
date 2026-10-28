/**
 * Clarification card options render labels while submitting canonical values.
 *
 * Incident: an English user's question and Lead's clarification were in English, but options were datarc's canonical Chinese names
 * `["一级品牌名称英文","品规","产品组","市场分类一级"]`. Translation must not change `options` directly:
 * those strings must return unchanged to the model to build `bi_query`; datarc recognizes only the canonical Chinese names.
 */

import { describe, expect, it } from "vitest";

import { buildOptionValueByLabel, relabelQuestions } from "./clarification-option-labels";
import type { ThetaClarificationQuestion } from "@/lib/types";

// Synthetic clarification with translated labels and one untranslated canonical value.
const exampleQuestions = (): ThetaClarificationQuestion[] => [
  {
    question: "For “total sales this year by product,” which product level should I use?",
    options: ["一级品牌名称英文", "品规", "产品组", "市场分类一级"],
    // datarc has no en-US label for `品规` → fall back unchanged; mixed languages are **correct**, never invent a label.
    option_labels: ["Brand", "品规", "Product Group", "Market L1"],
    type: "single_select",
  },
];

describe("relabelQuestions", () => {
  it("有展示名 → 渲染换成展示名", () => {
    expect(relabelQuestions(exampleQuestions())[0]!.options).toEqual(["Brand", "品规", "Product Group", "Market L1"]);
  });

  it("没有 option_labels → 同一个引用原样返回（memo 友好，逐字节同今天）", () => {
    const qs: ThetaClarificationQuestion[] = [{ question: "q", options: ["甲", "乙"], type: "single_select" }];
    expect(relabelQuestions(qs)).toBe(qs);
  });

  it("长度对不上 → 不换（宁可全中文，也不能错位）", () => {
    const qs: ThetaClarificationQuestion[] = [
      { question: "q", options: ["甲", "乙", "丙"], option_labels: ["A", "B"], type: "single_select" },
    ];
    expect(relabelQuestions(qs)[0]!.options).toEqual(["甲", "乙", "丙"]);
  });
});

describe("buildOptionValueByLabel", () => {
  it("展示名 → 规范值；未改名的那项不进表（提交所见即所得）", () => {
    expect(buildOptionValueByLabel(exampleQuestions())).toEqual({
      Brand: "一级品牌名称英文",
      "Product Group": "产品组",
      "Market L1": "市场分类一级",
    });
  });

  it("没有改名 → null", () => {
    expect(buildOptionValueByLabel([{ question: "q", options: ["甲"], type: "single_select" }])).toBeNull();
    expect(buildOptionValueByLabel(undefined)).toBeNull();
  });

  it("★ label 跨问题重名 → 整张卡都不换（回传值会串到别的问题上）", () => {
    const qs: ThetaClarificationQuestion[] = [
      { question: "q1", options: ["销售额"], option_labels: ["Value"], type: "single_select" },
      { question: "q2", options: ["销售金额"], option_labels: ["Value"], type: "single_select" },
    ];
    expect(buildOptionValueByLabel(qs)).toBeNull();
    // Rendering must use the same condition; otherwise it half-works by showing English but submitting the label unchanged.
    expect(relabelQuestions(qs)).toBe(qs);
  });

  it("★ 展示名撞上另一项的规范名 → 同样整体不换", () => {
    const qs: ThetaClarificationQuestion[] = [
      { question: "q", options: ["甲", "乙"], option_labels: ["乙", "B"], type: "single_select" },
    ];
    expect(buildOptionValueByLabel(qs)).toBeNull();
  });
});
