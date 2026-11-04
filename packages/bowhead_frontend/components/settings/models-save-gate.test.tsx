/** Batch 1-d completion: guard `saveEnabled`'s **use site**.
 *
 * Earlier rounds repeatedly guarded the predicate (isolated `modelRequestBodyOverridesValid` tests passed),
 * without asserting that saveEnabled included it—removing it from saveEnabled
 * left predicate tests green. Render real `SettingsModels`, navigate to a connection draft, and inspect Save directly.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

const llmConfigMocks = vi.hoisted(() => ({
  fetchAdminLlmConfig: vi.fn(),
  saveAdminLlmConfig: vi.fn(),
  validateAdminLlmConfig: vi.fn(),
  verifyDraftProvider: vi.fn(),
  verifyProvider: vi.fn(),
}));
vi.mock("@/lib/llm-config-api", async (original) => ({ ...await original<typeof import("@/lib/llm-config-api")>(), ...llmConfigMocks }));
vi.mock("@/lib/llm-config-v2-api", async (original) => ({
  ...await original<typeof import("@/lib/llm-config-v2-api")>(),
  fetchOutboundAdaptations: vi.fn().mockResolvedValue({ items: [] }),
  fetchActiveVerificationBatches: vi.fn().mockResolvedValue({ batches: [] }),
}));

// 2026-08-18: gating removed; Copilot always enabled—the old premise mocked off here no longer exists.

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(window.location.search) }));

import { SettingsModels } from "@/components/settings/models";
import messagesZh from "@/messages/zh-CN.json";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const CONFIG = {
  schema_version: 2,
  providers: {
    qwen: {
      type: "openai_compatible",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      models: [{ id: "qwen-plus", capabilities: ["chat", "tool"], source: "manual" }],
    },
  },
  tiers: {},
  kb: { models: {}, active: {}, pending_embed: null },
};

async function openEditDraft() {
  window.history.replaceState({}, "", "/team/settings/models/connections/edit/?id=llmp_qwen");
  llmConfigMocks.fetchAdminLlmConfig.mockResolvedValue({ config: CONFIG, version: 2, providers: [], provider_identities: { qwen: "llmp_qwen" }, features: { model_discovery_v2: true } });
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <SettingsModels section="connection" />
    </NextIntlClientProvider>,
  );
  await waitFor(() => expect(document.querySelector(".connect-body")).toBeTruthy());
}

const saveButton = () =>
  [...document.querySelectorAll(".ui-savebar button")].find((b) => b.textContent?.includes("保存")) as HTMLButtonElement | undefined;

describe("connection save validation at the editor", () => {
  it("blocks saving invalid override JSON", async () => {
    await openEditDraft();
    const box = document.querySelector("textarea[name^='provider_model_overrides']") as HTMLTextAreaElement | null;
    expect(box).toBeTruthy();
    const before = saveButton();
    expect(before?.disabled).toBe(false);
    fireEvent.change(box!, { target: { value: "{ invalid JSON" } });
    await waitFor(() => expect(saveButton()?.disabled).toBe(true));
  });
});

it("blocks saving a model with an invalid cached-input multiplier", async () => {
  await openEditDraft();
  const input = document.querySelector("input[name^='provider_model_credit_rate_cached_input']") as HTMLInputElement;
  expect(input).toBeTruthy();
  fireEvent.change(input, { target: { value: "1000.000001" } });
  await waitFor(() => expect(saveButton()?.disabled).toBe(true));
});
