import { describe, expect, it } from "vitest";

import { contentExcerpt, displayWidth, splitMatches } from "./search-match";

describe("contentExcerpt", () => {
  it("keeps the term within the first columns of a long CJK message", () => {
    const message =
      "这是一段很长很长的提问，先交代了背景、口径和时间范围，然后才在后面问到老客复购周期和新客贡献的拆分，最后还补充了渠道差异与季节因素";
    const excerpt = contentExcerpt(message, "复购");

    // About 5 CJK characters of lead-in: the term starts within the first 12 display columns.
    const lead = excerpt.slice(0, excerpt.indexOf("复购"));
    expect(
      Array.from(lead).reduce(
        (sum, character) => sum + displayWidth(character),
        0,
      ),
    ).toBeLessThanOrEqual(12);
    expect(excerpt).toBe("…面问到老客复购周期和新客贡献的拆分，最…");
  });

  it("counts Latin characters as one column", () => {
    expect(
      contentExcerpt(
        "Please look at the repurchase cycle of returning customers this quarter",
        "repurchase",
      ),
    ).toBe("…ok at the repurchase cycle of returning cust…");
  });

  it("leaves a short message whole and marks a cut only where text was dropped", () => {
    expect(contentExcerpt("老客复购周期", "复购")).toBe("老客复购周期");
    expect(contentExcerpt("…看一下老客复购周期", "复购")).toBe(
      "…看一下老客复购周期",
    );
  });

  it("returns the normalized text when the term is absent", () => {
    expect(contentExcerpt("  没有   命中  ", "复购")).toBe("没有 命中");
  });
});

describe("splitMatches", () => {
  it("marks every case-insensitive occurrence", () => {
    expect(splitMatches("GMV and gmv", "gmv")).toEqual([
      { text: "GMV", match: true },
      { text: " and ", match: false },
      { text: "gmv", match: true },
    ]);
  });
});
