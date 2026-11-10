import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as bowraApi from "@/lib/bowra-connect-api";
import { SettingsModels } from "@/components/settings/models";
import * as configApi from "@/lib/llm-config-api";
import * as verificationApi from "@/lib/llm-config-v2-api";
import { ApiError } from "@/lib/api-client";
import messages from "@/messages/zh-CN.json";

vi.mock("@/lib/bowra-connect-api", () => ({ claimBowraImport: vi.fn(), startBowraConnection: vi.fn() }));

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(window.location.search) }));
vi.mock("@/lib/config-copilot-lite", () => ({ refreshLiteState: vi.fn() }));

const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => navigation,
  BEFORE_APP_NAVIGATION_EVENT: "bowhead:before-app-navigation",
}));
vi.mock("@/lib/llm-config-api", async (original) => ({
  ...(await original<typeof configApi>()),
  fetchAdminLlmConfig: vi.fn(),
  fetchModelConfigurationProposal: vi.fn(),
  reconcileVerifiedAssignments: vi.fn(),
  validateAdminLlmConfig: vi.fn(),
  saveAdminLlmConfig: vi.fn(),
}));
vi.mock("@/lib/llm-config-v2-api", async (original) => ({
  ...(await original<typeof verificationApi>()),
  fetchActiveVerificationBatches: vi.fn(),
  fetchOutboundAdaptations: vi.fn(),
  precheckV2Provider: vi.fn(),
  createVerificationPlan: vi.fn(),
  createVerificationBatch: vi.fn(),
  fetchVerificationBatch: vi.fn(),
}));

const config = {
  schema_version: 2,
  providers: {
    corp: {
      type: "openai_compatible",
      base_url: "https://models.example.test/v1",
      models: [{ id: "model-a", capabilities: ["chat", "tool"] }],
    },
  },
  tiers: {
    primary: { provider: "corp", model: "model-a" },
    fast: { follow: "primary" },
    judge: { auto: true },
  },
  kb: { models: {}, active: {} },
  media: {
    models: {
      image: {
        provider: "corp",
        real_id: "image-a",
        capability: "image_gen",
        protocol: "ark_sync",
      },
    },
    active: { image_gen: "image" },
  },
  search: {
    models: {
      search: {
        provider: "corp",
        real_id: "search-a",
        capability: "web_search",
        protocol: "dashscope_native",
      },
    },
    active: { web_search: "search" },
  },
};
const response = {
  version: 7,
  config,
  providers: [],
  provider_identities: { corp: "llmp_corp" },
  features: { model_discovery_v2: true },
  verification: { as_of: "2026-09-17", entries: [] },
};
const readiness = {
  as_of: "2026-09-17",
  draft_semantic_hash: "hash",
  may_apply: true,
  rows: ["chat", "tool"].map((capability) => ({
    role_key: "tier.primary",
    provider_id: "llmp_corp",
    provider_display_name: "corp",
    model_id: "model-a",
    capability,
    routing_critical: true,
    declared: true,
    fact_state: "unknown",
    status: "warning",
    changed: false,
  })),
  blocking: [],
  warnings: [],
  required_acceptances: [],
  external_rows: [],
  baseline_will_invalidate: false,
};
const plan = {
  plan_id: "plan",
  confirmation_token: "token",
  expires_at: "2099-01-01",
  item_count: 2,
  provider_request_count: 3,
  max_concurrency: 2,
  notices: [],
  items: ["chat", "tool"].map((capability) => ({
    provider_id: "llmp_corp",
    provider_display_name: "corp",
    model_id: "model-a",
    capability,
    testability: "testable",
    provider_request_count: 1,
    testability_contract_version: "v1",
    probe_execution_policy_version: "v1",
  })),
} as verificationApi.VerificationPlan;

function show(
  section:
    "overview" | "connection" | "assignments" | "index" | "external" | "image" | "search",
) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <SettingsModels section={section} />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(
    {},
    "",
    "/team/settings/models/connections/edit/?id=llmp_corp",
  );
  vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(
    structuredClone(response),
  );
  vi.mocked(configApi.validateAdminLlmConfig).mockResolvedValue({
    valid: true,
    version: 7,
    issues: [],
    has_changes: true,
    changed_sections: ["providers"],
    readiness,
  });
  vi.mocked(configApi.reconcileVerifiedAssignments).mockResolvedValue(structuredClone(response));
  vi.mocked(configApi.saveAdminLlmConfig).mockResolvedValue({
    ...structuredClone(response),
    version: 8,
  });
  vi.mocked(verificationApi.fetchActiveVerificationBatches).mockResolvedValue({
    batches: [],
  });
  vi.mocked(verificationApi.fetchOutboundAdaptations).mockResolvedValue({
    items: [],
  });
  vi.mocked(verificationApi.precheckV2Provider).mockImplementation(async (draft) => ({
    provider_id: `llmp_${draft.runtime_key}`,
  }) as verificationApi.ProviderPrecheckResult);
  vi.mocked(verificationApi.createVerificationPlan).mockResolvedValue(plan);
  vi.mocked(verificationApi.createVerificationBatch).mockResolvedValue({
    batch_id: "batch",
    plan_id: "plan",
    state: "completed",
    revision: 2,
    replayed: false,
  });
  vi.mocked(verificationApi.fetchVerificationBatch).mockResolvedValue({
    batch_id: "batch",
    plan_id: "plan",
    state: "completed",
    revision: 2,
    base_config_version: 7,
    counts: { verified: 2 },
    items: plan.items.map((item, ordinal) => ({
      ...item,
      item_id: String(ordinal),
      ordinal,
      revision: 2,
      state: "verified",
    })),
  });
});
afterEach(cleanup);

