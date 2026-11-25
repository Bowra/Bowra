/** The connection editor reports what Save and verification do, where the administrator acted.
 *
 * Audit and design: docs/sprints/model-settings-save-flow-2026-11-25. Before it, Save read "保存中…" while a
 * verification ran, results showed three times, warnings about other connections forced a Team-wide review, a save
 * ended without saying so, and save errors appeared out of view with advice that lost the draft.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";

const llmConfigMocks = vi.hoisted(() => ({
  fetchAdminLlmConfig: vi.fn(),
  saveAdminLlmConfig: vi.fn(),
  validateAdminLlmConfig: vi.fn(),
  verifyDraftProvider: vi.fn(),
  verifyProvider: vi.fn(),
}));
vi.mock("@/lib/llm-config-api", async (original) => ({ ...await original<typeof import("@/lib/llm-config-api")>(), ...llmConfigMocks }));

const v2Mocks = vi.hoisted(() => ({
  createVerificationBatch: vi.fn(),
  createVerificationPlan: vi.fn(),
  fetchActiveVerificationBatches: vi.fn().mockResolvedValue({ batches: [] }),
  fetchDraftEvidence: vi.fn(),
  fetchOutboundAdaptations: vi.fn().mockResolvedValue({ items: [] }),
  fetchVerificationBatch: vi.fn(),
  precheckV2Provider: vi.fn(),
}));
vi.mock("@/lib/llm-config-v2-api", async (original) => ({ ...await original<typeof import("@/lib/llm-config-v2-api")>(), ...v2Mocks }));

const toastMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/use-toast", async (original) => ({ ...await original<typeof import("@/lib/use-toast")>(), toast: toastMock }));

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(window.location.search) }));

import { SettingsModels } from "@/components/settings/models";
import messagesZh from "@/messages/zh-CN.json";

beforeEach(() => {
  v2Mocks.fetchDraftEvidence.mockResolvedValue({ provider_id: PROVIDER_ID, draft_invalid: false, items: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const PROVIDER_ID = "llmp_qwen";
const CONFIG = {
  schema_version: 2,
  providers: {
    qwen: {
      type: "openai_compatible",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      models: [{ id: "qwen-plus", capabilities: ["chat", "tool"], source: "manual", credit_rates: { input: "1", cached_input: "1", output: "1" } }],
    },
  },
  tiers: {},
  kb: { models: {}, active: {}, pending_embed: null },
};

function response(version: number, entries: Array<{ capability: string; fact_state: string }> = []) {
  return {
    config: CONFIG,
    version,
    providers: [],
    provider_identities: { qwen: PROVIDER_ID },
    features: { model_discovery_v2: true },
    verification: {
      as_of: `2026-11-25T00:00:0${version}Z`,
      entries: entries.map((entry) => ({ provider_id: PROVIDER_ID, model_id: "qwen-plus", attempt_state: null, ...entry })),
    },
  };
}

function readiness(overrides: Record<string, unknown> = {}) {
  return {
    as_of: "2026-11-25", draft_semantic_hash: "a".repeat(64), may_apply: true, rows: [], blocking: [], warnings: [],
    required_acceptances: [], external_rows: [], baseline_will_invalidate: false, migration_acceptance: null, ...overrides,
  };
}

function checked(overrides: Record<string, unknown> = {}) {
  return { valid: true, version: 2, issues: [], has_changes: true, changed_sections: ["providers"], readiness: readiness(overrides) };
}

const ITEMS = ["chat", "tool"].map((capability) => ({
  provider_id: PROVIDER_ID, provider_display_name: "qwen", model_id: "qwen-plus", capability,
  testability: "testable", provider_request_count: 1, testability_contract_version: "v1", probe_execution_policy_version: "v1",
}));

async function openEditor(initial = response(2)) {
  window.history.replaceState({}, "", `/team/settings/models/connections/edit/?id=${PROVIDER_ID}`);
  llmConfigMocks.fetchAdminLlmConfig.mockResolvedValue(initial);
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <SettingsModels section="connection" />
    </NextIntlClientProvider>,
  );
  await waitFor(() => expect(document.querySelector(".connect-body")).toBeTruthy());
}

const bar = () => document.querySelector(".ui-savebar") as HTMLElement;
const barButton = (label: string) =>
  within(bar()).getAllByRole("button").find((button) => button.textContent?.trim() === label) as HTMLButtonElement;
/** Chat, Tool calling and Vision chips of the only model row (Embedding and Rerank follow, unticked). */
const chipText = () => [...document.querySelectorAll(".medit-caps .captoggle")].slice(0, 3).map((chip) => chip.textContent?.replace(/\s+/g, " ").trim());
const editRate = () => fireEvent.change(document.querySelector("input[name^='provider_model_credit_rate_input']") as HTMLInputElement, { target: { value: "2" } });

