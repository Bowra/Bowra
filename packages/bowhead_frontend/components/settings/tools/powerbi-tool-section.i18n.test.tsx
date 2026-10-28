/**
 * i18n batch 3B: Power BI connection settings use the UI language (messages `powerbi_tool`).
 * zh-CN matches the original verbatim; en-US / de-DE contain no Han characters.
 */
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { Locale } from "@/lib/locale";

vi.mock("@/lib/team-builtin-tools-api", () => ({
  createPowerBiTemplate: vi.fn(),
  createPowerBiTool: vi.fn(),
  deletePowerBiTemplate: vi.fn(),
  deletePowerBiTool: vi.fn(),
  listPowerBiTemplates: vi.fn(async () => []),
  setPowerBiEnabled: vi.fn(),
  updatePowerBiTool: vi.fn(),
}));

import { PowerBiToolSection } from "./powerbi-tool-section";
import deMessages from "@/messages/de-DE.json";
import enMessages from "@/messages/en-US.json";
import frMessages from "@/messages/fr-FR.json";

type PowerBiCopy = { edit: string; dax_templates: string; delete: string; disable: string; edit_title: string; intro: string; secret_keep_placeholder: string };

/** Expected row actions (menu items, then the enable toggle) and dialog copy straight from a translated catalog. */
function fromCatalog(copy: PowerBiCopy) {
  return {
    buttons: [copy.edit, copy.dax_templates, copy.delete, copy.disable],
    title: copy.edit_title,
    intro: copy.intro,
    secret: copy.secret_keep_placeholder,
  };
}

const HAN = /\p{Script=Han}/u;
const LOCALES: Locale[] = ["zh-CN", "en-US", "de-DE", "fr-FR"];

afterEach(cleanup);

function expectNoHanOutsideZh(locale: Locale, values: string[]) {
  if (locale === "zh-CN") return;
  for (const v of values) expect(v, `${locale}: ${v}`).not.toMatch(HAN);
}

describe("Power BI 连接设置：按界面语言渲染", () => {
  const TOOLS = [
    { id: "t1", name: "n1", display_name: "PBI", status: "enabled", workspace_id: "w", dataset_id: "d", key_hint: null, tenant_id: "x", client_id: "y" },
  ];

  it.each(LOCALES)("%s：Power BI 连接行与编辑弹窗", (locale) => {
    renderIntl(
      <PowerBiToolSection tools={TOOLS as never} createRequest={0} onChanged={async () => {}} />,
      { locale },
    );
    const buttons = screen.getAllByRole("button");
    const more = buttons[0];
    const toggle = buttons[buttons.length - 1];
    fireEvent.click(more);
    const menu = screen.getByRole("menu");
    const items = within(menu).getAllByRole("menuitem");
    // Delete confirms first, so its menu label carries an ellipsis.
    expect(items[2].textContent).toMatch(/…$/);
    const rowActions = [...items.map((item) => item.textContent?.trim().replace(/…$/, "")), toggle.textContent];
    const moreLabel = more.getAttribute("aria-label") ?? "";
    fireEvent.click(items[0]);
    const dialog = document.body.textContent ?? "";
    const secret = (document.getElementById("powerbi-tool-secret") as HTMLInputElement).placeholder;
    const want = {
      "zh-CN": { buttons: ["编辑", "DAX 模板", "删除", "停用"], title: "编辑 Power BI", intro: "使用 Microsoft Entra 服务主体连接 Power BI 语义模型。", secret: "留空则保留现有密钥" },
      "en-US": { buttons: ["Edit", "DAX Templates", "Delete", "Disable"], title: "Edit Power BI", intro: "Connect a Power BI semantic model with a Microsoft Entra service principal.", secret: "Leave blank to keep current secret" },
      "de-DE": fromCatalog(deMessages.powerbi_tool),
      "fr-FR": fromCatalog(frMessages.powerbi_tool),
    }[locale];
    expect(rowActions).toEqual(want.buttons);
    expect(dialog).toContain(want.title);
    expect(dialog).toContain(want.intro);
    expect(secret).toBe(want.secret);
    expectNoHanOutsideZh(locale, [dialog, secret, moreLabel]);
  });

});

describe("Power BI errors", () => {
  it("shows catalog copy for coded failures and never a raw code", async () => {
    const api = await import("@/lib/team-builtin-tools-api");
    const copy = "A Power BI connection with this name already exists. Choose a different name.";
    // checked() rethrows readApiError's localized message with the code attached.
    vi.mocked(api.setPowerBiEnabled)
      .mockRejectedValueOnce(Object.assign(new Error(copy), { status: 409, code: "powerbi_name_conflict" }))
      .mockRejectedValueOnce(Object.assign(new Error("powerbi_tool_not_found"), { status: 404 }));
    const tools = [{ id: "t1", name: "n1", display_name: "PBI", status: "enabled", workspace_id: "w", dataset_id: "d", key_hint: null, tenant_id: "x", client_id: "y" }];
    renderIntl(<PowerBiToolSection tools={tools as never} createRequest={0} onChanged={async () => {}} />, { locale: "en-US" });
    const buttons = screen.getAllByRole("button");
    const toggle = buttons[buttons.length - 1];
    fireEvent.click(toggle);
    expect(await screen.findByText(copy)).toBeTruthy();
    fireEvent.click(toggle);
    await vi.waitFor(() => expect(api.setPowerBiEnabled).toHaveBeenCalledTimes(2));
    // An uncoded failure falls back to the generic copy, never the raw code.
    const { genericUserFacingErrorMessage } = await import("@/lib/user-facing-error");
    expect(await screen.findByText(genericUserFacingErrorMessage())).toBeTruthy();
    expect(document.body.textContent).not.toContain("powerbi_tool_not_found");
  });
});

describe("translated Power BI copy is not an English placeholder", () => {
  it.each([
    ["de-DE", deMessages.powerbi_tool],
    ["fr-FR", frMessages.powerbi_tool],
  ] as const)("%s expected strings differ from en-US", (_locale, copy) => {
    const en = fromCatalog(enMessages.powerbi_tool);
    const own = fromCatalog(copy);
    own.buttons.forEach((label, i) => expect(label).not.toBe(en.buttons[i]));
    expect(own.title).not.toBe(en.title);
    expect(own.intro).not.toBe(en.intro);
    expect(own.secret).not.toBe(en.secret);
  });
});
