import { describe, expect, it } from "vitest";

import { autoJudgeModel } from "./judge-auto";

describe("autoJudgeModel", () => {
  const primary = { provider: "dashscope", model: "qwen3.6-plus" };

  it("picks another model at the same provider when the team has one connection", () => {
    expect(autoJudgeModel([{ providerKey: "dashscope", id: "qwen3.6-plus" }, { providerKey: "dashscope", id: "qwen3.6-flash" }], primary))
      .toEqual({ provider: "dashscope", model: "qwen3.6-flash" });
  });

  it("prefers a model at another provider when one exists", () => {
    expect(autoJudgeModel([
      { providerKey: "dashscope", id: "qwen3.6-flash" },
      { providerKey: "deepseek", id: "deepseek-v4-flash" },
    ], primary)).toEqual({ provider: "deepseek", model: "deepseek-v4-flash" });
  });

  it("returns null when the main model is the only chat model", () => {
    expect(autoJudgeModel([{ providerKey: "dashscope", id: "qwen3.6-plus" }], primary)).toBeNull();
    expect(autoJudgeModel([{ providerKey: "dashscope", id: "qwen3.6-plus" }], null)).toBeNull();
  });
});