async function startVerification(finalItems?: Array<{ capability: string; state: string; safe_code?: string }>) {
  v2Mocks.precheckV2Provider.mockResolvedValue({ ok: true, provider_id: PROVIDER_ID, warnings: [], request_previews: [] });
  v2Mocks.createVerificationPlan.mockResolvedValue({
    plan_id: "plan", confirmation_token: "token", expires_at: "2099-01-01", item_count: 2,
    provider_request_count: 2, max_concurrency: 3, notices: [], items: ITEMS,
  });
  v2Mocks.createVerificationBatch.mockResolvedValue({ batch_id: "batch", plan_id: "plan", state: "running", revision: 1 });
  const states = finalItems ?? ITEMS.map((item) => ({ capability: item.capability, state: "verifying" }));
  const done = Boolean(finalItems);
  v2Mocks.fetchVerificationBatch.mockResolvedValue({
    batch_id: "batch", plan_id: "plan", state: done ? "completed" : "running", revision: 2, base_config_version: 2,
    counts: states.reduce<Record<string, number>>((counts, item) => ({ ...counts, [item.state]: (counts[item.state] || 0) + 1 }), {}),
    items: ITEMS.map((item, ordinal) => ({ ...item, item_id: `i${ordinal}`, ordinal, revision: 2, ...states[ordinal] })),
  });
  fireEvent.click(barButton("验证"));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText("确认验证计划")).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认并开始验证" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
}

describe("verification in the connection editor", () => {
  it("shows progress in the save bar and keeps Save reading Save", async () => {
    await openEditor();
    await startVerification();

    await waitFor(() => expect(bar().textContent).toMatch(/正在验证 0\/2/));
    expect(within(bar()).getByRole("button", { name: "停止" })).toBeTruthy();
    expect(barButton("保存")).toBeTruthy();
    expect(bar().textContent).not.toContain("保存中");
    expect(bar().querySelector(".ui-savebar-dot")).toBeNull();
    expect(document.body.textContent).not.toContain("未执行模型能力验证");
    await waitFor(() => expect(chipText()).toEqual(["对话 · 验证中", "工具调用 · 验证中", "视觉理解"]));
  });

  it("puts each result on its chip and a failure's reason under its model", async () => {
    await openEditor();
    await startVerification([
      { capability: "chat", state: "verified" },
      { capability: "tool", state: "failed", safe_code: "tool_call_missing" },
    ]);

    await waitFor(() => expect(chipText()).toEqual(["对话 · 已验证", "工具调用 · 验证失败", "视觉理解"]));
    const failure = document.querySelector(".medit-row .verification-row-failures") as HTMLElement;
    expect(failure.textContent).toContain("工具调用验证失败");
    expect(failure.textContent).toContain("tool_call_missing");
    expect(failure.textContent).toContain("请确认这个模型支持工具调用");
    expect(bar().textContent).toContain("验证完成：1 项通过，1 项未通过（原因见对应模型）");
    expect(within(bar()).getByRole("button", { name: "重试未通过的 1 项" })).toBeTruthy();
    // One place per result: no summary bar stack and no batch list on the page.
    expect(document.querySelector(".connect-body .test-result")).toBeNull();
    expect(document.querySelector(".verification-items")).toBeNull();
  });
});

describe("capability chips follow the server's answer for the draft", () => {
  it("keeps results an edit does not touch and marks changed ones", async () => {
    await openEditor(response(2, [
      { capability: "chat", fact_state: "verified" },
      { capability: "tool", fact_state: "verified" },
    ]));
    // Before the server answers, the saved results show.
    expect(chipText()).toEqual(["对话 · 已验证", "工具调用 · 已验证", "视觉理解"]);

    v2Mocks.fetchDraftEvidence.mockResolvedValue({
      provider_id: PROVIDER_ID, draft_invalid: false,
      items: [
        { model_id: "qwen-plus", capability: "chat", fact_state: "stale", observed_at: null, safe_code: null },
        { model_id: "qwen-plus", capability: "tool", fact_state: "verified", observed_at: "2026-11-20T00:00:00Z", safe_code: "ok" },
        { model_id: "qwen-plus", capability: "vision", fact_state: "unknown", observed_at: null, safe_code: null },
      ],
    });
    fireEvent.click(document.querySelectorAll(".medit-caps .captoggle")[2]);

    await waitFor(() => expect(chipText()).toEqual(["对话 · 需重新验证", "工具调用 · 已验证", "视觉理解 · 未验证"]));
    const draft = v2Mocks.fetchDraftEvidence.mock.calls.at(-1)?.[0];
    expect(draft.models[0].capabilities).toContain("vision");
  });
});

