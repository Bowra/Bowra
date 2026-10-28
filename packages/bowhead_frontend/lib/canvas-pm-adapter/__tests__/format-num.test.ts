/**
 * 0.24.3 — lock format-num.ts shared util behavior. Prevents future drift
 * between citation-plugin (canvas) and agent-section (chat .insight row).
 */
import { describe, it, expect } from "vitest";
import {
  formatNumValue,
  stripMarkersForPlainText,
  NUM_NEW_RE,
  NUM_OLD_RE,
} from "../format-num";

describe("formatNumValue", () => {
  it("CNY 亿/万 thresholds", () => {
    expect(formatNumValue("100000000", "CNY")).toBe("1亿元");
    expect(formatNumValue("723119586", "CNY")).toBe("7.23亿元");
    expect(formatNumValue("51800000", "CNY")).toBe("5,180万元");
    expect(formatNumValue("9999", "CNY")).toBe("9,999元");
  });
  it("percent multiplies by 100", () => {
    expect(formatNumValue("0.512", "percent")).toBe("51.2%");
  });
  it("int / no-unit uses locale", () => {
    expect(formatNumValue("2319500", "")).toBe("231.95万");
    expect(formatNumValue("12345", "int")).toBe("1.23万");
  });
  it("unknown unit suffixes literally", () => {
    expect(formatNumValue("1473", "万元")).toBe("1,473万元");
    expect(formatNumValue("267", "人次")).toBe("267人次");
  });
  it("scientific notation parses (NOT silently leaks)", () => {
    expect(formatNumValue("1.2e10", "CNY")).toBe("120亿元");
  });
  it("non-numeric falls back to input", () => {
    expect(formatNumValue("abc", "")).toBe("abc");
  });
});

describe("stripMarkersForPlainText", () => {
  it("replaces [num:V:U] with formatted value", () => {
    expect(stripMarkersForPlainText("营收 [num:1473:万元] 增长"))
      .toBe("营收 1,473万元 增长");
  });
  it("replaces [num:V:U:src:S] (new format)", () => {
    expect(stripMarkersForPlainText("利润 [num:51800000:CNY:src:tl_001]"))
      .toBe("利润 5,180万元");
  });
  it("drops [ref:S] and [chart:S] (whitespace collapsed)", () => {
    expect(stripMarkersForPlainText("详见 [ref:s_001] 及 [chart:c_002]"))
      .toBe("详见 及");
  });
  it("handles scientific notation (0.24.2 regex would leak)", () => {
    expect(stripMarkersForPlainText("[num:1.2e10:CNY:src:s_x]"))
      .toBe("120亿元");
  });
  it("preserves text without markers", () => {
    expect(stripMarkersForPlainText("阿迪达斯全年领先")).toBe("阿迪达斯全年领先");
  });
});
