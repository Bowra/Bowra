/** Batch 1-d WP-2: DTO ↔ ConnectDraft mapping (three type mismatches in tech-design-batch1d §7-2). */

import { describe, expect, it } from "vitest";

import { mapProviderDraftToConnect, pickSecretForApiKey } from "./provider-draft-map";

describe("mapProviderDraftToConnect", () => {
  it("snake_case → camelCase 逐字段改名", () => {
    const patch = mapProviderDraftToConnect({
      protocol: "openai_compatible",
      name: "dashscope",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      api_version: "2024-05-01",
      messages_path: "/messages",
      auth_type: "api_key",
      auth_header_name: "x-api-key",
      auth_prefix: "Token ",
    });
    expect(patch).toEqual({
      protocol: "openai_compatible",
      name: "dashscope",
      baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      apiVersion: "2024-05-01",
      messagesPath: "/messages",
      authType: "api_key",
      authHeaderName: "x-api-key",
      authPrefix: "Token ",
    });
  });

  it("模型没产出的字段不出现在 patch 里——副驾是填表不是重置表", () => {
    const patch = mapProviderDraftToConnect({ name: "x" });
    expect(Object.keys(patch)).toEqual(["name"]);
    expect("baseUrl" in patch).toBe(false);
  });

  it("request_body_overrides 要转成 JSON 文本（宿主是 textarea，不是对象）", () => {
    const patch = mapProviderDraftToConnect({
      models: [{ id: "qwen-plus", caps: ["chat"], request_body_overrides: { chat: { enable_thinking: false } } }],
    });
    const model = patch.models![0];
    expect(typeof model.requestBodyOverrides).toBe("string");
    expect(JSON.parse(model.requestBodyOverrides!)).toEqual({ chat: { enable_thinking: false } });
  });

  it("空 overrides 不产出字段（免得 textarea 里出现一个 {}）", () => {
    const patch = mapProviderDraftToConnect({ models: [{ id: "m", caps: ["chat"], request_body_overrides: {} }] });
    expect("requestBodyOverrides" in patch.models![0]).toBe(false);
  });

  it("source 必填而 DTO 没有 → 补 manual", () => {
    const patch = mapProviderDraftToConnect({ models: [{ id: "m", caps: ["chat"] }] });
    expect(patch.models![0].source).toBe("manual");
  });

  it("丢弃没有 id 的模型行，caps 缺省为空数组", () => {
    const patch = mapProviderDraftToConnect({ models: [{ id: "  ", caps: ["chat"] }, { id: " m2 " }] });
    expect(patch.models).toEqual([{ id: "m2", caps: [], source: "manual" }]);
  });
});

describe("pickSecretForApiKey", () => {
  it("curl 的真实形状：kind=kv / label=Authorization / value 带 Bearer 前缀", () => {
    // Design v1 used separate label/kind criteria that each matched the other shape (review P2-2); decide by value instead
    const result = pickSecretForApiKey([
      { value: "Bearer sk-FAKE1234567890abcdefFAKE9d0e1234", label: "Authorization", kind: "kv" },
    ]);
    expect(result.apiKey).toBe("sk-FAKE1234567890abcdefFAKE9d0e1234");
    expect(result.authType).toBe("bearer");
  });

  it("裸密钥不加 authType（不替用户改认证方式）", () => {
    const result = pickSecretForApiKey([{ value: "sk-FAKE1234567890abcdefFAKE9d0e1234", label: "API_KEY", kind: "kv" }]);
    expect(result.apiKey).toBe("sk-FAKE1234567890abcdefFAKE9d0e1234");
    expect(result.authType).toBeUndefined();
  });

  it("多个候选只填第一个，其余把 label 交回给用户选——不猜", () => {
    const result = pickSecretForApiKey([
      { value: "sk-first", label: "API_KEY", kind: "kv" },
      { value: "pg-pass", label: "DB_PASSWORD", kind: "kv" },
      { value: "ghp_x", label: null, kind: "token" },
    ]);
    expect(result.apiKey).toBe("sk-first");
    expect(result.others).toEqual(["DB_PASSWORD", "token"]);
  });

  it("无候选 / 空值 → 不填", () => {
    expect(pickSecretForApiKey([]).apiKey).toBeNull();
    expect(pickSecretForApiKey([{ value: "   " }]).apiKey).toBeNull();
    expect(pickSecretForApiKey([{ value: "Bearer   " }]).apiKey).toBeNull();
  });
});
