import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";
import { ApiError } from "@/lib/api-client";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const llmConfigMocks = vi.hoisted(() => ({
  fetchAdminLlmConfig: vi.fn(),
  saveAdminLlmConfig: vi.fn(),
  validateAdminLlmConfig: vi.fn(),
  verifyDraftProvider: vi.fn(),
  verifyProvider: vi.fn(),
}));

vi.mock("@/lib/llm-config-api", () => llmConfigMocks);

const llmConfigV2Mocks = vi.hoisted(() => ({
  cancelVerificationBatch: vi.fn(),
  createVerificationBatch: vi.fn(),
  createVerificationPlan: vi.fn(),
  discoverV2Models: vi.fn(),
  fetchActiveVerificationBatches: vi.fn().mockResolvedValue({ batches: [] }),
  // Outbound-parameter automatic adaptation observations: normally an empty array (keeping the page quiet by default)
  fetchOutboundAdaptations: vi.fn().mockResolvedValue({ items: [] }),
  fetchVerificationBatch: vi.fn(),
  precheckV2Provider: vi.fn(),
}));

vi.mock("@/lib/llm-config-v2-api", () => llmConfigV2Mocks);

import {
  hasBillableImageProbe,
  legacyConnectionTestCapabilities,
  modelCapabilityTestState,
  modelCapabilityTestSummary,
  nextDraftModelCapability,
  projectedVerificationSteps,
  providerDraftMatchesApplied,
  providerSignature,
  ModelSettingsValidationIssueText,
  providerTestAllowsAdd,
  regularProviderRows,
  SettingsModels,
  uniqueCapabilitySteps,
  uniqueFallbackPairs,
  verifiableModelCapabilities,
} from "./models";

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    configConflict:
      "模型配置在草稿打开后已被其他管理员更新，请刷新页面后重新应用修改。",
    blockedUrl: "模型来源地址被安全策略拦截，请填写允许访问的公开地址。",
    reindexFailed:
      "知识库索引重建失败，旧索引仍在继续服务。请检查模型来源配置后重新选择向量模型并应用。",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    configConflict:
      "The model configuration was updated by another administrator while this draft was open. Refresh the page and apply your changes again.",
    blockedUrl:
      "A model source address was blocked by the security policy. Enter an allowed public address.",
    reindexFailed:
      "The knowledge-base index rebuild failed, and the previous index is still serving. Check the model source, then select the vector model again and apply.",
  },
] as const;

