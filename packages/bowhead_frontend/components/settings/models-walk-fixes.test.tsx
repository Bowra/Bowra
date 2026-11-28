/** Fixes from the ordinary-administrator walk on an observed deployment (docs/sprints/model-settings-save-flow-2026-11-25/spike.md §8). */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReadinessReview } from "@/components/settings/models/readiness-review";
import { mapProviderDraftToConnect } from "@/lib/provider-draft-map";
import messages from "@/messages/zh-CN.json";

const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(window.location.search) }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => navigation, BEFORE_APP_NAVIGATION_EVENT: "bowhead:before-app-navigation" }));
vi.mock("@/lib/config-copilot-lite", () => ({ refreshLiteState: vi.fn() }));

const configMocks = vi.hoisted(() => ({
  fetchAdminLlmConfig: vi.fn(),
  saveAdminLlmConfig: vi.fn(),
  validateAdminLlmConfig: vi.fn(),
  verifyDraftProviderCapability: vi.fn(),
}));
vi.mock("@/lib/llm-config-api", async (original) => ({ ...await original<typeof import("@/lib/llm-config-api")>(), ...configMocks }));

const v2Mocks = vi.hoisted(() => ({
  createVerificationPlan: vi.fn(),
  fetchActiveVerificationBatches: vi.fn(),
  fetchDraftEvidence: vi.fn(),
  fetchOutboundAdaptations: vi.fn(),
}));
vi.mock("@/lib/llm-config-v2-api", async (original) => ({ ...await original<typeof import("@/lib/llm-config-v2-api")>(), ...v2Mocks }));

import { SettingsModels } from "@/components/settings/models";

const CONFIG = {
  schema_version: 2,
  providers: {
    corp: {
      type: "openai_compatible",
      base_url: "https://models.example.test/v1",
      models: [{ id: "model-a", capabilities: ["chat", "tool"], source: "manual", credit_rates: { input: "1", cached_input: "1", output: "1" } }],
    },
  },
  tiers: {},
  kb: { models: {}, active: {}, pending_embed: null },
  media: { models: { image: { provider: "corp", real_id: "image-a", capability: "image_gen", protocol: "ark_sync" } }, active: { image_gen: "image" } },
  search: { models: { search: { provider: "corp", real_id: "search-a", capability: "web_search", protocol: "dashscope_native" } }, active: { web_search: "search" } },
};

const RESPONSE = {
  version: 7,
  config: CONFIG,
  providers: [],
  provider_identities: { corp: "llmp_corp" },
  features: { model_discovery_v2: true },
  verification: {
    as_of: "2026-11-25",
    entries: [
      { provider_id: "llmp_corp", model_id: "model-a", capability: "chat", fact_state: "verified", observed_at: "2026-11-20T00:00:00Z" },
      { provider_id: "llmp_corp", model_id: "model-a", capability: "tool", fact_state: "stale", observed_at: null },
    ],
  },
};

