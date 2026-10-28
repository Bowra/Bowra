import { describe, expect, it } from "vitest";

import {
  buildProviderModelSpec,
  connectDraftHasModel,
  connectDraftModelIdsUnique,
  customHttpBaseUrlSatisfied,
  draftApiKeyForEdit,
  modelRequestBodyOverridesValid,
  PROVIDER_DRAFT_BACKEND_KEYS,
  parseModelRequestBodyOverrides,
  providerConsumesBodyOverrides,
  providerConsumesOutboundParams,
  shouldOfferProviderCopilot,
  suggestedCapabilitiesForModelId,
  providerSignature,
  v2ProviderDraftFromConnect,
  v2ProviderDraftFromSavedConfig,
} from "./models";

describe("model source connect progress", () => {
  it("round-trips declared output capacity without saving automatic defaults", () => {
    const row = { id: "deployment", caps: ["chat"], source: "manual" as const, maxOutputTokens: 128000 };
    expect(buildProviderModelSpec("openai_compatible", row).max_output_tokens).toBe(128000);
    const source = { type: "openai_compatible", base_url: "https://example.com/v1", max_output_tokens: 64000, models: [{ id: row.id, capabilities: row.caps, max_output_tokens: 128000 }, { id: "automatic", capabilities: ["chat"] }] };
    const draft = v2ProviderDraftFromSavedConfig("example", "llmp_example", source as never);
    expect(draft.max_output_tokens).toBe(64000);
    expect(draft.models[0].max_output_tokens).toBe(128000);
    expect(draft.models[1]).not.toHaveProperty("max_output_tokens");
    expect(buildProviderModelSpec("openai_compatible", { id: "automatic", caps: ["chat"], source: "manual" })).not.toHaveProperty("max_output_tokens");
  });

  it("does not mark a blank catalog row as filled", () => {
    expect(connectDraftHasModel([{ id: "" }])).toBe(false);
    expect(connectDraftHasModel([{ id: "   " }])).toBe(false);
  });

  it("advances only after a real model id is present", () => {
    expect(connectDraftHasModel([{ id: "" }, { id: "deepseek-v4-pro" }])).toBe(true);
  });

  it("rejects duplicate model ids after trimming", () => {
    expect(connectDraftModelIdsUnique([
      { id: "gpt-51" },
      { id: " gpt-51 " },
    ])).toBe(false);
    expect(connectDraftModelIdsUnique([
      { id: "gpt-51" },
      { id: "gpt-54" },
      { id: "" },
    ])).toBe(true);
  });

  it("keeps a new provider secret in the unapplied browser draft only", () => {
    expect(draftApiKeyForEdit({ api_key: "draft-secret" }, false))
      .toBe("draft-secret");
    expect(draftApiKeyForEdit({ api_key: "saved-secret" }, true)).toBe("");
    expect(draftApiKeyForEdit({}, false)).toBe("");
  });

  it("classifies only unambiguous embedding model names", () => {
    expect(suggestedCapabilitiesForModelId("text-embedding-3-small")).toEqual([
      "embed",
    ]);
    expect(suggestedCapabilitiesForModelId("gpt-51")).toEqual([]);
  });

  it("allows a full custom URL without a separate base URL", () => {
    expect(customHttpBaseUrlSatisfied("", [
      "https://gateway.example.com/company/chat",
    ])).toBe(true);
    expect(customHttpBaseUrlSatisfied("", [
      "{base_url}/company/chat",
    ])).toBe(false);
    expect(customHttpBaseUrlSatisfied("https://gateway.example.com", [
      "{base_url}/company/chat",
    ])).toBe(true);
  });

  it("accepts only capability-keyed model request body overrides", () => {
    expect(parseModelRequestBodyOverrides(
      "{\"chat\":{\"temperature\":0}}",
    )).toEqual({ chat: { temperature: 0 } });
    expect(parseModelRequestBodyOverrides("{\"chat\":0}")).toBeNull();
    expect(parseModelRequestBodyOverrides("[]")).toBeNull();
    expect(modelRequestBodyOverridesValid(
      "{\"chat\":{\"temperature\":0}}",
      ["chat"],
    )).toBe(true);
    expect(modelRequestBodyOverridesValid(
      "{\"embed\":{}}",
      ["chat"],
    )).toBe(false);
  });
});

