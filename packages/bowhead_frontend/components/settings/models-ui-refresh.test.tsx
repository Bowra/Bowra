/**
 * Team Models settings: the unified save bar replaces the draft dock and loading shows a skeleton.
 * Check and Apply / Discard use the same review / discard flows.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsModels } from "@/components/settings/models";
import * as configApi from "@/lib/llm-config-api";
import * as verificationApi from "@/lib/llm-config-v2-api";
import messages from "@/messages/zh-CN.json";

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  BEFORE_APP_NAVIGATION_EVENT: "bowhead:before-app-navigation",
}));
vi.mock("@/lib/llm-config-api", async (original) => ({
  ...(await original<typeof configApi>()),
  fetchAdminLlmConfig: vi.fn(),
  validateAdminLlmConfig: vi.fn(),
  saveAdminLlmConfig: vi.fn(),
}));
vi.mock("@/lib/llm-config-v2-api", async (original) => ({
  ...(await original<typeof verificationApi>()),
  fetchActiveVerificationBatches: vi.fn(),
  fetchOutboundAdaptations: vi.fn(),
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
  tiers: { primary: { provider: "corp", model: "model-a" }, fast: { follow: "primary" }, judge: { auto: true } },
  kb: { models: {}, active: {} },
  media: {
    models: { image: { provider: "corp", real_id: "image-a", capability: "image_gen", protocol: "ark_sync" } },
    active: { image_gen: "image" },
  },
  search: { models: {}, active: {} },
};
const response = {
  version: 7,
  config,
  providers: [],
  provider_identities: { corp: "llmp_corp" },
  features: { model_discovery_v2: true },
  verification: { as_of: "2026-09-17", entries: [] },
};

function show() {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <SettingsModels section="image" />
    </NextIntlClientProvider>,
  );
}

async function makeDirty() {
  fireEvent.change(await screen.findByLabelText("模型 ID"), { target: { value: "image-b" } });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(configApi.fetchAdminLlmConfig).mockResolvedValue(structuredClone(response) as never);
  vi.mocked(configApi.validateAdminLlmConfig).mockResolvedValue({
    valid: true,
    version: 7,
    issues: [],
    has_changes: true,
    changed_sections: ["media"],
  } as never);
  vi.mocked(verificationApi.fetchActiveVerificationBatches).mockResolvedValue({ batches: [] });
  vi.mocked(verificationApi.fetchOutboundAdaptations).mockResolvedValue({ items: [] });
});

afterEach(() => cleanup());

describe("Models settings: save bar and loading skeleton", () => {
  it("加载时渲染骨架（role=status、带「加载中」名称），不渲染原来的加载空态", () => {
    vi.mocked(configApi.fetchAdminLlmConfig).mockReturnValue(new Promise(() => {}));
    const view = show();
    const skeleton = view.container.querySelector(".ui-skeleton");
    expect(skeleton?.getAttribute("role")).toBe("status");
    expect(skeleton?.getAttribute("aria-label")).toBe(messages.settings.models.loading);
    expect(view.container.querySelector(".empty.big")).toBeNull();
  });

  it("没有改动时不出现保存栏；改动后出现保存栏（写出改了几个配置区），不再渲染草稿坞", async () => {
    const view = show();
    await screen.findByLabelText("模型 ID");
    expect(document.querySelector(".ui-savebar")).toBeNull();
    await makeDirty();
    const bar = await waitFor(() => {
      const found = document.querySelector(".ui-savebar");
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(bar.textContent).toContain("有未应用的草稿");
    expect(bar.textContent).toContain("已修改 1 个配置区");
    expect(view.container.querySelector(".draft-dock")).toBeNull();
  });

  it("保存栏的「检查并应用」走原来的审阅（校验改过的草稿）", async () => {
    show();
    await makeDirty();
    fireEvent.click(await screen.findByRole("button", { name: "检查并应用" }));
    // Initial readiness also validates once (original configuration); clicking Check and Apply validates the edited draft
    await waitFor(() =>
      expect(vi.mocked(configApi.validateAdminLlmConfig).mock.calls.some(([draft]) => JSON.stringify(draft.media ?? {}).includes('"real_id":"image-b"'))).toBe(true),
    );
  });

  it("「放弃」恢复原值并收起保存栏", async () => {
    show();
    await makeDirty();
    fireEvent.click(await screen.findByRole("button", { name: "放弃" }));
    await waitFor(() => expect(document.querySelector(".ui-savebar")).toBeNull());
    expect((screen.getByLabelText("模型 ID") as HTMLInputElement).value).toBe("image-a");
  });
});
