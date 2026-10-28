/**
 * API-type labels for the model overview's active connection: never expose raw configuration values (`provider.type`) to users.
 * Known protocols use localized settings.llm.provider_types copy; unrecognized protocols show “其他接口”.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ModelSettingsOverview, type ModelSettingsOverviewProvider } from "@/components/settings/models/model-settings-overview";
import messagesDe from "@/messages/de-DE.json";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

afterEach(cleanup);

const PROVIDERS: ModelSettingsOverviewProvider[] = [
  { runtimeKey: "a", displayName: "Claude 网关", type: "anthropic_compatible", endpoint: "https://a.example.com/v1", modelCount: 2 },
  { runtimeKey: "b", displayName: "Azure 生产", type: "azure_openai", endpoint: "https://b.example.com", modelCount: 1 },
  { runtimeKey: "c", displayName: "旧网关", type: "legacy_vendor_x", endpoint: "https://c.example.com", modelCount: 1 },
];

function renderOverview(locale: "zh-CN" | "en-US") {
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "zh-CN" ? messagesZh : messagesEn}>
      <ModelSettingsOverview
        version={1}
        providers={PROVIDERS}
        readiness={null}
        readinessLoading={false}
        readinessError={null}
        reindexState="idle"
        onEdit={vi.fn()}
        onAddProvider={vi.fn()}
        onEditProvider={vi.fn()}
        onRetryReadiness={vi.fn()}
        onVerify={vi.fn()}
        verificationBusy={false}
        verificationAvailable
        configurationActionsDisabled={false}
      />
    </NextIntlClientProvider>,
  );
}

function typeLabel(name: string): string | null {
  return screen.getByText(name).nextElementSibling?.textContent ?? null;
}

describe("模型概览连接的接口类型标签", () => {
  it("中文界面：已知协议显示中文名，未知协议显示「其他接口」，不露出原始值", () => {
    renderOverview("zh-CN");
    expect(typeLabel("Claude 网关")).toBe("Anthropic 兼容");
    expect(typeLabel("Azure 生产")).toBe("Azure OpenAI");
    expect(typeLabel("旧网关")).toBe("其他接口");
    expect(screen.queryByText("legacy_vendor_x")).toBeNull();
  });

  it("英文界面用同一套文案", () => {
    renderOverview("en-US");
    expect(typeLabel("Claude 网关")).toBe("Anthropic-compatible");
    expect(typeLabel("旧网关")).toBe("Other API");
  });
});

describe("Azure 接口显示名", () => {
  it("三种语言下，模型概览/对话模型菜单与协议选择都只显示「Azure OpenAI」", () => {
    for (const messages of [messagesZh, messagesEn, messagesDe]) {
      expect(messages.settings.llm.provider_types.azure_openai).toBe("Azure OpenAI");
      expect(messages.settings.models.proto.azure).toBe("Azure OpenAI");
    }
  });

  it("三种语言下，Azure 接入提示与 API Version 提示不出现 UPTIMIZE", () => {
    for (const messages of [messagesZh, messagesEn, messagesDe]) {
      expect(messages.settings.models.connect.proto_hint_azure).not.toContain("UPTIMIZE");
      expect(messages.settings.models.connect.azure_api_version_hint).not.toContain("UPTIMIZE");
    }
  });
});
