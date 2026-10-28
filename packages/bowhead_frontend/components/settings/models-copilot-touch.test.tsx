/** Batch 1-d R3: manual-edit protection **call-site** guard (tech-design-batch1d §12).
 *
 * In r2 I inferred manual edits from patch keys, marking internal linked changes kept: clicking protocol button
 * produces linked models in patch (azure also auto apiVersion), leaving copilot **permanently** unable to fill
 * both fields, entirely silently. Pure-function guards cannot catch this: render real panel, click real button, observe onTouch.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectPanel, modelRequestBodyOverridesValid } from "@/components/settings/models";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

afterEach(cleanup);

const DRAFT = {
  mode: "add" as const,
  protocol: "openai_compatible" as const,
  name: "",
  baseUrl: "",
  apiKey: "",
  messagesPath: "",
  apiVersion: "",
  authType: "bearer" as const,
  authHeaderName: "",
  authPrefix: "",
  customChatUrl: "",
  customEmbedUrl: "",
  customChatContract: "openai_chat_completions" as const,
  customChatQuery: "",
  customChatHeaders: "",
  customChatBody: "",
  customEmbedQuery: "",
  customEmbedHeaders: "",
  customEmbedBody: "",
  models: [{ id: "m1", caps: ["chat"], source: "manual" as const }],
  testing: false,
};

function renderPanel(overrides: Record<string, unknown> = {}, panelProps: Record<string, unknown> = {}) {
  const onTouch = vi.fn();
  const onPatch = vi.fn();
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <ConnectPanel
        t={((k: string) => k) as never}
        draft={{ ...DRAFT, ...overrides } as never}
        caps={["chat", "tool"]}
        busy={false}
        saveEnabled
        saveBlocked={null}
        testEnabled
        capLabel={(c: string) => c}
        onPatch={onPatch}
        onToggleCap={vi.fn()}
        onTest={vi.fn()}
        onAutoFill={vi.fn()}
        discoveringModels={false}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        v2
        azureEnabled
        customHttpEnabled
        onTouch={onTouch}
        {...panelProps}
      />
    </NextIntlClientProvider>,
  );
  return { onTouch, onPatch };
}

describe("手改保护的使用点（R3 review P1-1）", () => {
  it("★切协议只标 protocol——patch 里联动算出的 models/apiVersion 不算用户手改", () => {
    const { onTouch, onPatch } = renderPanel();
    fireEvent.click(screen.getByRole("radio", { name: /proto\.anthropic/ }));
    expect(onTouch).toHaveBeenCalledWith("protocol");
    // Inference implementation would also mark models here (patch does contain it) → copilot can never fill models again.
    expect(onTouch).not.toHaveBeenCalledWith("models");
    expect(onPatch.mock.calls[0][0]).toHaveProperty("models"); // Confirm patch really contains models, preventing vacuous assertion.
  });

  it("★切到 azure 也只标 protocol（patch 带自动 apiVersion）", () => {
    const { onTouch, onPatch } = renderPanel();
    fireEvent.click(screen.getByRole("radio", { name: /proto\.azure/ }));
    expect(onTouch).toHaveBeenCalledWith("protocol");
    expect(onTouch).not.toHaveBeenCalledWith("apiVersion");
    expect(onTouch).not.toHaveBeenCalledWith("models");
    expect(onPatch.mock.calls[0][0]).toHaveProperty("apiVersion");
  });

  it("真的手改名称/地址 → 标对应字段", () => {
    const { onTouch } = renderPanel();
    fireEvent.change(document.querySelector("input[name='provider_name']")!, { target: { value: "x" } });
    expect(onTouch).toHaveBeenCalledWith("name");
    fireEvent.change(document.querySelector("input[name='provider_base_url']")!, { target: { value: "https://x/v1" } });
    expect(onTouch).toHaveBeenCalledWith("baseUrl");
  });

  it("改模型行 → 标 models", () => {
    const { onTouch } = renderPanel();
    const rowId = document.querySelector("input[name='provider_model_0']");
    fireEvent.change(rowId!, { target: { value: "qwen-plus" } });
    expect(onTouch).toHaveBeenCalledWith("models");
  });

  it("apiKey 不标——它是 secret，副驾从不产出也就无所谓保护", () => {
    const { onTouch } = renderPanel();
    const key = document.querySelector("input[name='provider_api_key']");
    if (key) fireEvent.change(key, { target: { value: "sk-x" } });
    expect(onTouch).not.toHaveBeenCalledWith("apiKey");
  });
});

describe("引用智能体所需的模型窗口", () => {
  it.each(["openai_compatible", "anthropic_compatible"])("%s 连接可填写模型上下文窗口并随草稿保存", (protocol) => {
    const { onPatch } = renderPanel({ protocol });
    const field = screen.getByRole("spinbutton", { name: "connect.context_window_label" });
    fireEvent.change(field, { target: { value: "1000000" } });
    expect(onPatch).toHaveBeenCalledWith(expect.objectContaining({
      models: [expect.objectContaining({ id: "m1", contextWindow: 1000000 })],
    }));
    onPatch.mockClear();
    fireEvent.change(field, { target: { value: "-1" } });
    expect(onPatch).not.toHaveBeenCalled();
  });
});

describe("overrides 渲染门（R3 review P2-3：此前只有落库门那半有闸）", () => {
  // 2026-08-19: raw JSON escape hatch moved one disclosure level deeper in “出站参数” (design ④D).
  // Rendering gate unchanged; criterion shifts from summary wording to **that textarea’s existence**:
  // it is actual escape hatch, aria-label retains same i18n key (capability moved, not lost).
  const rawEditor = () =>
    document.querySelector("textarea[name='provider_model_overrides_0']");

  it("anthropic 面不渲染 overrides 编辑器——它的 adapter 永不消费", () => {
    renderPanel({ protocol: "anthropic_compatible" });
    expect(rawEditor()).toBeNull();
  });

  it("openai_compatible 面渲染——qwen 的 enable_thinking 正是这个场景", () => {
    renderPanel({ protocol: "openai_compatible" });
    expect(rawEditor()).not.toBeNull();
  });

  it("★ 逃生口收在「出站参数」里，但仍然可达（不是被藏没了）", () => {
    renderPanel({ protocol: "openai_compatible" });

    // Use structure rather than i18n wording (stub echoes keys, namespace mismatch):
    // outbound-parameter section exists, raw JSON editor **inside it**.
    const section = screen.getByTestId("outbound-params-0");
    expect(section).not.toBeNull();
    expect(section.querySelector("textarea[name='provider_model_overrides_0']")).not.toBeNull();
    // Each of four typed-parameter rows has a mode select.
    expect(section.querySelectorAll("select").length).toBe(4);
  });
});

describe("非法 overrides 必须挡住保存（批 1-d 收尾）", () => {
  it("JSON 打错 → 保存禁用，而不是保存成功但静默丢掉", () => {
    // Parent computes saveEnabled; guard criterion directly here: newly added saveEnabled conjunct.
    expect(modelRequestBodyOverridesValid("{ bad json", ["chat"])).toBe(false);
    expect(modelRequestBodyOverridesValid('{"chat":{"enable_thinking":false}}', ["chat"])).toBe(true);
    // Keys must be declared model capabilities, otherwise never match at runtime.
    expect(modelRequestBodyOverridesValid('{"embed":{"x":1}}', ["chat"])).toBe(false);
    expect(modelRequestBodyOverridesValid("{}", ["chat"])).toBe(true);
  });
});

describe("跑批期间不吞输入（批 1-d 收尾）", () => {
  it("busy 时表单控件全禁用——此前 patchConnect no-op 而输入框还能打字，值被静默吞掉", () => {
    renderPanel({}, { busy: true });
    const name = document.querySelector("input[name='provider_name']") as HTMLInputElement;
    // jsdom `.disabled` reflects own attribute only, no fieldset inheritance; `:disabled` pseudo-class includes inheritance.
    expect(name.matches(":disabled")).toBe(true);
    expect(document.querySelector("fieldset.connect-body")?.hasAttribute("disabled")).toBe(true);
  });

  it("非 busy 时照常可编辑", () => {
    const { onPatch } = renderPanel();
    const name = document.querySelector("input[name='provider_name']") as HTMLInputElement;
    expect(name.matches(":disabled")).toBe(false);
    fireEvent.change(name, { target: { value: "x" } });
    expect(onPatch).toHaveBeenCalled();
  });
});

describe("保存键为灰时，理由长在按钮旁边", () => {
  it("★ 理由渲染出来，并且无障碍地绑在按钮上", () => {
    // Correct criterion left unwired to UI repeatedly fails here: gate disabled, user cannot tell why.
    renderPanel({}, { saveBlocked: "capability_missing", saveEnabled: false });

    const reason = document.getElementById("connect-save-blocked");
    expect(reason?.textContent).toContain("connect.blocked_capability_missing");

    const save = [...document.querySelectorAll("button")]
      .find((b) => b.textContent?.includes("common.save"));
    expect(save?.getAttribute("aria-describedby")).toBe("connect-save-blocked");
    expect(save?.disabled).toBe(true);
  });

  it("负对照：可保存时不留空壳", () => {
    renderPanel({}, { saveBlocked: null, saveEnabled: true });

    expect(document.getElementById("connect-save-blocked")).toBeNull();
  });
});

describe("必填星号", () => {
  /** Asterisks must correspond one-to-one with actual blocking: marking without blocking or blocking without marking misleads. */
  const starOn = (labelFor: string) =>
    !!document.querySelector(`label[for="${labelFor}"] .star`);

  it("★ 来源标识必填 —— 用户就是把它留空、页面一个字不说才卡住的", () => {
    renderPanel();

    expect(starOn("mset-provider-name")).toBe(true);
    expect(document.querySelector('input[name="provider_name"]')?.getAttribute("aria-required"))
      .toBe("true");
  });

  it("base_url 必填", () => {
    renderPanel();

    expect(starOn("mset-provider-base-url")).toBe(true);
  });

  it("★ 负对照：custom_http 的 base_url 可留空（模板可写全 URL），就不许标星", () => {
    renderPanel({ protocol: "custom_http" });

    // First prove label still exists; otherwise missing asterisk merely means entire field absent, assertion vacuous.
    expect(document.querySelector('label[for="mset-provider-base-url"]')).not.toBeNull();
    expect(starOn("mset-provider-base-url")).toBe(false);
  });

  it("负对照：密钥不标星 —— 保存闸不要求它（只有「验证计划」要）", () => {
    renderPanel();

    // Likewise: prove key field actually present first.
    expect(document.querySelector('label[for="mset-provider-api-key"]')).not.toBeNull();
    expect(starOn("mset-provider-api-key")).toBe(false);
  });
});

