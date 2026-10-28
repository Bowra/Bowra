import { describe, expect, it } from "vitest";

import { pickCandidates } from "./use-autocomplete";

describe("history completion with resource references", () => {
  it("does not suggest old Agent or Skill names as unbound text", () => {
    expect(pickCandidates("请用", [
      "请用 @经营分析师 /杜邦分析 计算 ROE",
      "请用普通方法计算 ROE",
    ])).toEqual(["普通方法计算 ROE"]);
  });
});