beforeEach(() => {
  configMocks.fetchAdminLlmConfig.mockResolvedValue(structuredClone(RESPONSE));
  configMocks.validateAdminLlmConfig.mockResolvedValue({ valid: true, version: 7, issues: [], has_changes: false, changed_sections: [] });
  v2Mocks.fetchActiveVerificationBatches.mockResolvedValue({ batches: [] });
  v2Mocks.fetchOutboundAdaptations.mockResolvedValue({ items: [] });
  v2Mocks.fetchDraftEvidence.mockResolvedValue({ provider_id: "llmp_corp", draft_invalid: false, items: [] });
  v2Mocks.createVerificationPlan.mockImplementation(async (body: { items: Array<{ provider_id: string; model_id: string; capability: string }> }) => ({
    plan_id: "plan", confirmation_token: "token", expires_at: "2099-01-01", item_count: body.items.length,
    provider_request_count: body.items.length, max_concurrency: 3, notices: [],
    items: body.items.map((item) => ({ ...item, provider_display_name: "corp", testability: "testable", provider_request_count: 1, testability_contract_version: "v1", probe_execution_policy_version: "v1" })),
  }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState({}, "", "/");
});

function show(section: "overview" | "connection" | "external", path = "/team/settings/models/", newConnection = false) {
  window.history.replaceState({}, "", path);
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <SettingsModels section={section} newConnection={newConnection} />
    </NextIntlClientProvider>,
  );
}

describe("connection list", () => {
  it("explains a label that needs attention and verifies just that item", async () => {
    show("overview");
    const label = await screen.findByRole("button", { name: /model-a 工具调用: 连接设置改过/ });
    expect(label.getAttribute("title")).toContain("原来的验证结果已不适用");
    expect(screen.queryByRole("button", { name: /对话: / })).toBeNull();

    fireEvent.click(label);

    const dialog = await screen.findByRole("dialog", { name: "验证 corp" });
    await waitFor(() => expect(v2Mocks.createVerificationPlan).toHaveBeenCalledOnce());
    expect(v2Mocks.createVerificationPlan.mock.calls[0][0].items).toEqual([{ provider_id: "llmp_corp", model_id: "model-a", capability: "tool" }]);
    expect(await within(dialog).findByRole("button", { name: "确认并开始验证" })).toBeTruthy();
  });

  it("opens a connection's Verify as a dialog with only what needs verifying selected", async () => {
    show("overview");
    const row = (await screen.findByText("corp")).closest(".overview-provider-row") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "验证" }));

    const dialog = await screen.findByRole("dialog", { name: "验证 corp" });
    const checked = within(dialog).getAllByRole("checkbox").filter((box) => (box as HTMLInputElement).checked);
    expect(checked).toHaveLength(1);
    expect(checked[0].closest("label")?.textContent).toContain("工具调用");
    expect(v2Mocks.createVerificationPlan).not.toHaveBeenCalled();
    expect(document.querySelector(".mset > .verification-plan")).toBeNull();
  });
});

describe("external capabilities", () => {
  it("names the model and connection and tests the service after confirmation", async () => {
    configMocks.verifyDraftProviderCapability.mockResolvedValue({ ok: true, code: "ok" });
    show("external", "/team/settings/models/external/");
    const row = (await screen.findByText("图片生成")).closest(".overview-external-rows > div") as HTMLElement;
    expect(row.textContent).toContain("image-a · corp");
    expect(screen.getAllByText("外部能力").filter((node) => node.tagName === "H2")).toHaveLength(0);

    fireEvent.click(within(row).getByRole("button", { name: "测试" }));
    // The confirmation names the cost before anything is sent.
    expect(await screen.findByText("测试图片生成？")).toBeTruthy();
    expect(screen.getByText("测试会用 image-a 生成一张图片，可能产生费用。")).toBeTruthy();
    expect(configMocks.verifyDraftProviderCapability).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "测试" }).at(-1) as HTMLElement);

    await waitFor(() => expect(configMocks.verifyDraftProviderCapability).toHaveBeenCalledOnce());
    expect(configMocks.verifyDraftProviderCapability).toHaveBeenCalledWith("corp", expect.objectContaining({
      model: "image-a", capability: "image_gen", capability_protocol: "ark_sync", base_url: "https://models.example.test/v1",
    }));
    expect(await within(row).findByText("测试通过")).toBeTruthy();
  });

  it("names the failing step's cause, not the summary code", async () => {
    configMocks.verifyDraftProviderCapability.mockResolvedValue({
      ok: false,
      code: "capability_failed",
      steps: [{ step: "capability", ok: false, code: "web_search_output_missing", status: "failed", model: "search-a", capability: "web_search", http_status: 200 }],
    });
    show("external", "/team/settings/models/external/");
    const row = (await screen.findByText("联网搜索")).closest(".overview-external-rows > div") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "测试" }));
    fireEvent.click((await screen.findAllByRole("button", { name: "测试" })).at(-1) as HTMLElement);

    expect(await within(row).findByText("测试未通过：服务已响应，但没有返回搜索结果。请确认该模型支持联网搜索，并在「修改配置」中检查调用协议。")).toBeTruthy();
    expect(row.textContent).not.toContain("模型能力验证未通过");
  });

  it("shows the code and HTTP status of a cause it has no words for", async () => {
    configMocks.verifyDraftProviderCapability.mockResolvedValue({
      ok: false,
      code: "capability_failed",
      steps: [{ step: "capability", ok: false, code: "provider_new_code", status: "failed", http_status: 502 }],
    });
    show("external", "/team/settings/models/external/");
    const row = (await screen.findByText("联网搜索")).closest(".overview-external-rows > div") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "测试" }));
    fireEvent.click((await screen.findAllByRole("button", { name: "测试" })).at(-1) as HTMLElement);

    expect(await within(row).findByText(/服务返回了未识别的结果（provider_new_code，HTTP 502）/)).toBeTruthy();
  });
});

