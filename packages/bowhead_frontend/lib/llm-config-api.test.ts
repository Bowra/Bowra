import { afterEach, describe, expect, it, vi } from "vitest";

import { headersExactly } from "@/__tests__/request-headers";
import {
  fetchAdminLlmConfig,
  fetchLlmCatalog,
  saveAdminLlmConfig,
  validateAdminLlmConfig,
  verifyDraftProvider,
  verifyDraftProviderCapability,
} from "./llm-config-api";

const ORIGINAL_FETCH = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

function jsonResp(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("llm-config-api", () => {
  it("fetches the team catalog with credentials", async () => {
    const catalog = {
      version: 3,
      providers: [
        {
          id: "openai_main",
          display_name: "openai_main",
          type: "openai_compatible",
          models: ["gpt-4.1-mini"],
        },
      ],
      default_model: { provider: "openai_main", model: "gpt-4.1-mini" },
    };
    const fetchSpy = vi.fn(async () => jsonResp(200, catalog));
    globalThis.fetch = fetchSpy as typeof fetch;

    await expect(fetchLlmCatalog()).resolves.toEqual(catalog);
    // appFetch always sends a Headers instance carrying the public base (d59ce0c12).
    expect(fetchSpy).toHaveBeenCalledWith("/api/v1/llm/catalog", {
      credentials: "include",
      headers: headersExactly({}),
    });
  });

  it("saves the admin config with the explicit reindex confirmation flag", async () => {
    const resp = {
      version: 4,
      config: { schema_version: 2, providers: {}, agents: {}, kb: { models: {}, active: {} } },
      providers: [],
      reindex: { state: "pending" },
    };
    const fetchSpy = vi.fn(async () => jsonResp(200, resp));
    globalThis.fetch = fetchSpy as typeof fetch;
    const config = {
      schema_version: 2,
      providers: { p1: { type: "openai_compatible", api_key: "sk-test" } },
      agents: {},
      kb: { models: {}, active: {} },
    };

    await expect(saveAdminLlmConfig(config, true, 3)).resolves.toEqual(resp);
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/admin/llm-config",
      expect.objectContaining({
        credentials: "include",
        method: "PUT",
        body: JSON.stringify({
          config,
          confirm_embed_reindex: true,
          expected_version: 3,
        }),
      }),
    );
    const init = (fetchSpy.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
  });

  it("surfaces structured API error messages", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResp(400, { detail: { code: "missing_provider", message: "provider required" } }),
    ) as typeof fetch;

    await expect(fetchAdminLlmConfig()).rejects.toThrow("provider required");
  });

  it("validates the full draft against the loaded version", async () => {
    const result = {
      valid: true,
      version: 7,
      issues: [],
      has_changes: true,
      changed_sections: ["tiers"],
      impact: {},
    };
    const fetchSpy = vi.fn(async () => jsonResp(200, result));
    globalThis.fetch = fetchSpy as typeof fetch;
    const config = { schema_version: 2, providers: {}, tiers: {}, kb: {} };

    await expect(validateAdminLlmConfig(config, 7)).resolves.toEqual(result);
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/admin/llm-config/validate",
      expect.objectContaining({
        credentials: "include",
        method: "POST",
        body: JSON.stringify({ config, expected_version: 7 }),
      }),
    );
  });

  it("tests edited provider fields without sending an unchanged secret", async () => {
    const fetchSpy = vi.fn(async () => jsonResp(200, { ok: true, code: "ok" }));
    globalThis.fetch = fetchSpy as typeof fetch;

    await verifyDraftProvider("our/proxy", {
      provider_type: "openai_compatible",
      base_url: "https://candidate.example.com/v1",
      model: "gpt-4.1-mini",
    });

    // Since 577cfe8f5, provider names use request-body `provider_name` (names may contain `/`, so no longer interpolate them into paths).
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/admin/llm-config/providers/verify-draft",
      expect.objectContaining({
        credentials: "include",
        method: "POST",
        body: JSON.stringify({
          provider_type: "openai_compatible",
          base_url: "https://candidate.example.com/v1",
          model: "gpt-4.1-mini",
          provider_name: "our/proxy",
        }),
      }),
    );
  });

  it("probes each draft capability through its own request", async () => {
    const fetchSpy = vi.fn(async () => jsonResp(200, { ok: true, code: "ok" }));
    globalThis.fetch = fetchSpy as typeof fetch;

    await verifyDraftProviderCapability("our/proxy", {
      provider_type: "openai_compatible",
      base_url: "https://candidate.example.com/v1",
      model: "gpt-4.1-mini",
      capability: "tool",
      capability_protocol: "dashscope_compat",
    });

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/admin/llm-config/providers/verify-draft-capability",
      expect.objectContaining({
        credentials: "include",
        method: "POST",
        body: JSON.stringify({
          provider_type: "openai_compatible",
          base_url: "https://candidate.example.com/v1",
          model: "gpt-4.1-mini",
          capability: "tool",
          capability_protocol: "dashscope_compat",
          provider_name: "our/proxy",
        }),
      }),
    );
  });

  it("surfaces string detail API errors", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResp(403, { detail: "team admin required" }),
    ) as typeof fetch;

    await expect(fetchAdminLlmConfig()).rejects.toThrow("team admin required");
  });
});
