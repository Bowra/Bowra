/**
 * Unit tests for provider-registry · preferred_model pack/unpack.
 * (Legacy provider templates and their tests were removed together in the tiering sprint, 2026-07-22.)
 */

import { describe, expect, it } from "vitest";

import { packPreferredModel, unpackPreferredModel } from "./provider-registry";

describe("pack/unpack preferred_model", () => {
  it("round trip aliyun_qwen + qwen-plus", () => {
    const s = packPreferredModel("aliyun_qwen", "qwen-plus");
    expect(s).toBe("aliyun_qwen:qwen-plus");
    expect(unpackPreferredModel(s)).toEqual({ providerId: "aliyun_qwen", model: "qwen-plus" });
  });

  it("model 含冒号也能解析 (取第一个 : split)", () => {
    // e.g. anthropic model names contain datestamps
    const s = packPreferredModel("anthropic", "claude-opus-4-7-2026:01:01");
    expect(unpackPreferredModel(s)).toEqual({
      providerId: "anthropic",
      model: "claude-opus-4-7-2026:01:01",
    });
  });

  it("malformed → null", () => {
    expect(unpackPreferredModel("no-colon-here")).toBeNull();
  });
});