describe("scoped runtime parameter persistence", () => {
  const runtime = {
    profile: "dashscope-mixed-chat-v1",
    defaults: { thinking_mode: "off" as const },
    capability_overrides: {
      tool: {
        thinking_mode: "on" as const,
        outbound_params: {
          parallel_tool_calls: { mode: "fixed" as const, value: false },
        },
      },
    },
  };
  const outbound = { temperature: { mode: "fixed" as const, value: 0.4 } };
  const row = {
    id: "example-model",
    caps: ["chat", "tool"],
    source: "manual" as const,
    runtimeParameters: runtime,
    outboundParams: outbound,
    contextWindow: 128000,
    requestBodyOverrides: { chat: { vendor_option: false } },
  };

  it("carries scoped policy and boolean false into saved model specifications", () => {
    expect(buildProviderModelSpec("openai_compatible", row)).toMatchObject({
      runtime_parameters: runtime,
      outbound_params: outbound,
      context_window: 128000,
      request_body_overrides: row.requestBodyOverrides,
    });
  });

  it("retains model and provider baselines in verification and reopened drafts", () => {
    const connect = v2ProviderDraftFromConnect({
      mode: "add",
      name: "service",
      protocol: "openai_compatible",
      baseUrl: "https://api.example.com/v1",
      apiKey: "draft-secret",
      messagesPath: "",
      providerOutboundParams: outbound,
      providerContextWindow: 256000,
      models: [
        {
          ...row,
          requestBodyOverrides: JSON.stringify(row.requestBodyOverrides),
        },
      ],
    } as never);
    const saved = v2ProviderDraftFromSavedConfig(
      "service",
      "llmp_00000000000000000000000000000000",
      {
        type: "openai_compatible",
        base_url: "https://api.example.com/v1",
        api_key: "saved-secret",
        outbound_params: outbound,
        context_window: 256000,
        models: [buildProviderModelSpec("openai_compatible", row)],
      } as never,
    );
    for (const draft of [connect, saved]) {
      expect(draft).toMatchObject({
        outbound_params: outbound,
        context_window: 256000,
      });
      expect(draft.models[0]).toMatchObject({
        runtime_parameters: runtime,
        outbound_params: outbound,
        context_window: 128000,
        request_body_overrides: row.requestBodyOverrides,
      });
    }
    expect(saved.api_key).toBeUndefined();
  });

  it("invalidates local evidence after a scoped policy edit", () => {
    const before = providerSignature(
      "openai_compatible",
      "https://api.example.com/v1",
      "",
      [row],
      "token",
    );
    const after = providerSignature(
      "openai_compatible",
      "https://api.example.com/v1",
      "",
      [
        {
          ...row,
          runtimeParameters: {
            ...runtime,
            defaults: { thinking_mode: "on" as const },
          },
        },
      ],
      "token",
    );
    expect(after).not.toBe(before);
  });
});

