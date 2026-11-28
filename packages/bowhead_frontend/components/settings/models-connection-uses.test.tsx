/** Everything a connection serves (walk W4; docs/sprints/model-settings-save-flow-2026-11-25/connection-purposes-design.md). */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectionUses } from "@/components/settings/models";
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

const RATES = { input: "1", cached_input: "1", output: "1" };
/** `corp` declares one chat model that also extracts rows and serves a vector model outside its catalog; `native`
 *  declares nothing and serves knowledge-base rerank, image generation and web search (the 116 shape). */
const CONFIG = {
  schema_version: 2,
  providers: {
    corp: {
      type: "openai_compatible",
      base_url: "https://corp.example.test/v1",
      models: [{ id: "model-a", capabilities: ["chat", "tool"], source: "manual", credit_rates: RATES }],
    },
    native: { type: "openai_compatible", base_url: "https://native.example.test/api/v1", models: [] },
  },
  tiers: { primary: { provider: "corp", model: "model-a" } },
  kb: {
    models: {
      vectors: { provider: "corp", real_id: "embed-x", capability: "embed", dim: 1024 },
      ranking: { provider: "native", real_id: "rerank-x", capability: "rerank", protocol: "dashscope_native" },
      rows: { provider: "corp", real_id: "model-a", capability: "chat" },
      spare: { provider: "native", real_id: "unused-x", capability: "embed", dim: 768 },
    },
    active: { embed: "vectors", rerank: "ranking", row_extractor: "rows" },
    pending_embed: null,
  },
  media: { models: { image: { provider: "native", real_id: "image-a", capability: "image_gen", protocol: "dashscope_native" } }, active: { image_gen: "image" } },
  search: { models: { search: { provider: "native", real_id: "search-a", capability: "web_search", protocol: "dashscope_native" } }, active: { web_search: "search" } },
};

const RESPONSE = {
  version: 9,
  config: CONFIG,
  providers: [],
  provider_identities: { corp: "llmp_corp", native: "llmp_native" },
  features: { model_discovery_v2: true },
  verification: {
    as_of: "2026-11-25",
    entries: [
      { provider_id: "llmp_corp", model_id: "model-a", capability: "chat", fact_state: "verified", observed_at: "2026-11-20T00:00:00Z" },
      { provider_id: "llmp_corp", model_id: "model-a", capability: "tool", fact_state: "verified", observed_at: "2026-11-20T00:00:00Z" },
    ],
  },
};