describe("地址提示跟着部署模式走", () => {
  // Harness `t` echoes keys, so assertion checks **which key is used** (wiring),
  // while cross-language assertion below guarantees actual wording exists; both needed:
  // key alone renders missing wording as key itself; wording alone may coincidentally pass despite wrong wiring.
  const hint = () =>
    document.querySelector('label[for="mset-provider-base-url"]')
      ?.parentElement?.querySelector(".hint")?.textContent ?? "";

  it("★ local（私有化部署的默认）用 local 那句", () => {
    renderPanel({}, { deploymentMode: "local" });

    expect(hint()).toContain("connect.base_url_scope_local");
  });

  it("★ saas 用 saas 那句", () => {
    renderPanel({}, { deploymentMode: "saas" });

    expect(hint()).toContain("connect.base_url_scope_saas");
  });

  it("★ 模式未知时什么都不说——猜错的那一半就是在骗用户", () => {
    renderPanel({});

    // Positive control: path half remains, proving hint really rendered.
    expect(hint()).toContain("connect.base_url_hint");
    expect(hint()).not.toContain("base_url_scope");
  });

  it("两种语言都得有话可说，否则界面上印的是 key 本身", () => {
    for (const messages of [messagesZh, messagesEn]) {
      const connect = (messages as Record<string, any>).settings.models.connect;
      expect(connect.base_url_scope_local).toBeTruthy();
      expect(connect.base_url_scope_saas).toBeTruthy();
      // saas sentence must clearly attribute decision to deployment rather than user error.
      expect(`${connect.base_url_scope_saas}`.length).toBeGreaterThan(10);
    }
  });
});
