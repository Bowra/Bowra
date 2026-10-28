/**
 * Model-settings dialog behavior for two confirmations (delete model source, rebuild index when changing vector model; component unification P1).
 * Formerly custom divs: no role, Esc could not close, Tab reached the background, and closing left focus on body.
 * CSS is class-scoped under `.mset`; mount in place (mount="inline") to preserve appearance.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

const llmConfigMocks = vi.hoisted(() => ({
  fetchAdminLlmConfig: vi.fn(),
  saveAdminLlmConfig: vi.fn(),
  validateAdminLlmConfig: vi.fn(),
  verifyDraftProvider: vi.fn(),
  verifyProvider: vi.fn(),
}));
vi.mock("@/lib/llm-config-api", () => llmConfigMocks);

const llmConfigV2Mocks = vi.hoisted(() => ({
  precheckProvider: vi.fn(),
  discoverProviderModels: vi.fn(),
  discoverDraftProviderModels: vi.fn(),
  createVerificationPlan: vi.fn(),
  fetchOutboundAdaptations: vi.fn().mockResolvedValue({ items: [] }),
  createVerificationBatch: vi.fn(),
  getVerificationBatch: vi.fn(),
  listVerificationBatches: vi.fn(),
  cancelVerificationBatch: vi.fn(),
  listVerificationEvidence: vi.fn(),
}));
vi.mock("@/lib/llm-config-v2-api", () => llmConfigV2Mocks);

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

import { SettingsModels } from "@/components/settings/models";
import messagesZh from "@/messages/zh-CN.json";
import { chooseRowAction, expectConfirmGate, menuItemNames, openRowMenu } from "@/__tests__/row-actions";

const m = messagesZh.settings.models;

const CONFIG = {
  schema_version: 2,
  providers: {
    qwen: {
      type: "openai_compatible",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      models: [
        { id: "qwen-plus", capabilities: ["chat", "tool"], source: "manual" },
        { id: "text-embedding-v3", capabilities: ["embed"], source: "manual" },
      ],
    },
  },
  tiers: {},
  kb: { models: {}, active: {}, pending_embed: null },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const MENU = "qwen 的更多操作";

/** Renders the page and returns the qwen card's row menu trigger (delete lives in that menu). */
async function renderPage() {
  llmConfigMocks.fetchAdminLlmConfig.mockResolvedValue({ config: CONFIG, version: 2, features: {} });
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <SettingsModels />
    </NextIntlClientProvider>,
  );
  return screen.findByRole("button", { name: MENU });
}