function renderLocalized(
  entry: (typeof locales)[number],
  child: React.ReactNode,
) {
  return render(
    <NextIntlClientProvider
      locale={entry.locale}
      messages={entry.messages}
    >
      {child}
    </NextIntlClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("model settings draft invariants", () => {
  it("removes the selected primary and duplicate entries from fallback", () => {
    expect(uniqueFallbackPairs(
      [
        { provider: "deepseek", model: "flash" },
        { provider: "openai", model: "gpt" },
        { provider: "openai", model: "gpt" },
      ],
      { provider: "deepseek", model: "flash" },
    )).toEqual([{ provider: "openai", model: "gpt" }]);
  });

  it("projects only applicable terminal evidence into an untouched editor", () => {
    expect(projectedVerificationSteps([
      {
        provider_id: "llmp_1",
        model_id: "chat-model",
        capability: "chat",
        fact_state: "failed",
      },
      {
        provider_id: "llmp_1",
        model_id: "chat-model",
        capability: "tool",
        fact_state: "verified",
      },
      {
        provider_id: "llmp_1",
        model_id: "old-model",
        capability: "chat",
        fact_state: "stale",
      },
      {
        provider_id: "llmp_other",
        model_id: "other-model",
        capability: "chat",
        fact_state: "verified",
      },
    ], "llmp_1")).toEqual([
      expect.objectContaining({
        model: "chat-model",
        capability: "chat",
        ok: false,
        status: "failed",
      }),
      expect.objectContaining({
        model: "chat-model",
        capability: "tool",
        ok: true,
        status: "passed",
      }),
    ]);
  });

  it("projects evidence only while the provider draft still matches the applied scope", () => {
    const applied = {
      schema_version: 2,
      providers: {
        corp: {
          type: "azure_openai",
          endpoint: "https://models.example.test",
          api_version: "2024-10-21",
          fingerprint: "sha256:saved",
          auth: { type: "bearer", fingerprint: "sha256:saved" },
          models: [{ id: "gpt-51", capabilities: ["chat", "tool"] }],
        },
      },
      tiers: {},
      kb: { models: {}, active: {} },
      media: { models: {}, active: {} },
      search: { models: {}, active: {} },
    } as Parameters<typeof providerDraftMatchesApplied>[0];
    const untouched = structuredClone(applied);
    delete untouched.providers.corp.fingerprint;
    delete (untouched.providers.corp.auth as Record<string, unknown>).fingerprint;

    expect(providerDraftMatchesApplied(untouched, applied, "corp")).toBe(true);

    const changedEndpoint = structuredClone(untouched);
    changedEndpoint.providers.corp.endpoint = "https://new.example.test";
    expect(providerDraftMatchesApplied(changedEndpoint, applied, "corp")).toBe(false);

    const changedSecret = structuredClone(untouched);
    changedSecret.providers.corp.api_key = "new-browser-draft-secret";
    expect(providerDraftMatchesApplied(changedSecret, applied, "corp")).toBe(false);

    const changedMedia = structuredClone(untouched);
    changedMedia.media = {
      active: {},
      models: {
        image: {
          provider: "corp",
          real_id: "image-1",
          protocol: "openai_images",
        },
      },
    };
    expect(providerDraftMatchesApplied(changedMedia, applied, "corp")).toBe(false);
  });

  it("keeps the Deployment placeholder localized", () => {
    expect(messagesEn.settings.models.connect.deployment_default_placeholder)
      .toBe("Defaults to the model ID");
    expect(messagesZh.settings.models.connect.deployment_default_placeholder)
      .toBe("默认与模型 ID 相同");
  });
});

describe.each(locales)("model settings errors — $locale", (entry) => {
  it("localizes load failures without displaying backend prose or code", async () => {
    llmConfigMocks.fetchAdminLlmConfig.mockRejectedValueOnce(
      new ApiError(
        409,
        "RAW_BACKEND_CONFIG_CONFLICT",
        { code: "config_version_conflict" },
      ),
    );

    renderLocalized(entry, <SettingsModels />);

    expect((await screen.findByRole("alert")).textContent).toContain(
      entry.configConflict,
    );
    expect(document.body.textContent).not.toContain(
      "config_version_conflict",
    );
    expect(document.body.textContent).not.toContain("RAW_BACKEND");
    expect(document.body.textContent).not.toContain("HTTP 409");
  });

  it("localizes successful validation issues without displaying raw details", () => {
    renderLocalized(
      entry,
      <ModelSettingsValidationIssueText
        issue={{
          code: "blocked_url",
          path: "providers.internal_proxy.base_url",
          message: "RAW_BACKEND_SSRF_GUARD_DETAIL",
          severity: "error",
        }}
      />,
    );

    expect(screen.getByText(entry.blockedUrl)).toBeTruthy();
    expect(document.body.textContent).not.toContain("blocked_url");
    expect(document.body.textContent).not.toContain(
      "providers.internal_proxy.base_url",
    );
    expect(document.body.textContent).not.toContain("RAW_BACKEND");
  });

  it("shows a localized retryable reindex failure without worker details", async () => {
    llmConfigMocks.fetchAdminLlmConfig.mockResolvedValueOnce({
      version: 4,
      providers: [],
      config: {
        schema_version: 2,
        providers: {
          source: {
            type: "openai_compatible",
            base_url: "https://models.example.com/v1",
            models: [
              {
                id: "embed-1",
                capabilities: ["embed"],
              },
            ],
          },
        },
        tiers: {},
        kb: {
          models: {
            emb__source__embed_1: {
              provider: "source",
              real_id: "embed-1",
              capability: "embed",
              dim: 1536,
            },
          },
          active: {
            embed: "emb__source__embed_1",
          },
          pending_embed: {
            state: "failed",
            error_code: "reindex_failed",
            error: "RAW_WORKER_EXCEPTION",
          },
        },
      },
    });

    renderLocalized(entry, <SettingsModels />);

    const failures = await screen.findAllByText(entry.reindexFailed);
    expect(failures.length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toContain("reindex_failed");
    expect(document.body.textContent).not.toContain("RAW_WORKER_EXCEPTION");
  });
});

describe("model settings V2 provider identity", () => {
  it("plans verification for active assignments without returning the saved secret", async () => {
    const providerId = "llmp_0123456789abcdef0123456789abcdef";
    const activeConfig = {
      schema_version: 2,
      providers: {
        corp_azure: {
          type: "azure_openai",
          endpoint: "https://models.example.test",
          api_version: "2024-10-21",
          fingerprint: "sha256:safe-fingerprint",
          auth: { type: "bearer" },
          models: [{
            id: "gpt-51",
            deployment: "gpt-51",
            capabilities: ["chat", "tool"],
            source: "manual",
          }],
        },
      },
      tiers: { primary: { provider: "corp_azure", model: "gpt-51" } },
      kb: { models: {}, active: {}, pending_embed: null },
      media: { models: {}, active: {} },
      search: { models: {}, active: {} },
    };
    llmConfigMocks.fetchAdminLlmConfig.mockResolvedValue({
      version: 12,
      providers: [],
      config: activeConfig,
      features: {
        model_discovery_v2: true,
        capability_probes: true,
        azure_openai: true,
        custom_http: true,
      },
      provider_identities: { corp_azure: providerId },
    });
    llmConfigMocks.validateAdminLlmConfig.mockResolvedValue({
      valid: true,
      version: 12,
      issues: [],
      has_changes: false,
      changed_sections: [],
      readiness: {
        as_of: "2026-07-31T00:00:00Z",
        draft_semantic_hash: "a".repeat(64),
        may_apply: false,
        rows: [{
          role_key: "tier.primary",
          provider_id: providerId,
          provider_display_name: "corp_azure",
          model_id: "gpt-51",
          capability: "chat",
          routing_critical: true,
          declared: true,
          fact_state: "unknown",
          status: "blocked",
          changed: false,
        }],
        blocking: [],
        warnings: [],
        required_acceptances: [],
        external_rows: [],
        baseline_will_invalidate: false,
      },
    });
    llmConfigV2Mocks.createVerificationPlan.mockResolvedValueOnce({
      plan_id: "lvplan_0123456789abcdef0123456789abcdef",
      confirmation_token: "signed-confirmation-token",
      expires_at: "2026-07-31T00:15:00Z",
      item_count: 1,
      provider_request_count: 1,
      max_concurrency: 1,
      notices: [],
      items: [{
        provider_id: providerId,
        provider_display_name: "corp_azure",
        model_id: "gpt-51",
        capability: "chat",
        testability: "testable",
        provider_request_count: 1,
        testability_contract_version: "azure-chat-v1",
        probe_execution_policy_version: "probe-policy-v1",
      }],
    });
    renderLocalized(locales[1], <SettingsModels section="assignments" />);
    const verify = await screen.findByRole("button", {
      name: "Verify current assignments",
    });
    // The scoped assignments page re-validates the draft after a 250 ms debounce, which
    // disables the button again; wait for that second readiness check before clicking.
    await waitFor(() => expect(llmConfigMocks.validateAdminLlmConfig).toHaveBeenCalledTimes(2));
    await waitFor(() => expect((verify as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(verify);
    expect(await screen.findByRole("heading", {
      name: "Choose model capabilities to verify",
    })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", {
      name: "Create verification plan",
    }));

    await waitFor(() => {
      expect(llmConfigV2Mocks.createVerificationPlan).toHaveBeenCalledOnce();
    });
    const request = llmConfigV2Mocks.createVerificationPlan.mock.calls[0][0];
    expect(request).toEqual(expect.objectContaining({
      base_config_version: 12,
      items: [{ provider_id: providerId, model_id: "gpt-51", capability: "chat" }],
      provider_drafts: [expect.objectContaining({
        provider_id: providerId,
        runtime_key: "corp_azure",
        type: "azure_openai",
        endpoint: "https://models.example.test",
        api_version: "2024-10-21",
      })],
    }));
    expect(JSON.stringify(request)).not.toContain("api_key");
    expect(await screen.findByText("1 model capability check(s) planned")).toBeTruthy();
  });

  it("reserves a stable identity when a new source is saved without a probe", async () => {
    llmConfigMocks.fetchAdminLlmConfig.mockResolvedValue({
      version: 4,
      providers: [],
      config: {
        schema_version: 2,
        providers: {},
        tiers: {},
        kb: { models: {}, active: {}, pending_embed: null },
        media: { models: {}, active: {} },
        search: { models: {}, active: {} },
      },
      features: {
        model_discovery_v2: true,
        capability_probes: true,
        azure_openai: true,
        custom_http: true,
      },
      provider_identities: {},
      verification: { as_of: "2026-07-31T00:00:00Z", entries: [] },
    });
    llmConfigV2Mocks.precheckV2Provider.mockResolvedValueOnce({
      ok: true,
      provider_id: "llmp_0123456789abcdef0123456789abcdef",
      discovery: { supported: true },
      request_previews: [],
      warnings: [],
    });
    llmConfigMocks.validateAdminLlmConfig.mockResolvedValue({
      valid: true,
      version: 4,
      issues: [],
      has_changes: true,
      changed_sections: ["providers"],
      readiness: {
        as_of: "2026-07-31T00:00:00Z",
        draft_semantic_hash: "a".repeat(64),
        may_apply: true,
        rows: [],
        blocking: [],
        warnings: [],
        required_acceptances: [],
        external_rows: [],
        baseline_will_invalidate: false,
      },
    });
    llmConfigMocks.saveAdminLlmConfig.mockImplementationOnce(async (config) => ({
      version: 5,
      providers: [],
      config,
      provider_identities: { corp_openai: "llmp_0123456789abcdef0123456789abcdef" },
    }));

    const view = renderLocalized(locales[1], <SettingsModels section="connection" newConnection />);
    await waitFor(() => expect(view.container.querySelector("#mset-provider-name")).toBeTruthy());
    fireEvent.change(view.container.querySelector("#mset-provider-name")!, {
      target: { value: "corp_openai" },
    });
    fireEvent.change(view.container.querySelector("#mset-provider-base-url")!, {
      target: { value: "https://models.example.com/v1" },
    });
    fireEvent.change(view.container.querySelector("#mset-provider-api-key")!, {
      target: { value: "test-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add a model" }));
    fireEvent.change(view.container.querySelector(
      'input[name="provider_model_0"]',
    )!, {
      target: { value: "gpt-enterprise" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(llmConfigV2Mocks.precheckV2Provider).toHaveBeenCalledOnce();
    });
    expect(llmConfigV2Mocks.precheckV2Provider).toHaveBeenCalledWith(
      expect.objectContaining({
        runtime_key: "corp_openai",
        models: [
          expect.objectContaining({
            id: "gpt-enterprise",
            capabilities: ["chat"],
          }),
        ],
      }),
    );
    expect(llmConfigMocks.validateAdminLlmConfig).toHaveBeenCalled();
    await waitFor(() => expect(llmConfigMocks.saveAdminLlmConfig).toHaveBeenCalledOnce());
    expect(llmConfigMocks.saveAdminLlmConfig.mock.calls[0][0].providers.corp_openai).toBeTruthy();
  });
});

describe("model capability test status", () => {
  const steps = [
    { step: "connection" as const, ok: true, code: "ok" as const },
    { step: "capability" as const, ok: true, code: "ok" as const, model: "chat-1", capability: "chat" },
    { step: "capability" as const, ok: false, code: "model_not_found" as const, model: "chat-1", capability: "tool" },
  ];

  it("projects every capability result to its corresponding model label", () => {
    expect(modelCapabilityTestState("chat-1", ["chat", "tool"], "chat", false, steps)).toBe("verified");
    expect(modelCapabilityTestState("chat-1", ["chat", "tool"], "tool", false, steps)).toBe("failed");
    expect(modelCapabilityTestState("chat-1", ["chat", "tool"], "vision", false, steps)).toBe("off");
    expect(modelCapabilityTestState("chat-1", ["chat"], "chat", true, steps)).toBe("verified");
    expect(modelCapabilityTestState("chat-1", ["vision"], "vision", true, steps)).toBe("testing");
  });

  it("does not report a failed connection as verified capabilities", () => {
    expect(modelCapabilityTestSummary({ ok: true, code: "ok", steps })).toEqual({
      state: "capability_failed", failed: 1, degraded: 0, total: 2,
    });
    expect(modelCapabilityTestSummary({
      ok: false, code: "unauthorized",
      steps: [{ step: "connection", ok: false, code: "unauthorized" }],
    })).toEqual({ state: "connection_failed", failed: 0, degraded: 0, total: 0 });
  });

  it("keeps a successful connection summary when a capability fails", () => {
    expect(modelCapabilityTestSummary({ ok: false, code: "capability_failed", steps })).toEqual({
      state: "capability_failed", failed: 1, degraded: 0, total: 2,
    });
  });

  it("keeps streaming-only failures visible as degraded while allowing save", () => {
    const degradedSteps = [
      {
        step: "capability" as const,
        ok: false,
        code: "stream_degraded" as const,
        status: "degraded" as const,
        phase: "stream" as const,
        model: "chat-1",
        capability: "chat",
      },
    ];
    expect(modelCapabilityTestState("chat-1", ["chat"], "chat", false, degradedSteps)).toBe("degraded");
    expect(modelCapabilityTestSummary({
      ok: false,
      code: "capability_degraded",
      steps: degradedSteps,
    })).toEqual({
      state: "capability_degraded",
      failed: 0,
      degraded: 1,
      total: 1,
    });
    expect(providerTestAllowsAdd({ ok: false, code: "capability_degraded" })).toBe(true);
    expect(providerTestAllowsAdd({ ok: false, code: "capability_failed" })).toBe(false);
  });

  it("keeps only the latest result for each model capability", () => {
    expect(uniqueCapabilitySteps([
      ...steps,
      { step: "capability", ok: false, code: "invalid_response", model: "chat-1", capability: "chat" },
    ])).toEqual([
      { step: "capability", ok: false, code: "invalid_response", model: "chat-1", capability: "chat" },
      { step: "capability", ok: false, code: "model_not_found", model: "chat-1", capability: "tool" },
    ]);
  });

  it("invalidates evidence when a capability or its protocol changes", () => {
    const common = [
      "openai_compatible",
      "https://models.example.com/v1",
      "",
    ] as const;
    const search = providerSignature(
      ...common,
      [{
        id: "qwen-plus",
        caps: ["web_search"],
        protocol: "dashscope_compat",
      }],
      "saved-secret",
    );

    expect(providerSignature(
      ...common,
      [{
        id: "qwen-plus",
        caps: ["web_search"],
        protocol: "dashscope_native",
      }],
      "saved-secret",
    )).not.toBe(search);
    expect(providerSignature(
      ...common,
      [{ id: "qwen-plus", caps: ["chat"] }],
      "saved-secret",
    )).not.toBe(search);
  });

  it("keeps web search additive for conversational models and image exclusive", () => {
    expect(nextDraftModelCapability(
      { caps: ["chat", "tool"] },
      "web_search",
    )).toEqual({
      caps: ["chat", "tool", "web_search"],
      protocol: "dashscope_compat",
    });
    expect(nextDraftModelCapability(
      { caps: ["web_search"], protocol: "dashscope_compat" },
      "chat",
    )).toEqual({
      caps: ["web_search", "chat"],
      protocol: "dashscope_compat",
    });
    expect(nextDraftModelCapability(
      { caps: ["chat", "web_search"], protocol: "dashscope_compat" },
      "image_gen",
    )).toEqual({
      caps: ["image_gen"],
      protocol: "dashscope_async",
    });
  });

  it("assigns and preserves the rerank protocol", () => {
    expect(nextDraftModelCapability(
      { caps: ["chat"] },
      "rerank",
    )).toEqual({
      caps: ["chat", "rerank"],
      protocol: "openai_compat",
    });
    expect(nextDraftModelCapability(
      { caps: ["rerank"], protocol: "dashscope_native" },
      "chat",
    )).toEqual({
      caps: ["rerank", "chat"],
      protocol: "dashscope_native",
    });
  });

  it("keeps embedding models out of chat capabilities", () => {
    expect(nextDraftModelCapability(
      { caps: ["chat", "tool"] },
      "embed",
    )).toEqual({
      caps: ["embed"],
      protocol: undefined,
    });
    expect(nextDraftModelCapability(
      { caps: ["embed"] },
      "chat",
    )).toEqual({
      caps: ["chat"],
      protocol: undefined,
    });
  });

  it("does not submit legacy thinking hints as standalone capability probes", () => {
    expect(verifiableModelCapabilities([
      "chat",
      "tool",
      "think",
    ])).toEqual([
      "chat",
      "tool",
    ]);
  });

  it("keeps special probes in the legacy connection test", () => {
    expect(legacyConnectionTestCapabilities([
      "chat",
      "think",
      "image_gen",
      "web_search",
    ])).toEqual([
      "chat",
      "image_gen",
      "web_search",
    ]);
  });

  it("defensively preserves ordinary capabilities from a mixed legacy row", () => {
    expect(regularProviderRows([
      {
        id: "qwen-plus",
        caps: ["chat", "web_search"],
        source: "manual" as const,
        protocol: "dashscope_compat",
      },
    ])).toEqual([
      {
        id: "qwen-plus",
        caps: ["chat"],
        source: "manual",
        protocol: "dashscope_compat",
      },
    ]);
  });

  it("identifies tests that submit a billable image request", () => {
    expect(hasBillableImageProbe([
      { id: "qwen-image-plus", caps: ["image_gen"] },
    ])).toBe(true);
    expect(hasBillableImageProbe([
      { id: "qwen-plus", caps: ["web_search"] },
    ])).toBe(false);
  });
});
