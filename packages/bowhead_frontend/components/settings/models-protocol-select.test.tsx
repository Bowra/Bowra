/**
 * Model-connection protocol radios: switching filters model capabilities by protocol (anthropic removes embed/rerank).
 * Arrows only move focus; Space / Enter / click switches, otherwise passing through anthropic silently removes capabilities.
 */
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectPanel } from "@/components/settings/models";
import messagesZh from "@/messages/zh-CN.json";

afterEach(cleanup);

const DRAFT = {
  mode: "add" as const, protocol: "openai_compatible" as const, name: "", baseUrl: "", apiKey: "",
  messagesPath: "", apiVersion: "", authType: "bearer" as const, authHeaderName: "", authPrefix: "",
  customChatUrl: "", customEmbedUrl: "", customChatContract: "openai_chat_completions" as const,
  customChatQuery: "", customChatHeaders: "", customChatBody: "", customEmbedQuery: "",
  customEmbedHeaders: "", customEmbedBody: "",
  models: [{ id: "m1", caps: ["chat", "embed"], source: "manual" as const }],
  testing: false,
};

let last: typeof DRAFT = DRAFT;
function Stateful() {
  const [draft, setDraft] = useState<typeof DRAFT>(DRAFT);
  last = draft;
  return (
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <ConnectPanel
        t={((k: string) => k) as never}
        draft={draft as never}
        caps={["chat", "tool", "embed"]}
        busy={false} saveEnabled saveBlocked={null} testEnabled
        capLabel={(c: string) => c}
        onPatch={((p: Partial<typeof DRAFT>) => setDraft((d) => ({ ...d, ...p }))) as never}
        onToggleCap={vi.fn()} onTest={vi.fn()} onAutoFill={vi.fn()} discoveringModels={false}
        onSave={vi.fn()} onCancel={vi.fn()} v2 azureEnabled customHttpEnabled onTouch={vi.fn()}
      />
    </NextIntlClientProvider>
  );
}

describe("ConnectPanel 协议单选", () => {
  it("点击 Azure：embed 保留", () => {
    render(<Stateful />);
    fireEvent.click(screen.getByRole("radio", { name: /proto\.azure/ }));
    expect(last.protocol).toBe("azure_openai");
    expect(last.models[0].caps).toContain("embed");
  });

  it("方向键只移动焦点，不切协议，也不会途经 anthropic 删掉 embed", () => {
    render(<Stateful />);
    const openai = screen.getByRole("radio", { name: /proto\.openai/ });
    openai.focus();
    fireEvent.keyDown(openai, { key: "ArrowRight" });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    expect(last.protocol).toBe("openai_compatible");
    expect(last.models[0].caps).toEqual(["chat", "embed"]);
    // After moving to Azure, only Space/Enter (native button translates to click) switches.
    fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
    fireEvent.click(document.activeElement as HTMLElement);
    expect(last.protocol).toBe("azure_openai");
    expect(last.models[0].caps).toContain("embed");
  });
});
