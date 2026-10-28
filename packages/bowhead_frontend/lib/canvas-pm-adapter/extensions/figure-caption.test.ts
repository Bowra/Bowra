/**
 * Batch 1 §2.1 · figure-caption attachment golden cases. This file tests only TS behavior; **cross-language parity is
 * enforced by packages/bowhead_core/tests/test_figure_caption_parity.py** (it reads these
 * golden cases, compares with PY, and extracts regex literals from source). Update both sets together when changing rules.
 */

import { describe, expect, it } from "vitest";

import { captionPrefixLength, stripCaptionPrefix } from "./figure-caption";

// ⚠ Identical literal fixtures (exactly match PY GOLDEN_CAPTION_CASES)
const GOLDEN_HITS: Array<[string, string]> = [
  ["图1：营业额自 1 月高点一路走低", "营业额自 1 月高点一路走低"],
  ["**图1**：营业额自 1 月高点一路走低", "营业额自 1 月高点一路走低"],
  ["图 12：分渠道占比对比", "分渠道占比对比"],
  ["图3: 英文冒号也认", "英文冒号也认"],
  ["**图2** ：星号后空格再冒号", "星号后空格再冒号"],
];

const GOLDEN_MISSES: string[] = [
  "图表1：图表开头不是图N",
  "营业额走低，无前缀",
  "如图1所示，营收下滑",
  "[1] 来源引用不是图注",
];

describe("CAPTION_RE golden（跨语言对拍）", () => {
  it.each(GOLDEN_HITS)("命中并剥前缀: %s", (input, expected) => {
    expect(captionPrefixLength(input)).toBeGreaterThan(0);
    expect(stripCaptionPrefix(input)).toBe(expected);
  });

  it.each(GOLDEN_MISSES)("不命中原样返回: %s", (input) => {
    expect(captionPrefixLength(input)).toBe(0);
    expect(stripCaptionPrefix(input)).toBe(input);
  });

  it("PM textContent 载体（无 markdown 语法）与 markdown 原文同规则", () => {
    // Decorations scan textContent: "**图1**：x" becomes "图1：x" with a bold mark in PM
    expect(stripCaptionPrefix("图1：营业额走低")).toBe("营业额走低");
    expect(stripCaptionPrefix("**图1**：营业额走低")).toBe("营业额走低");
  });
});
