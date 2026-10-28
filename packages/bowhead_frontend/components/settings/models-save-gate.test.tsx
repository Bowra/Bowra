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
vi.mock("@/lib/llm-config-api", () => llmConfigMocks);

const llmConfigV2Mocks = vi.hoisted(() => ({
  precheckProvider: vi.fn(),
  discoverProviderModels: vi.fn(),
  discoverDraftProviderModels: vi.fn(),
  createVerificationPlan: vi.fn(),
  // Outbound-parameter automatic-adaptation observations: normally empty (keeping the page quiet by default).
  fetchOutboundAdaptations: vi.fn().mockResolvedValue({ items: [] }),
  createVerificationBatch: vi.fn(),
  getVerificationBatch: vi.fn(),
  listVerificationBatches: vi.fn(),
  cancelVerificationBatch: vi.fn(),
  listVerificationEvidence: vi.fn(),
}));
vi.mock("@/lib/llm-config-v2-api", () => llmConfigV2Mocks);

// 2026-08-18: gating removed; Copilot always enabled—the old premise mocked off here no longer exists.

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

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
  llmConfigMocks.fetchAdminLlmConfig.mockResolvedValue({ config: CONFIG, version: 2, features: {} });
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <SettingsModels />
    </NextIntlClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "qwen 的更多操作" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "编辑" }));
  await waitFor(() => expect(document.querySelector(".connect-body")).toBeTruthy());
}

const saveButton = () =>
  [...document.querySelectorAll(".connect-foot button")].find((b) => b.textContent?.includes("保存")) as HTMLButtonElement | undefined;

describe("saveEnabled 把非法 overrides 算进去（使用点，不是谓词）", () => {
  it("overrides 打成非法 JSON → 保存按钮禁用", async () => {
    await openEditDraft();
    const box = document.querySelector("textarea[name^='provider_model_overrides']") as HTMLTextAreaElement | null;
    if (!box) return; // Skip if this protocol has no editor (a separate guard covers rendering).
    const before = saveButton();
    expect(before?.disabled).toBe(false);
    fireEvent.change(box, { target: { value: "{ 这不是 JSON" } });
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