async function openDelete() {
  await chooseRowAction(MENU, `${m.common.delete}…`);
}

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("删除模型来源确认框", () => {
  it("是带标题名称与说明的模态弹窗，就地渲染在 .mset 里，外观 class 不变", async () => {
    await renderPage();
    await openDelete();
    const dialog = await screen.findByRole("dialog", { name: m.del.title.replace("{name}", "qwen") });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.className).toBe("mset-modal");
    // The panel sits in the shared dialog sheet host inside the scrim.
    expect(dialog.parentElement?.className).toBe("ui-dialog-sheet-host");
    expect(dialog.parentElement?.parentElement?.className).toBe("mset-scrim");
    expect(dialog.closest(".mset")).not.toBeNull();
    expect(document.getElementById(dialog.getAttribute("aria-describedby") ?? "")?.textContent).toBe(m.del.clean_body);
    expect(dialog.contains(document.activeElement)).toBe(true);
    // Confirmation action is in the footer (footer placement makes it solid red)
    const confirm = screen.getByRole("button", { name: new RegExp(messagesZh.settings.models.v4.delete_draft) });
    expect(confirm.closest(".modal-foot")).not.toBeNull();
  });

  it("Esc closes it and focus returns to the row menu trigger that opened it", async () => {
    const trigger = await renderPage();
    (document.activeElement as HTMLElement | null)?.blur();
    await openDelete();
    const dialog = await screen.findByRole("dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await flush();
    expect(document.activeElement).toBe(trigger);
  });

  it("点遮罩关闭，点面板不关", async () => {
    await renderPage();
    await openDelete();
    const dialog = await screen.findByRole("dialog");
    fireEvent.pointerDown(dialog);
    fireEvent.click(dialog);
    expect(screen.queryByRole("dialog")).not.toBeNull();
    const scrim = dialog.closest(".mset-scrim") as HTMLElement;
    fireEvent.pointerDown(scrim);
    fireEvent.click(scrim);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("换向量模型确认框", () => {
  async function openRebuild() {
    await renderPage();
    const select = screen.getByRole("combobox", { name: m.kb.vector });
    select.focus();
    fireEvent.change(select, { target: { value: "qwen::text-embedding-v3" } });
    return { select, dialog: await screen.findByRole("dialog", { name: m.kb.rebuild_title }) };
  }

  it("是带标题名称与说明的模态弹窗，焦点落在维度输入框", async () => {
    const { dialog } = await openRebuild();
    expect(dialog.className).toBe("mset-modal");
    expect(dialog.closest(".mset")).not.toBeNull();
    expect(document.getElementById(dialog.getAttribute("aria-describedby") ?? "")?.textContent).toBe(
      m.kb.rebuild_body.replace("{model}", "text-embedding-v3"),
    );
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: m.kb.dim_label }));
  });

  it("Esc 关闭并把焦点还给向量模型下拉框", async () => {
    const { select, dialog } = await openRebuild();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await flush();
    expect(document.activeElement).toBe(select);
  });

  it("填好维度后确认：暂存进草稿并关闭，焦点回到下拉框", async () => {
    const { select } = await openRebuild();
    const confirm = screen.getByRole("button", { name: new RegExp(m.v4.stage_embedding) }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox", { name: m.kb.dim_label }), { target: { value: "1024" } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await flush();
    expect(document.activeElement).toBe(select);
    expect((select as HTMLSelectElement).value).toBe("qwen::text-embedding-v3");
  });
});

describe("连接卡片「⋯」（外观段第四批）", () => {
  it("menu lists edit, delete; edit opens this source's editor", async () => {
    llmConfigMocks.fetchAdminLlmConfig.mockResolvedValue({ config: CONFIG, version: 2, features: {} });
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <SettingsModels />
      </NextIntlClientProvider>,
    );
    expect(screen.queryByRole("button", { name: `${m.common.delete} qwen` })).toBeNull();
    const menu = await openRowMenu(MENU);
    expect(menuItemNames(menu)).toEqual([m.common.edit, `${m.common.delete}…`]);
    fireEvent.click(screen.getByRole("menuitem", { name: m.common.edit }));
    expect(await screen.findByDisplayValue("https://dashscope.aliyuncs.com/compatible-mode/v1")).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: m.del.title.replace("{name}", "qwen") })).toBeNull();
  });

  it("delete is a danger item that confirms first; cancel keeps the source, confirm removes it", async () => {
    llmConfigMocks.fetchAdminLlmConfig.mockResolvedValue({ config: CONFIG, version: 2, features: {} });
    render(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <SettingsModels />
      </NextIntlClientProvider>,
    );
    await chooseRowAction(MENU, `${m.common.delete}…`, { danger: true });
    const removed = { mock: { calls: [] as unknown[][] } };
    await expectConfirmGate({
      action: removed,
      dialogName: m.del.title.replace("{name}", "qwen"),
      confirmName: new RegExp(messagesZh.settings.models.v4.delete_draft),
      cancelName: m.common.cancel,
      reopen: async () => {
        expect(screen.getByRole("button", { name: MENU })).toBeTruthy();
        await chooseRowAction(MENU, `${m.common.delete}…`);
      },
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: MENU })).toBeNull());
  });
});