// Mutation check: removing the batch-to-catalog projection makes this test fail.
describe("scoped model settings", () => {
  it("returns directly to the parent assignment tab even without prior overview navigation", async () => {
    show("assignments");
    fireEvent.click(await screen.findByRole("button", { name: "返回概览" }));
    expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/#assignments");
    cleanup();
    window.history.replaceState({}, "", "/team/settings/models/#assignments");
    show("overview");
    const tab = await screen.findByRole("tab", { name: "模型分工" });
    await waitFor(() => expect(tab.getAttribute("aria-selected")).toBe("true"));
    expect(screen.getByRole("button", { name: "编辑分工" })).toBeTruthy();
    window.history.replaceState({}, "", "/team/settings/models/#mset-overview-evidence");
    fireEvent(window, new HashChangeEvent("hashchange"));
    expect(tab.getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: "模型连接" }));
    expect(navigation.replace).toHaveBeenLastCalledWith("/team/settings/models/#connections", { scroll: false });
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it("keeps the KB count automatic until edited and includes an explicit edit in review", async () => {
    show("assignments");
    fireEvent.click(await screen.findByText("检索返回数量", { selector: "summary" }));
    const count = await screen.findByRole("combobox", { name: "检索返回数量" });
    expect((count as HTMLSelectElement).value).toBe("15");
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
    fireEvent.change(count, { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "检查并应用" }));
    await waitFor(() => expect(configApi.validateAdminLlmConfig).toHaveBeenCalled());
    const reviewed = vi.mocked(configApi.validateAdminLlmConfig).mock.calls.at(-1)![0];
    expect(reviewed).toMatchObject({ kb: { retrieval_top_k: 30, active: {} } });
  });

  it("shows effective selections by task and expands verification without calling the provider", async () => {
    const view = show("overview");
    fireEvent.click(await screen.findByRole("tab", { name: "模型分工" }));
    const rows = view.container.querySelectorAll(".assignment-summary-row");
    const fast = Array.from(rows).find((row) => within(row as HTMLElement).queryByText("轻量模型")) as HTMLElement;
    const judge = Array.from(rows).find((row) => within(row as HTMLElement).queryByText("评估模型")) as HTMLElement;
    expect(within(fast).getByText("corp · model-a")).toBeTruthy();
    expect(within(fast).getByText("跟随主模型")).toBeTruthy();
    expect(within(judge).getByText("corp · model-a")).toBeTruthy();
    expect(within(judge).getByText("自动选择")).toBeTruthy();
    const toggle = screen.getByRole("button", { name: "展开验证详情" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: "收起验证详情" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getAllByRole("button", { name: messages.settings.models.overview.verify_one_label.replace("{model}", "model-a") }).length).toBeGreaterThan(0);
    expect(verificationApi.createVerificationPlan).not.toHaveBeenCalled();
    expect(verificationApi.createVerificationBatch).not.toHaveBeenCalled();
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it("keeps saved assignments visible when verification has no applicable records", async () => {
    vi.mocked(configApi.validateAdminLlmConfig).mockResolvedValue({
      valid: true, version: 7, issues: [], has_changes: false, changed_sections: [], readiness: { ...readiness, rows: [] },
    });
    show("overview");
    fireEvent.click(await screen.findByRole("tab", { name: "模型分工" }));
    fireEvent.click(screen.getByRole("button", { name: "展开验证详情" }));
    expect(await screen.findByText(messages.settings.models.overview.no_records_title)).toBeTruthy();
    expect(screen.queryByText(messages.settings.models.overview.no_assignments_title)).toBeNull();
    expect(screen.getAllByText("corp · model-a").length).toBeGreaterThan(0);
  });

  it("matches summary evidence to connection identity even when display names and model IDs coincide", async () => {
    const saved = structuredClone(response);
    saved.config.providers = {
      corp: { ...config.providers.corp, display_name: "Shared" },
      other: { ...config.providers.corp, display_name: "Shared" },
    } as typeof config.providers;
    saved.provider_identities = { corp: "llmp_corp", other: "llmp_other" } as typeof saved.provider_identities;
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(saved);
    vi.mocked(configApi.validateAdminLlmConfig).mockResolvedValue({
      valid: true, version: 7, issues: [], has_changes: false, changed_sections: [],
      readiness: { ...readiness, rows: [
        { ...readiness.rows[0], provider_display_name: "Shared", fact_state: "verified" },
        { ...readiness.rows[0], provider_id: "llmp_other", provider_display_name: "Shared", fact_state: "failed" },
      ] },
    });
    const view = show("overview");
    fireEvent.click(await screen.findByRole("tab", { name: "模型分工" }));
    const primary = view.container.querySelector(".assignment-summary-row") as HTMLElement;
    await waitFor(() => expect(within(primary).getByText(messages.settings.models.overview.fact_verified)).toBeTruthy());
    expect(within(primary).queryByText(messages.settings.models.overview.fact_failed)).toBeNull();
  });

  it("reviews switching between connections with the same display name and model ID", async () => {
    const saved = structuredClone(response);
    saved.config.providers = {
      corp: { ...config.providers.corp, display_name: "Shared" },
      other: { ...config.providers.corp, display_name: "Shared" },
    } as typeof config.providers;
    saved.provider_identities = { corp: "llmp_corp", other: "llmp_other" } as typeof saved.provider_identities;
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(saved);
    show("assignments");
    fireEvent.change(await screen.findByRole("combobox", { name: "主模型" }), { target: { value: "other::model-a" } });
    fireEvent.click(screen.getByRole("button", { name: "检查并应用" }));
    const changes = await screen.findByRole("heading", { name: "分工修改" });
    const primary = Array.from(changes.parentElement!.querySelectorAll<HTMLElement>(".assignment-review-row"))
      .find((row) => within(row).queryByText("主模型"))!;
    expect(primary).toBeTruthy();
    expect(within(primary).getByText(/Shared \(corp\) · model-a/)).toBeTruthy();
    expect(within(primary).getByText(/Shared \(other\) · model-a/)).toBeTruthy();
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it("retries failed draft readiness without discarding selections or saving assignments", async () => {
    show("assignments");
    await waitFor(() => expect(configApi.validateAdminLlmConfig).toHaveBeenCalled());
    vi.mocked(configApi.validateAdminLlmConfig).mockRejectedValueOnce(new Error("offline"));
    fireEvent.click(screen.getByRole("button", { name: /进阶：/ }));
    fireEvent.change(screen.getByRole("combobox", { name: "轻量模型" }), { target: { value: "corp::model-a" } });
    const retry = await screen.findByRole("button", { name: messages.settings.models.overview.retry });
    expect(retry.closest('[role="alert"]')).toBeTruthy();
    expect((screen.getByRole("button", { name: "验证草稿分工" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(retry);
    await waitFor(() => expect((screen.getByRole("button", { name: "验证草稿分工" }) as HTMLButtonElement).disabled).toBe(false));
    expect((screen.getByRole("combobox", { name: "轻量模型" }) as HTMLSelectElement).value).toBe("corp::model-a");
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it("verifies newly assigned models before applying while preserving the draft", async () => {
    const saved = structuredClone(response);
    saved.config.providers.corp.models.push({ id: "model-b", capabilities: ["chat", "tool"] });
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(saved);
    vi.mocked(configApi.validateAdminLlmConfig).mockImplementation(async (draft) => ({
      valid: true, version: 7, issues: [], has_changes: true, changed_sections: ["tiers"],
      readiness: { ...readiness, rows: [
        ...readiness.rows,
        { ...readiness.rows[0], role_key: "tier.fast", model_id: (draft.tiers as { fast?: { model?: string } }).fast?.model || "model-a" },
      ] },
    }));
    const view = show("assignments");
    fireEvent.click(await screen.findByRole("button", { name: /进阶：/ }));
    fireEvent.change(screen.getByRole("combobox", { name: "轻量模型" }), { target: { value: "corp::model-b" } });
    const verify = screen.getByRole("button", { name: "验证草稿分工" });
    await waitFor(() => expect((verify as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(verify);
    fireEvent.click(await screen.findByRole("button", { name: "生成验证计划" }));
    await screen.findByRole("heading", { name: "确认验证计划" });
    expect(verificationApi.createVerificationPlan).toHaveBeenCalledWith(expect.objectContaining({
      items: expect.arrayContaining([{ provider_id: "llmp_corp", model_id: "model-b", capability: "chat" }]),
    }));
    expect(verificationApi.createVerificationBatch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认并开始验证" }));
    await waitFor(() => expect(configApi.fetchAdminLlmConfig).toHaveBeenCalledTimes(2));
    expect(view.container.querySelector<HTMLSelectElement>('select[name="fast_model"]')!.value).toBe("corp::model-b");
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it("reviews adding the first fallback once and preserves its order in the draft", async () => {
    const saved = structuredClone(response);
    saved.config.providers.corp.models.push({ id: "model-b", capabilities: ["chat", "tool"] });
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(saved);
    show("assignments");
    fireEvent.change(await screen.findByRole("combobox", { name: messages.settings.models.default.fallback_add }), { target: { value: "corp::model-b" } });
    fireEvent.click(screen.getByRole("button", { name: "检查并应用" }));
    const changes = await screen.findByRole("heading", { name: "分工修改" });
    expect(changes.parentElement!.querySelectorAll(".assignment-review-row")).toHaveLength(1);
    expect(within(changes.parentElement!).getByText(/1. corp · model-b/)).toBeTruthy();
    expect(vi.mocked(configApi.validateAdminLlmConfig).mock.calls.at(-1)![0].tiers).toMatchObject({ primary_fallback: [{ provider: "corp", model: "model-b" }] });
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it("leads with assignment controls and reviews changed selection rules before saving", async () => {
    show("assignments");
    await screen.findByRole("combobox", { name: "主模型" });
    expect(screen.queryByRole("combobox", { name: "轻量模型" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /进阶：/ }));
    fireEvent.change(screen.getByRole("combobox", { name: "轻量模型" }), { target: { value: "corp::model-a" } });
    expect(screen.getByText(messages.settings.models.overview.draft_evidence_boundary)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "验证当前分工" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "检查并应用" }));
    const changes = await screen.findByRole("heading", { name: "分工修改" });
    expect(within(changes.parentElement!).getByText(/跟随主模型/)).toBeTruthy();
    expect(within(changes.parentElement!).getByText(/指定模型/)).toBeTruthy();
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it("shows external-only guidance only when a connection has no catalog models", async () => {
    show("connection");
    await screen.findByRole("textbox", { name: "模型目录 1" });
    expect(screen.queryByText(/若此连接只用于图像生成或搜索/)).toBeNull();

    cleanup();
    const emptyResponse = structuredClone(response);
    emptyResponse.config.providers.corp.models = [];
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(emptyResponse);
    show("connection");
    expect(await screen.findByText(/若此连接只用于图像生成或搜索/)).toBeTruthy();
  });

  it("keeps assignment views local and opens external capabilities through their dedicated route", async () => {
    show("overview");
    const connectionsTab = await screen.findByRole("tab", { name: "模型连接" });
    const assignmentsTab = screen.getByRole("tab", { name: "模型分工" });
    expect(connectionsTab.getAttribute("aria-selected")).toBe("true");
    expect(within(screen.getByRole("tabpanel")).getByText("当前生效连接")).toBeTruthy();
    expect(within(screen.getByRole("tabpanel")).queryByText("模型分工与验证")).toBeNull();

    fireEvent.click(assignmentsTab);
    const panel = screen.getByRole("tabpanel");
    expect(assignmentsTab.getAttribute("aria-selected")).toBe("true");
    expect(within(panel).getByRole("heading", { name: messages.settings.models.overview.assignments_title })).toBeTruthy();
    expect(within(panel).queryByText("外部能力")).toBeNull();
    expect(within(panel).queryByText("当前生效连接")).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "外部能力" }));
    expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/external/");
    cleanup();
    const externalPanel = show("external").container;
    await screen.findByRole("heading", { name: "外部能力", level: 1 });
    expect(within(externalPanel).getByRole("heading", { name: "外部能力", level: 1 })).toBeTruthy();
    expect(within(externalPanel).queryByText("知识库索引")).toBeNull();
    expect(within(externalPanel).queryByText("模型分工与验证")).toBeNull();
    fireEvent.click(within(externalPanel).getAllByRole("button", { name: "修改配置" })[0]);
    expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/external/image/");
  });

  it("offers image configuration in its row when no image model is active", async () => {
    const unconfigured = structuredClone(response);
    unconfigured.config.media.active.image_gen = "";
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(unconfigured);
    show("external");
    const imageRow = (await screen.findByText("图片生成")).parentElement as HTMLElement;
    expect(within(imageRow).getByText("未配置")).toBeTruthy();
    fireEvent.click(within(imageRow).getByRole("button", { name: "配置" }));
    expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/external/image/");
  });

  it("moves the main assignment when the connection's sole model ID is replaced", async () => {
    const view = show("connection");
    const modelId = await screen.findByRole("textbox", { name: "模型目录 1" });
    fireEvent.change(modelId, { target: { value: "model-b" } });
    fireEvent.click(within(view.container.querySelector(".connect") as HTMLElement).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(configApi.saveAdminLlmConfig).toHaveBeenCalledOnce());
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/"));
    const outgoing = vi.mocked(configApi.saveAdminLlmConfig).mock.calls[0][0];
    expect((outgoing.providers as typeof config.providers).corp.models[0].id).toBe("model-b");
    expect(outgoing.tiers).toEqual({
      ...config.tiers,
      primary: { provider: "corp", model: "model-b" },
    });
  });

  it("shows an empty main-model selection when its saved model ID no longer exists", async () => {
    const stale = structuredClone(response);
    stale.config.providers.corp.models[0].id = "model-b";
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(stale);

    show("assignments");
    const primary = await screen.findByRole("combobox", { name: "主模型" }) as HTMLSelectElement;
    expect(primary.value).toBe("");
    expect(screen.getByText("请选择一个仍然存在的主模型。")).toBeTruthy();

    fireEvent.change(primary, { target: { value: "corp::model-b" } });
    await waitFor(() => expect(primary.value).toBe("corp::model-b"));
    expect(screen.queryByText("请选择一个仍然存在的主模型。")).toBeNull();
  });

  it("updates connection capability labels after verification and invalidates them on endpoint edits", async () => {
    const view = show("connection");
    await screen.findByDisplayValue("https://models.example.test/v1");
    fireEvent.click(screen.getByRole("button", { name: messages.settings.models.overview.verify_one }));
    await screen.findByRole("heading", { name: "确认验证计划" });
    expect(
      view.container.querySelector(".connect .verification-plan"),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认并开始验证" }));
    await waitFor(() =>
      expect(
        view.container.querySelectorAll(".captoggle.verified").length,
      ).toBe(2),
    );
    fireEvent.change(
      screen.getByDisplayValue("https://models.example.test/v1"),
      { target: { value: "https://other.example.test/v1" } },
    );
    await waitFor(() =>
      expect(
        view.container.querySelectorAll(".captoggle.verified").length,
      ).toBe(0),
    );
  });

  it("saves a connection while preserving assignments and external capability configuration", async () => {
    const view = show("connection");
    const endpoint = await screen.findByDisplayValue(
      "https://models.example.test/v1",
    );
    fireEvent.change(endpoint, {
      target: { value: "https://other.example.test/v1" },
    });
    fireEvent.click(
      within(view.container.querySelector(".connect") as HTMLElement).getByRole(
        "button",
        { name: "保存" },
      ),
    );
    await waitFor(() =>
      expect(configApi.saveAdminLlmConfig).toHaveBeenCalledOnce(),
    );
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/"));
    const outgoing = vi.mocked(configApi.saveAdminLlmConfig).mock.calls[0][0];
    expect(outgoing.media).toEqual(config.media);
    expect(outgoing.search).toEqual(config.search);
    expect(outgoing.tiers).toEqual(config.tiers);
    expect(outgoing.kb).toEqual({
      ...config.kb,
      pending_embed: null,
      index_version: undefined,
    });
    expect((outgoing.providers as typeof config.providers).corp.base_url).toBe(
      "https://other.example.test/v1",
    );
  });

  it("opens review immediately when connection validation needs attention", async () => {
    vi.mocked(configApi.validateAdminLlmConfig).mockResolvedValue({
      valid: false,
      version: 7,
      issues: [{ path: "providers.corp.base_url", code: "blocked_url", message: "Blocked address", severity: "error" }],
      has_changes: true,
      changed_sections: ["providers"],
      readiness,
    });
    const view = show("connection");
    fireEvent.change(await screen.findByDisplayValue("https://models.example.test/v1"), {
      target: { value: "https://other.example.test/v1" },
    });
    fireEvent.click(within(view.container.querySelector(".connect") as HTMLElement).getByRole("button", { name: "保存" }));

    expect(await screen.findByRole("heading", { name: "应用模型配置" })).toBeTruthy();
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it("returns to the list after a reviewed connection change is applied", async () => {
    vi.mocked(configApi.validateAdminLlmConfig).mockResolvedValue({
      valid: true,
      version: 7,
      issues: [{ path: "providers.corp", code: "review_recommended", message: "Review the change", severity: "warning" }],
      has_changes: true,
      changed_sections: ["providers"],
      readiness,
    });
    const view = show("connection");
    fireEvent.change(await screen.findByDisplayValue("https://models.example.test/v1"), {
      target: { value: "https://other.example.test/v1" },
    });
    fireEvent.click(within(view.container.querySelector(".connect") as HTMLElement).getByRole("button", { name: "保存" }));

    await screen.findByRole("heading", { name: "应用模型配置" });
    fireEvent.click(screen.getByRole("button", { name: "一次应用" }));
    await waitFor(() => expect(configApi.saveAdminLlmConfig).toHaveBeenCalledOnce());
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/"));
  });

  it("confirms an uncertain save from the server before returning to the list", async () => {
    vi.mocked(configApi.fetchAdminLlmConfig)
      .mockResolvedValueOnce(structuredClone(response))
      .mockImplementationOnce(async () => ({
        ...structuredClone(response),
        version: 8,
        config: vi.mocked(configApi.saveAdminLlmConfig).mock.calls[0][0],
      }));
    vi.mocked(configApi.saveAdminLlmConfig).mockRejectedValue(new Error("Connection closed after save"));
    const view = show("connection");
    fireEvent.change(await screen.findByDisplayValue("https://models.example.test/v1"), {
      target: { value: "https://other.example.test/v1" },
    });
    fireEvent.click(within(view.container.querySelector(".connect") as HTMLElement).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(configApi.saveAdminLlmConfig).toHaveBeenCalledOnce());
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/"));
  });

  it("opens the three section editors through their own overview actions", async () => {
    show("overview");
    await screen.findByRole("button", { name: "添加连接" });
    fireEvent.click(screen.getByRole("button", { name: "添加连接" }));
    expect(navigation.push).toHaveBeenLastCalledWith(
      "/team/settings/models/connections/new/",
    );
    fireEvent.click(screen.getByRole("tab", { name: "模型分工" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑分工" }));
    expect(navigation.push).toHaveBeenLastCalledWith(
      "/team/settings/models/assignments/",
    );
    fireEvent.click(screen.getByRole("tab", { name: "外部能力" }));
    expect(navigation.push).toHaveBeenLastCalledWith("/team/settings/models/external/");
    cleanup();
    show("external");
    await screen.findByText("联网搜索");
    fireEvent.click(screen.getAllByRole("button", { name: "修改配置" })[1]);
    expect(navigation.push).toHaveBeenLastCalledWith(
      "/team/settings/models/external/search/",
    );
  });

  it("confirms production verification within the selector and preserves selection when returning", async () => {
    show("overview");
    fireEvent.click(await screen.findByRole("tab", { name: "模型分工" }));
    await screen.findByRole("button", { name: "验证当前分工" });
    // The overview loads readiness before verification becomes available.
    await waitFor(() => expect(configApi.validateAdminLlmConfig).toHaveBeenCalled());
    await waitFor(() =>
      expect(
        (
          screen.getByRole("button", {
            name: "验证当前分工",
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "验证当前分工" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog)
        .getAllByRole("checkbox")
        .every((checkbox) => (checkbox as HTMLInputElement).checked),
    ).toBe(true);
    fireEvent.click(
      within(dialog).getByRole("button", { name: "生成验证计划" }),
    );
    await within(dialog).findByRole("heading", { name: "确认验证计划" });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    fireEvent.click(
      within(dialog).getByRole("button", { name: "返回调整验证项" }),
    );
    expect(
      within(dialog)
        .getAllByRole("checkbox")
        .every((checkbox) => (checkbox as HTMLInputElement).checked),
    ).toBe(true);
    vi.mocked(verificationApi.createVerificationPlan).mockRejectedValueOnce(
      new Error("network_error"),
    );
    fireEvent.click(
      within(dialog).getByRole("button", { name: "生成验证计划" }),
    );
    await within(dialog).findByRole("alert");
    expect(
      within(dialog)
        .getAllByRole("checkbox")
        .every((checkbox) => (checkbox as HTMLInputElement).checked),
    ).toBe(true);
  });

  it("keeps image generation model edits in the external capability configuration", async () => {
    show("image");
    fireEvent.change(await screen.findByLabelText("模型 ID"), {
      target: { value: "image-b" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检查并应用" }));
    await screen.findByText("没有阻塞问题，可以应用。");
    fireEvent.click(screen.getByRole("button", { name: "一次应用" }));
    await waitFor(() =>
      expect(configApi.saveAdminLlmConfig).toHaveBeenCalledOnce(),
    );
    const outgoing = vi.mocked(configApi.saveAdminLlmConfig).mock.calls[0][0];
    expect(outgoing.search).toEqual(config.search);
    expect(outgoing.tiers).toEqual(config.tiers);
    expect(outgoing.providers).toEqual(config.providers);
    expect((outgoing.media as typeof config.media).models.image.real_id).toBe(
      "image-b",
    );
  });

  it("reviews and confirms retrying a failed index even when the target model ID is unchanged", async () => {
    const indexedConfig = {
      ...config,
      kb: {
        models: {
          vec: {
            provider: "corp",
            real_id: "vec",
            capability: "embed",
            dim: 1024,
          },
        },
        active: { embed: "vec" },
        pending_embed: {
          model: "vec",
          task_id: "failed-task",
          state: "failed",
        },
        index_version: "serving-index",
      },
    };
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue({
      ...response,
      config: indexedConfig,
    });
    show("index");
    fireEvent.click(
      await screen.findByRole("button", { name: "查看并重试" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "检查并应用" }));
    await screen.findByText("没有阻塞问题，可以应用。");
    const apply = screen.getByRole("button", { name: "一次应用" });
    expect((apply as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(
      screen.getByRole("checkbox", { name: "我确认应用后开始重建知识库索引" }),
    );
    fireEvent.click(apply);
    await waitFor(() =>
      expect(configApi.saveAdminLlmConfig).toHaveBeenCalledOnce(),
    );
    expect(vi.mocked(configApi.saveAdminLlmConfig).mock.calls[0][1]).toBe(true);
  });
});


describe("Configuration Agent model review", () => {
  it("stages the saved draft and binds its receipt only after original Review/Apply", async () => {
    window.history.replaceState({}, "", "/team/settings/models/?configuration_proposal=ccd_models");
    const candidate = structuredClone(config);
    candidate.tiers.fast = { provider: "corp", model: "model-a" } as unknown as typeof candidate.tiers.fast;
    vi.mocked(configApi.fetchModelConfigurationProposal).mockResolvedValue({ proposal_id: "ccd_models", version: 7, config: candidate });
    show("overview");
    const review = await screen.findByRole("button", { name: "检查并应用" });
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
    fireEvent.click(review);
    await screen.findByText("没有阻塞问题，可以应用。");
    fireEvent.click(screen.getByRole("button", { name: "一次应用" }));
    await waitFor(() => expect(configApi.saveAdminLlmConfig).toHaveBeenCalledOnce());
    const args = vi.mocked(configApi.saveAdminLlmConfig).mock.calls[0];
    expect(args[0].tiers).toEqual(candidate.tiers);
    expect(args[4]).toBe("ccd_models");
  });

  it("keeps manual input when a proposal link arrives in a dirty editor", async () => {
    const view = show("image");
    fireEvent.change(await screen.findByLabelText("模型 ID"), { target: { value: "manual-image" } });
    window.history.replaceState({}, "", "/team/settings/models/external/image/?configuration_proposal=ccd_later");
    view.rerender(<NextIntlClientProvider locale="zh-CN" messages={messages}><SettingsModels section="image" /></NextIntlClientProvider>);
    await screen.findByText(messages.settings.models.configuration_proposal_unsaved);
    expect((screen.getByLabelText("模型 ID") as HTMLInputElement).value).toBe("manual-image");
    expect(configApi.fetchModelConfigurationProposal).not.toHaveBeenCalled();
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it("rejects a stale saved draft without changing configuration", async () => {
    window.history.replaceState({}, "", "/team/settings/models/?configuration_proposal=ccd_stale");
    vi.mocked(configApi.fetchModelConfigurationProposal).mockResolvedValue({ proposal_id: "ccd_stale", version: 6, config });
    show("overview");
    await screen.findByText(messages.settings.models.configuration_proposal_stale);
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });

  it.each([
    ["config_version_conflict", "configuration_proposal_stale"],
    ["proposal_state_conflict", "configuration_proposal_unavailable"],
  ] as const)("explains a refused proposal read with %s", async (code, messageKey) => {
    window.history.replaceState({}, "", "/team/settings/models/?configuration_proposal=ccd_refused");
    vi.mocked(configApi.fetchModelConfigurationProposal).mockRejectedValue(new ApiError(409, "Proposal read refused", { code }));
    show("overview");
    await screen.findByText(messages.settings.models[messageKey]);
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
  });
});


describe("automatic assignment maintenance", () => {
  it("reviews deletion of the last referenced connection without requiring a cross-tab repair", async () => {
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue({ ...structuredClone(response), features: { model_discovery_v2: true, model_assignment_reconciliation: true } });
    vi.mocked(configApi.validateAdminLlmConfig).mockResolvedValue({ valid: true, version: 7, issues: [], has_changes: true, changed_sections: ["providers", "tiers", "media", "search"], assignment_config: { tiers: { fast: { follow: "primary" }, judge: { auto: true } }, kb: { models: {}, active: {} }, media: { models: {}, active: {} }, search: { models: {}, active: {} } }, readiness });
    show("connection");
    await waitFor(() => expect(document.querySelector(".connect")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: messages.settings.models.sections.delete_connection }));
    expect(screen.getByText(messages.settings.models.del.soft_body.replace("{count}", "3"))).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: messages.settings.models.del.confirm }));
    fireEvent.click(await screen.findByRole("button", { name: messages.settings.models.v4.review }));
    await waitFor(() => expect(configApi.validateAdminLlmConfig).toHaveBeenCalledWith(expect.objectContaining({ providers: {} }), 7));
  });

  it("records explicit optional-off intent in the original Review/Apply draft", async () => {
    const saved = structuredClone(response);
    saved.config.kb = { models: { vision: { provider: "corp", real_id: "model-a", capability: "vision" } }, active: { vision: "vision" } } as never;
    saved.features = { ...saved.features, model_assignment_reconciliation: true } as typeof saved.features;
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(saved);
    const view = show("assignments");
    fireEvent.click(await screen.findByText(messages.settings.models.overview.kb_options, { selector: "summary" }));
    const select = await waitFor(() => {
      const control = view.container.querySelector<HTMLSelectElement>('select[name="vision_model"]');
      expect(control).toBeTruthy();
      return control!;
    });
    fireEvent.change(select, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: messages.settings.models.v4.review }));
    await waitFor(() => expect(configApi.validateAdminLlmConfig).toHaveBeenCalledWith(expect.objectContaining({ kb: expect.objectContaining({ active: {}, auto_assign_disabled: ["vision"] }) }), 7));
  });
});

describe("Bowra import", () => {
  it("offers Bowra beside Add connection even without saved connections", async () => {
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue({ ...response, config: {}, features: { model_discovery_v2: true, bowra_connect: true } });
    show("overview");
    const button = await screen.findByRole("button", { name: "使用 Bowra 服务" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("button", { name: messages.settings.models.overview.add_connection })).toBeTruthy();
    expect(bowraApi.startBowraConnection).not.toHaveBeenCalled();
  });

  it.each([1, 2])("prepares Chat and Tool verification for %i Bowra models without requiring assignments", async (count) => {
    window.history.replaceState({}, "", "/bowhead/team/settings/models?bowra_import=opaque-import");
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue({ ...response, config: {}, features: { model_discovery_v2: true, bowra_connect: true } });
    vi.mocked(bowraApi.claimBowraImport).mockResolvedValue({ base_url: "https://api.bowra.test/custom/v1", api_key: "private-bowra-key", models: ["first", "second"].slice(0, count) });
    show("overview");
    await screen.findByText(messages.settings.models.bowra.imported);
    await waitFor(() => expect(verificationApi.createVerificationPlan).toHaveBeenCalledTimes(1));
    expect(verificationApi.createVerificationBatch).not.toHaveBeenCalled();
    expect(document.querySelector('select[name="primary_model"]')).toBeNull();
    expect(verificationApi.createVerificationPlan).toHaveBeenCalledWith(expect.objectContaining({
      items: ["first", "second"].slice(0, count).flatMap((model_id) => ["chat", "tool"].map((capability) => ({ provider_id: "llmp_bowra", model_id, capability }))),
    }));
    expect(window.location.pathname).toBe("/bowhead/team/settings/models/");
    expect(window.location.search).toBe("");
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
    expect(bowraApi.claimBowraImport).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: messages.settings.models.common.save }));
    await waitFor(() => expect(configApi.validateAdminLlmConfig).toHaveBeenCalledWith(expect.objectContaining({
      providers: expect.objectContaining({ bowra: expect.objectContaining({ type: "openai_compatible", base_url: "https://api.bowra.test/custom/v1", api_key: "private-bowra-key" }) }),
      tiers: {},
    }), 7));
    await waitFor(() => expect(configApi.saveAdminLlmConfig).toHaveBeenCalledTimes(1));
  });

  it("discards an imported connection without navigating to the same route or saving it", async () => {
    window.history.replaceState({}, "", "/team/settings/models/?bowra_import=opaque-import");
    vi.mocked(bowraApi.claimBowraImport).mockResolvedValue({ base_url: "https://api.bowra.test/v1", api_key: "private-bowra-key", models: ["first"] });
    const view = show("overview");
    await screen.findByRole("heading", { name: messages.settings.models.v2.verification_confirm_title });
    fireEvent.click(within(view.container.querySelector(".ui-savebar") as HTMLElement).getByRole("button", { name: messages.settings.models.common.cancel }));
    fireEvent.click(await screen.findByRole("button", { name: messages.settings.models.v4.leave_action }));
    await screen.findByRole("tab", { name: messages.settings.models.sections.tab_connections });
    expect(view.container.querySelector(".connect")).toBeNull();
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it("keeps an existing primary and adds a distinct connection", async () => {
    window.history.replaceState({}, "", "/team/settings/models/?bowra_import=opaque-import");
    vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue({ ...response, config: { ...config, providers: { ...config.providers, bowra: config.providers.corp } } });
    vi.mocked(bowraApi.claimBowraImport).mockResolvedValue({ base_url: "https://api.bowra.test/custom/v1", api_key: "private-bowra-key", models: ["new-model"] });
    show("overview");
    await screen.findByText(messages.settings.models.bowra.imported);
    await screen.findByText(messages.settings.models.v2.verification_confirm_title);
    expect(document.querySelector('select[name="primary_model"]')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: messages.settings.models.common.save }));
    await waitFor(() => expect(configApi.validateAdminLlmConfig).toHaveBeenCalledWith(expect.objectContaining({
      providers: expect.objectContaining({ bowra: config.providers.corp, bowra_2: expect.objectContaining({ base_url: "https://api.bowra.test/custom/v1", api_key: "private-bowra-key" }) }),
      tiers: config.tiers,
    }), 7));
  });
});
