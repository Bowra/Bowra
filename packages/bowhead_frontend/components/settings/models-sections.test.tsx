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

import { SettingsModels } from "@/components/settings/models";
import * as configApi from "@/lib/llm-config-api";
import * as verificationApi from "@/lib/llm-config-v2-api";
import messages from "@/messages/zh-CN.json";

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
  vi.mocked(verificationApi.precheckV2Provider).mockResolvedValue({
    provider_id: "llmp_corp",
  } as verificationApi.ProviderPrecheckResult);
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
  it("keeps the KB count automatic until edited and includes an explicit edit in review", async () => {
    show("assignments");
    const count = await screen.findByLabelText("检索返回数量");
    expect((count as HTMLSelectElement).value).toBe("15");
    expect(configApi.saveAdminLlmConfig).not.toHaveBeenCalled();
    fireEvent.change(count, { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "检查并应用" }));
    await waitFor(() => expect(configApi.validateAdminLlmConfig).toHaveBeenCalled());
    const reviewed = vi.mocked(configApi.validateAdminLlmConfig).mock.calls.at(-1)![0];
    expect(reviewed).toMatchObject({ kb: { retrieval_top_k: 30, active: {} } });
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
    expect(within(screen.getByRole("tabpanel")).queryByText("生产分工与验证依据")).toBeNull();

    fireEvent.click(assignmentsTab);
    const panel = screen.getByRole("tabpanel");
    expect(assignmentsTab.getAttribute("aria-selected")).toBe("true");
    expect(within(panel).getByText("生产分工与验证依据")).toBeTruthy();
    expect(within(panel).queryByText("外部能力")).toBeNull();
    expect(within(panel).queryByText("当前生效连接")).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "外部能力" }));
    expect(navigation.push).toHaveBeenCalledWith("/team/settings/models/external/");
    cleanup();
    const externalPanel = show("external").container;
    await screen.findByRole("heading", { name: "外部能力", level: 1 });
    expect(within(externalPanel).getByRole("heading", { name: "外部能力", level: 1 })).toBeTruthy();
    expect(within(externalPanel).queryByText("知识库索引")).toBeNull();
    expect(within(externalPanel).queryByText("生产分工与验证依据")).toBeNull();
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
    fireEvent.click(screen.getByRole("button", { name: "制定验证计划" }));
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
    show("assignments");
    await screen.findByRole("button", { name: "验证当前分工" });
    // Loading the assignments page first runs readiness validation (validateAdminLlmConfig), disabling the button during it. Before validation begins,
    // the button can briefly be enabled; clicking then hits a button that soon becomes disabled, so the dialog never appears. Wait for validation to start, then for it
    // to finish and the button to become enabled, and only then click.
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
});