describe("saving a connection", () => {
  it("saves despite warnings about other connections and says so", async () => {
    await openEditor();
    editRate();
    llmConfigMocks.validateAdminLlmConfig.mockResolvedValue(checked({
      warnings: [{ code: "capability_failed", field_path: "tier.fast", role_key: "tier.fast", provider_id: "llmp_other", model_id: "other", capability: "chat", action_code: "reverify" }],
    }));
    llmConfigMocks.saveAdminLlmConfig.mockResolvedValue(response(3));

    fireEvent.click(barButton("保存"));

    await waitFor(() => expect(llmConfigMocks.saveAdminLlmConfig).toHaveBeenCalledOnce());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ kind: "success", text: "已保存 qwen。" }));
  });

  it("says when there was nothing to save", async () => {
    await openEditor();

    fireEvent.click(barButton("保存"));

    await waitFor(() => expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ text: "qwen 没有修改，无需保存；验证结果已单独记录。" })));
    expect(llmConfigMocks.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it("asks only about this save's decisions, in plain names", async () => {
    await openEditor();
    editRate();
    const row = (roleKey: string, capability: string, status: string) => ({
      role_key: roleKey, provider_id: PROVIDER_ID, provider_display_name: "qwen", model_id: "qwen-plus", capability,
      routing_critical: true, declared: true, fact_state: status === "ready" ? "verified" : "failed", status,
    });
    llmConfigMocks.validateAdminLlmConfig.mockResolvedValue(checked({
      may_apply: false,
      rows: [row("tier.primary", "chat", "ready"), row("tier.primary", "tool", "risk_acceptance_required")],
      required_acceptances: [{ acceptance_token: "tok", provider_id: PROVIDER_ID, model_id: "qwen-plus", capability: "tool", role_key: "tier.primary", risk_code: "capability_failed" }],
    }));
    llmConfigMocks.saveAdminLlmConfig.mockResolvedValue(response(3));

    fireEvent.click(barButton("保存"));

    const dialog = (await screen.findByRole("heading", { name: "保存 qwen" })).closest(".review-panel") as HTMLElement;
    expect(dialog.textContent).toContain("qwen-plus 的工具调用能力用于主模型，最近一次验证失败");
    expect(dialog.textContent).not.toMatch(/tier\.primary|变更范围|没有阻塞问题|应用/);
    expect(dialog.querySelector(".readiness-rows")).toBeNull();
    const save = within(dialog).getByRole("button", { name: "保存" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole("checkbox"));
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(llmConfigMocks.saveAdminLlmConfig).toHaveBeenCalledOnce());
    expect(llmConfigMocks.saveAdminLlmConfig.mock.calls[0][3]).toEqual(expect.objectContaining({ acceptance_tokens: ["tok"] }));
    await waitFor(() => expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ text: "已保存 qwen。" })));
  });

  it("reports a version conflict beside Save and keeps the draft through a reload", async () => {
    await openEditor();
    editRate();
    llmConfigMocks.validateAdminLlmConfig.mockResolvedValue(checked());
    llmConfigMocks.saveAdminLlmConfig.mockRejectedValue(new ApiError(409, "conflict", { code: "config_version_conflict" }));
    llmConfigMocks.fetchAdminLlmConfig.mockResolvedValue(response(3));

    fireEvent.click(barButton("保存"));

    const alert = await within(bar()).findByRole("alert");
    expect(alert.textContent).toContain("其他管理员修改了模型配置");
    expect(alert.textContent).not.toContain("刷新页面");
    expect(document.querySelector(".mset > .test-result.err")).toBeNull();

    fireEvent.click(within(bar()).getByRole("button", { name: "载入最新配置（保留我的修改）" }));
    await waitFor(() => expect(within(bar()).queryByRole("alert")).toBeNull());
    expect((document.querySelector("input[name^='provider_model_credit_rate_input']") as HTMLInputElement).value).toBe("2");

    llmConfigMocks.saveAdminLlmConfig.mockResolvedValue(response(4));
    fireEvent.click(barButton("保存"));
    await waitFor(() => expect(llmConfigMocks.validateAdminLlmConfig).toHaveBeenLastCalledWith(expect.anything(), 3));
  });
});