describe("批 1-d：request_body_overrides 的落库门（tech-design-batch1d §7-5）", () => {
  const row = (extra: Record<string, unknown> = {}) => ({
    id: "qwen-plus",
    caps: ["chat"],
    source: "manual" as const,
    requestBodyOverrides: { chat: { enable_thinking: false } },
    ...extra,
  });

  it("openai_compatible 也要落库——qwen 的 enable_thinking 正是这个场景", () => {
    const spec = buildProviderModelSpec("openai_compatible", row());
    expect(spec.request_body_overrides).toEqual({ chat: { enable_thinking: false } });
  });

  it("门按「adapter 是否真消费」开，不是一刀切", () => {
    // openai/azure/custom_http construct OpenAICompatibleAdapter with models (registry.py:101-132).
    for (const protocol of ["openai_compatible", "azure_openai", "custom_http"]) {
      expect(buildProviderModelSpec(protocol, row()).request_body_overrides).toBeTruthy();
    }
    // anthropic uses AnthropicCompatibleAdapter, with no overrides consumer in whole file; enabling creates another false affordance.
    expect("request_body_overrides" in buildProviderModelSpec("anthropic_compatible", row())).toBe(false);
    expect(providerConsumesBodyOverrides("anthropic_compatible")).toBe(false);
  });

  it("白名单而非反向列举——将来新增的 adapter type 默认不消费", () => {
    expect(providerConsumesBodyOverrides("bedrock")).toBe(false);
    expect(providerConsumesBodyOverrides("vertex")).toBe(false);
    expect(providerConsumesBodyOverrides("mock")).toBe(false);
  });

  it("★ 出站参数是另一道门：anthropic 也消费，不能跟 raw JSON 共用判据", () => {
    // 116 dogfood finding: both once shared providerConsumesBodyOverrides, so Anthropic-compatible source
    // could neither render this section nor persist declaration, despite anthropic_compat.py _build_body
    // applying it (apply_outbound_params family="anthropic") and healing rejection.
    for (const protocol of ["openai_compatible", "azure_openai", "custom_http", "anthropic_compatible"]) {
      expect(providerConsumesOutboundParams(protocol)).toBe(true);
    }
    // Also a positive allowlist: new adapters default to nonconsuming.
    expect(providerConsumesOutboundParams("bedrock")).toBe(false);
    expect(providerConsumesOutboundParams("mock")).toBe(false);
  });

  it("★ anthropic 的声明必须真的落库，同时 raw JSON 仍然不落", () => {
    const declared = row({ outboundParams: { temperature: { mode: "fixed", value: 1 } } });
    const spec = buildProviderModelSpec("anthropic_compatible", declared);

    expect(spec.outbound_params).toEqual({ temperature: { mode: "fixed", value: 1 } });
    // Negative control: gates stay independent, never incidentally enable overrides.
    expect("request_body_overrides" in spec).toBe(false);
  });

  it("api_surface：仅 openai_compatible 且值为 responses 才落库（Responses 批 2）", () => {
    // Positive: model declaring responses includes api_surface in payload.
    const spec = buildProviderModelSpec("openai_compatible", row({ apiSurface: "responses" }));
    expect(spec.api_surface).toBe("responses");
    // Omit default-surface key: backend signature includes only nondefaults; no redundant keys.
    expect("api_surface" in buildProviderModelSpec("openai_compatible", row())).toBe(false);
    // Azure deployment paths contradict Responses throughout; omit even when declared.
    expect("api_surface" in buildProviderModelSpec("azure_openai", row({ apiSurface: "responses" }))).toBe(false);
    expect("api_surface" in buildProviderModelSpec("anthropic_compatible", row({ apiSurface: "responses" }))).toBe(false);
  });

  it("空 overrides 不产出键（后端 extra=forbid，别塞空对象）", () => {
    expect("request_body_overrides" in buildProviderModelSpec("openai_compatible", row({ requestBodyOverrides: {} }))).toBe(false);
    expect("request_body_overrides" in buildProviderModelSpec("openai_compatible", row({ requestBodyOverrides: undefined }))).toBe(false);
  });

  it("capability_transports 仍只给 custom_http（这条门没被顺手打开）", () => {
    expect(buildProviderModelSpec("custom_http", row()).capability_transports).toEqual({ chat: "chat" });
    expect("capability_transports" in buildProviderModelSpec("openai_compatible", row())).toBe(false);
  });
});

describe("批 1-d：副驾只在 add 模式提供（tech-design-batch1d §1-6）", () => {
  it("edit 模式不渲染——否则 runtime_key 与自己撞名，repair 轮会把用户的 provider 改名", () => {
    expect(shouldOfferProviderCopilot(true, true, { mode: "edit" })).toBe(false);
  });

  it("只在 add 模式、功能开启且团队已配置模型时提供", () => {
    expect(shouldOfferProviderCopilot(true, true, { mode: "add" })).toBe(true);
    expect(shouldOfferProviderCopilot(false, true, { mode: "add" })).toBe(false);
    expect(shouldOfferProviderCopilot(true, false, { mode: "add" })).toBe(false);
    expect(shouldOfferProviderCopilot(true, true, null)).toBe(false);
  });
});