beforeEach(() => {
  configMocks.fetchAdminLlmConfig.mockResolvedValue(structuredClone(RESPONSE));
  configMocks.validateAdminLlmConfig.mockResolvedValue({ valid: true, version: 9, issues: [], has_changes: false, changed_sections: [] });
  v2Mocks.fetchActiveVerificationBatches.mockResolvedValue({ batches: [] });
  v2Mocks.fetchOutboundAdaptations.mockResolvedValue({ items: [] });
  v2Mocks.fetchDraftEvidence.mockResolvedValue({ provider_id: "llmp_native", draft_invalid: false, items: [] });
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

function show(section: "overview" | "connection", path = "/team/settings/models/") {
  window.history.replaceState({}, "", path);
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <SettingsModels section={section} />
    </NextIntlClientProvider>,
  );
}

async function connectionRow(name: string): Promise<HTMLElement> {
  return (await screen.findByText(name, { selector: ".overview-provider-main b" })).closest(".overview-provider-row") as HTMLElement;
}

describe("connection list", () => {
  it("counts and lists every model a connection serves, grouped by where it is used", async () => {
    show("overview");
    const native = await connectionRow("native");
    expect(within(native).getByText("3 个模型")).toBeTruthy();
    expect(native.textContent).toContain("知识库在「模型分工」中修改rerank-x知识库重排重排 · 未验证");
    expect(native.textContent).toContain("image-a图片生成未测试");
    expect(native.textContent).toContain("search-a联网搜索未测试");
    // A knowledge-base declaration no role uses is not served.
    expect(native.textContent).not.toContain("unused-x");
    expect(within(native).getByRole("link", { name: "在「模型分工」中修改" })).toBeTruthy();
    expect(within(native).getByRole("link", { name: "在「外部能力」中修改" })).toBeTruthy();

    const corp = await connectionRow("corp");
    expect(within(corp).getByText("2 个模型")).toBeTruthy();
    expect(within(corp).getByText("模型目录")).toBeTruthy();
    expect(within(corp).getByText("也用于行抽取")).toBeTruthy();
    expect(corp.textContent).toContain("embed-x知识库向量向量 · 未验证");
  });

  it("verifies a knowledge-base model outside the catalog from its label, planning from the catalog-only draft", async () => {
    show("overview");
    const corp = await connectionRow("corp");
    fireEvent.click(within(corp).getByRole("button", { name: /embed-x 向量: 还没有验证过/ }));

    await waitFor(() => expect(v2Mocks.createVerificationPlan).toHaveBeenCalledOnce());
    const body = v2Mocks.createVerificationPlan.mock.calls[0][0];
    expect(body.items).toEqual([{ provider_id: "llmp_corp", model_id: "embed-x", capability: "embed" }]);
    expect(body.provider_drafts.map((draft: { runtime_key: string; models: Array<{ id: string }> }) => [draft.runtime_key, draft.models.map((model) => model.id)]))
      .toEqual([["corp", ["model-a"]]]);
  });

  it("offers a connection without catalog models its knowledge-base capabilities in Verify", async () => {
    show("overview");
    const native = await connectionRow("native");
    fireEvent.click(within(native).getByRole("button", { name: "验证" }));
    const dialog = await screen.findByRole("dialog", { name: "验证 native" });
    expect(within(dialog).getAllByRole("checkbox").map((box) => box.closest("label")?.textContent)).toEqual([expect.stringContaining("rerank-x")]);
  });

  it("opens Model assignments from the knowledge-base group", async () => {
    show("overview");
    const native = await connectionRow("native");
    const link = within(native).getByRole("link", { name: "在「模型分工」中修改" });
    expect(link.getAttribute("href")).toBe("/team/settings/models/#assignments");
    fireEvent.click(link);
    expect(navigation.replace).toHaveBeenCalledWith("/team/settings/models/#assignments", { scroll: false });
  });
});

describe("connection editor", () => {
  it("lists what the connection also serves and hides the chat request preview when it serves no chat", async () => {
    show("connection", "/team/settings/models/connections/edit/?id=llmp_native");
    const uses = await screen.findByRole("region", { name: "这条连接还用于" });
    expect([...uses.querySelectorAll("li")].map((item) => item.textContent)).toEqual(["rerank-x · 知识库重排", "image-a · 图片生成", "search-a · 联网搜索"]);
    expect(within(uses).getByRole("link", { name: "在「外部能力」中修改" }).getAttribute("href")).toBe("/team/settings/models/external/");
    expect(screen.queryByTestId("provider-effective-endpoint")).toBeNull();
  });

  it("keeps the request preview for a connection with a chat model", async () => {
    show("connection", "/team/settings/models/connections/edit/?id=llmp_corp");
    const uses = await screen.findByRole("region", { name: "这条连接还用于" });
    expect([...uses.querySelectorAll("li")].map((item) => item.textContent)).toEqual(["embed-x · 知识库向量", "model-a · 行抽取"]);
    expect(screen.getByTestId("provider-effective-endpoint").textContent).toContain("https://corp.example.test/v1/chat/completions");
  });
});

describe("connectionUses", () => {
  it("reads active knowledge-base roles by real_id, then model, and every image and search model", () => {
    expect(connectionUses({
      kb: { models: { legacy: { provider: "p", model: "legacy-embed", capability: "embed" } as never, other: { provider: "q", real_id: "x" } }, active: { embed: "legacy", vision: "other", unknown_role: "legacy" } },
      media: { models: { a: { provider: "p", real_id: "img" } }, active: {} },
      search: { models: { b: { provider: "q", real_id: "web" } }, active: {} },
    }, "p")).toEqual([{ modelId: "legacy-embed", role: "embed" }, { modelId: "img", role: "image" }]);
  });
});