describe("connection editor", () => {
  it("says why Verify is off for a new connection without a key", async () => {
    show("connection", "/team/settings/models/connections/new/", true);
    await waitFor(() => expect(document.querySelector(".connect-body")).toBeTruthy());
    fireEvent.change(document.querySelector("input[name='provider_name']") as HTMLInputElement, { target: { value: "fresh" } });
    fireEvent.change(document.querySelector("input[name='provider_base_url']") as HTMLInputElement, { target: { value: "https://fresh.example.test/v1" } });
    fireEvent.click(screen.getByRole("button", { name: /添加模型/ }));
    fireEvent.change(document.querySelector("input[name='provider_model_0']") as HTMLInputElement, { target: { value: "fresh-chat" } });
    fireEvent.click(document.querySelectorAll(".medit-caps .captoggle")[0]);

    const bar = document.querySelector(".ui-savebar") as HTMLElement;
    await waitFor(() => expect(bar.textContent).toContain("填写 API Key 后才能验证。"));
    const verify = within(bar).getByRole("button", { name: /验证/ }) as HTMLButtonElement;
    expect(verify.disabled).toBe(true);
    expect(verify.getAttribute("title")).toBe("填写 API Key 后才能验证。");
  });

  it("asks to discard unsaved changes, not unapplied ones, when leaving", async () => {
    show("connection", "/team/settings/models/connections/edit/?id=llmp_corp");
    await waitFor(() => expect(document.querySelector("input[name='provider_base_url']")).toBeTruthy());
    fireEvent.change(document.querySelector("input[name='provider_base_url']") as HTMLInputElement, { target: { value: "https://other.example.test/v1" } });
    // The app router announces in-app navigation; the page's guard asks before leaving.
    window.dispatchEvent(new CustomEvent("bowhead:before-app-navigation", { cancelable: true, detail: { href: "/team/settings/models/", proceed: vi.fn() } }));

    expect(await screen.findByText("放弃未保存的修改？")).toBeTruthy();
    expect(screen.getByText("离开后，这条连接中未保存的修改会被丢弃。")).toBeTruthy();
  });
});

describe("small fixes", () => {
  it("ticks tool calling with chat in smart fill", () => {
    expect(mapProviderDraftToConnect({ models: [{ id: "deepseek-chat", caps: ["chat"] }] } as never).models?.[0].caps).toEqual(["chat", "tool"]);
    expect(mapProviderDraftToConnect({ models: [{ id: "text-embedding-v4", caps: ["embed"] }] } as never).models?.[0].caps).toEqual(["embed"]);
  });

  it("does not say 其余 when nothing is listed above", () => {
    const ready = (capability: string) => ({ role_key: "tier.primary", provider_id: "p", provider_display_name: "corp", model_id: "m", capability, routing_critical: true, declared: true, fact_state: "verified", status: "ready" });
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messages}>
        <ReadinessReview
          readiness={{ as_of: "x", draft_semantic_hash: "h", may_apply: true, rows: [ready("chat"), ready("tool")], blocking: [], warnings: [], required_acceptances: [], migration_acceptance: null, external_rows: [], baseline_will_invalidate: false } as never}
          acceptedTokens={new Set()} migrationAccepted={false} onToggleAcceptance={vi.fn()} onToggleMigration={vi.fn()}
        />
      </NextIntlClientProvider>,
    );
    expect(document.querySelector(".readiness-settled summary")?.textContent).toBe("2 项无需处理");
  });
});
