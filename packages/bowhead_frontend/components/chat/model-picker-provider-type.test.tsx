/**
 * API-type labels next to model-picker group headings show user-friendly names such as “OpenAI 兼容” and “Anthropic 兼容”
 * (Menus Spec §1), never raw configuration field values such as `anthropic_compatible`.
 * Render real zh-CN / en-US messages without mocking next-intl.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

vi.mock("@/lib/api", () => ({
  getUserSettings: vi.fn(async () => ({})),
  setUserSetting: vi.fn(async () => ({})),
}));

import { ModelPicker } from "./model-picker";

const ORIGINAL_FETCH = globalThis.fetch;

const CATALOG = {
  version: 1,
  providers: [
    { id: "p_openai", display_name: "阿里云", type: "openai_compatible", models: ["qwen-plus"] },
    { id: "p_anthropic", display_name: "DeepSeek", type: "anthropic_compatible", models: ["deepseek-v4"] },
    { id: "p_azure", display_name: "Azure", type: "azure_openai", models: ["gpt-4.1"] },
    { id: "p_http", display_name: "Gateway", type: "custom_http", models: ["house-model"] },
    { id: "p_future", display_name: "Future", type: "future_protocol_x", models: ["fx-1"] },
  ],
  default_model: { provider: "p_openai", model: "qwen-plus" },
};

beforeEach(() => {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify(CATALOG), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  ) as typeof fetch;
});

afterEach(() => {
  cleanup();
  globalThis.fetch = ORIGINAL_FETCH;
});

async function openMenuTags(locale: "zh-CN" | "en-US"): Promise<string[]> {
  renderIntl(<ModelPicker />, { locale });
  fireEvent.click(await screen.findByRole("button", { name: /qwen-plus/ }));
  const menu = await screen.findByRole("menu");
  return Array.from(menu.querySelectorAll(".legacy-menu-model-tag")).map((n) => n.textContent ?? "");
}

describe("ModelPicker 接口类型标签", () => {
  it("中文：按协议映射，未知协议回落「其他接口」", async () => {
    const tags = await openMenuTags("zh-CN");
    expect(tags).toEqual(["OpenAI 兼容", "Anthropic 兼容", "Azure OpenAI", "自定义 HTTP", "其他接口"]);
  });

  it("英文：按协议映射，未知协议回落 Other API", async () => {
    const tags = await openMenuTags("en-US");
    expect(tags).toEqual(["OpenAI-compatible", "Anthropic-compatible", "Azure OpenAI", "Custom HTTP", "Other API"]);
  });

  it("菜单里不出现任何原始协议值", async () => {
    await openMenuTags("zh-CN");
    const text = screen.getByRole("menu").textContent ?? "";
    for (const raw of ["openai_compatible", "anthropic_compatible", "azure_openai", "custom_http", "future_protocol_x"]) {
      expect(text).not.toContain(raw);
    }
  });
});