describe("批 1-d：backendKey 契约（实现层 review P1-2）", () => {
  it("键名与后端 LlmProviderDraftDTO 逐个对齐——拼错就是静默不填", () => {
    // Backend field set enforced by test_config_copilot_llm_provider.py::test_generation_schema_field_names.
    expect(Object.values(PROVIDER_DRAFT_BACKEND_KEYS).sort()).toEqual(
      ["api_version", "auth_type", "base_url", "messages_path", "models", "name", "protocol"],
    );
  });
});

describe("api_surface 实现 review 修复批判据", () => {
  const row = (extra: object = {}) => ({
    id: "gpt-51",
    caps: ["chat"],
    source: "manual" as const,
    ...extra,
  });

  it("★三份落库实现逐一钉住（review R3-P1-5：删任何一份 spread 都必须有测试红）", () => {
    // ① Main connect-save path.
    const connectDraft = {
      mode: "add", name: "gw", protocol: "openai_compatible", baseUrl: "https://gw/v1",
      apiKey: "sk-x", messagesPath: "", models: [row({ apiSurface: "responses" })],
    };
    const draft1 = v2ProviderDraftFromConnect(connectDraft as never);
    expect(draft1.models[0].api_surface).toBe("responses");
    const draft1Default = v2ProviderDraftFromConnect({ ...connectDraft, models: [row()] } as never);
    expect("api_surface" in draft1Default.models[0]).toBe(false);
    // ② Reverification of persisted configuration.
    const spec = {
      type: "openai_compatible", base_url: "https://gw/v1",
      models: [{ id: "gpt-51", capabilities: ["chat"], api_surface: "responses" }],
    };
    const draft2 = v2ProviderDraftFromSavedConfig("gw", "llmp_00000000000000000000000000000000", spec as never);
    expect(draft2.models[0].api_surface).toBe("responses");
    // ③ buildProviderModelSpec already covered by existing tests.
  });

  it("caps 门：embed-only 残留声明不落库（review R4：隐形残留会打 stale embed 证据）", () => {
    const stale = row({ apiSurface: "responses", caps: ["embed"] });
    expect("api_surface" in buildProviderModelSpec("openai_compatible", stale)).toBe(false);
    const connectDraft = {
      mode: "add", name: "gw", protocol: "openai_compatible", baseUrl: "https://gw/v1",
      apiKey: "sk-x", messagesPath: "", models: [stale],
    };
    const draft = v2ProviderDraftFromConnect(connectDraft as never);
    expect(draft.models.length === 0 || !("api_surface" in draft.models[0])).toBe(true);
  });

  it("本地测试徽章签名随面失效（review R4：legacy 测试路签名不含面）", () => {
    const base = providerSignature("openai_compatible" as never, "https://gw/v1", "", [row()], "tok");
    const flipped = providerSignature("openai_compatible" as never, "https://gw/v1", "", [row({ apiSurface: "responses" })], "tok");
    expect(base).not.toBe(flipped);
  });
});


describe("context-engineering 批 4 review 修：模型级 context_window 保存不丢", () => {
  const row = (extra: Record<string, unknown> = {}) => ({
    id: "deepseek-v4-pro",
    caps: ["chat"],
    source: "manual" as const,
    ...extra,
  });

  it("读到的正整数原样带回（配置页不展示，但投影层不能把它吃掉）", () => {
    expect(buildProviderModelSpec("anthropic_compatible", row({ contextWindow: 128000 })).context_window).toBe(128000);
  });

  it("没配、非法值都不出现这个键——「没配」与「配了但为空」在后端校验上必须是同一件事", () => {
    expect("context_window" in buildProviderModelSpec("anthropic_compatible", row())).toBe(false);
    expect("context_window" in buildProviderModelSpec("anthropic_compatible", row({ contextWindow: 0 }))).toBe(false);
    expect("context_window" in buildProviderModelSpec("anthropic_compatible", row({ contextWindow: 1.5 }))).toBe(false);
  });
});
