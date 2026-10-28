/**
 * OpenAPI key cards: Edit, Disable/Enable, Regenerate and Revoke live in the card “⋯” menu; revocation and regeneration are confirmed first.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  chooseRowAction,
  expectConfirmGate,
  menuDangerNames,
  menuItemNames,
  openRowMenu,
} from "@/__tests__/row-actions";

const replaceMock = vi.fn();
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ replace: replaceMock }) }));

vi.mock("@/components/settings/team-settings-route", () => ({
  TeamSettingsRoute: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

import { NextIntlClientProvider } from "next-intl";
import enMessages from "@/messages/en-US.json";
import { withoutKeyFormPreset } from "@/lib/openapi-key-form";
import TeamOpenApiPage from "./page";

const KEY = {
  id: "oak_1", name: "BI 同步", token_prefix: "bh_live_7c1e", scopes: ["agents:read"],
  expires_at: null, status: "active", last_used_at: null,
};
const MENU = "BI 同步 的更多操作";
const REVOKE_TITLE = "注销密钥「BI 同步」？";
const ROTATE_TITLE = "重新生成密钥「BI 同步」？";
let fetchMock: ReturnType<typeof vi.fn>;
let keys: (typeof KEY)[];
const calls = (method: string, suffix: string) =>
  fetchMock.mock.calls.filter(([url, init]) => String(url).endsWith(suffix) && (init?.method ?? "GET") === method);
// Read live counts again when confirmation appears and after Cancel.
const liveCalls = (method: string, suffix: string) => ({ get mock() { return { calls: calls(method, suffix) }; } });
const ok = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));

beforeEach(() => {
  keys = [KEY];
  fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (String(url).endsWith("/keys")) return ok({ items: keys });
    if (init?.method === "DELETE") keys = [];
    if (String(url).endsWith("/rotate")) return ok({ token: "bh_live_new_token" });
    return ok({});
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  replaceMock.mockReset();
});

function dialogButton(dialog: HTMLElement, name: string): HTMLButtonElement {
  return within(dialog).getByRole("button", { name }) as HTMLButtonElement;
}

describe("OpenAPI 密钥：注销、重新生成前确认", () => {
  it("处理中：确认按钮不可点，连点只发一次注销", async () => {
    let finish: (r: Response) => void = () => {};
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/keys")) return ok({ items: [KEY] });
      if (init?.method === "DELETE") return new Promise<Response>((resolve) => { finish = resolve; });
      return ok({});
    });
    renderIntl(<TeamOpenApiPage />);
    await chooseRowAction(MENU, "注销…");
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(dialogButton(dialog, "注销"));
    await waitFor(() => expect(dialogButton(dialog, "注销").disabled).toBe(true));
    fireEvent.click(dialogButton(dialog, "注销"));
    expect(calls("DELETE", "/keys/oak_1")).toHaveLength(1);
    finish(new Response(JSON.stringify({}), { status: 200 }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("注销失败：确认框关闭，失败原因显示在页面上", async () => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/keys")) return ok({ items: [KEY] });
      if (init?.method === "DELETE") return Promise.resolve(new Response(JSON.stringify({ detail: "key_in_use" }), { status: 409 }));
      return ok({});
    });
    renderIntl(<TeamOpenApiPage />);
    await chooseRowAction(MENU, "注销…");
    fireEvent.click(dialogButton(await screen.findByRole("dialog"), "注销"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.body.textContent).toContain("没能完成这项操作");
    // Error codes belong only in collapsed diagnostics, not user-visible copy.
    expect(document.body.textContent).not.toContain("key_in_use");
    expect(screen.getByRole("button", { name: MENU })).toBeTruthy();
  });

  it.each([
    ["invalid_expiry", 422, "过期时间不能早于现在"],
    ["invalid_scopes", 422, "所选权限范围无效"],
    ["key_not_found", 404, "这把密钥已不存在"],
    ["openapi_unavailable", 503, "当前部署没有开放 OpenAPI"],
    ["something_new", 500, "没能完成这项操作"],
  ] as const)("注销失败（%s）：按错误码说清楚该怎么办，错误码与状态码不上页面", async (code, status, sentence) => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/keys")) return ok({ items: [KEY] });
      if (init?.method === "DELETE") {
        return Promise.resolve(new Response(JSON.stringify({ detail: { code } }), {
          status,
          headers: { "x-request-id": "0123456789abcdef0123456789abcdef" },
        }));
      }
      return ok({});
    });
    renderIntl(<TeamOpenApiPage />);
    await chooseRowAction(MENU, "注销…");
    fireEvent.click(dialogButton(await screen.findByRole("dialog"), "注销"));

    const alert = await screen.findByRole("alert");
    await waitFor(() => expect(alert.textContent).toContain(sentence));
    expect(document.body.textContent).not.toContain(code);
    expect(document.body.textContent).not.toMatch(new RegExp(`\\b${status}\\b`));
    // With diagnostics off, offer only Copy Diagnostics outside the announcement container.
    const copy = screen.getByTestId("error-copy-diagnostics");
    expect(alert.contains(copy)).toBe(false);
  });

  it("失效时间输入框不允许选早于现在的时间", async () => {
    renderIntl(<TeamOpenApiPage />);
    fireEvent.click(await screen.findByRole("button", { name: "增加密钥" }));
    const input = (await screen.findByLabelText("失效时间")) as HTMLInputElement;
    expect(input.min).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
    expect(new Date(input.min).getTime()).toBeLessThanOrEqual(Date.now());
    expect(Date.now() - new Date(input.min).getTime()).toBeLessThan(5 * 60_000);
  });

  it("creates a key with the expiry prefilled 30 days ahead, chosen scopes only, and a copyable one-time key", async () => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/keys") && init?.method === "POST") return ok({ token: "bh_live_created" });
      if (String(url).endsWith("/keys")) return ok({ items: keys });
      return ok({});
    });
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    renderIntl(<TeamOpenApiPage />);
    fireEvent.click(await screen.findByRole("button", { name: "增加密钥" }));
    const dialog = await screen.findByRole("dialog");
    // Nothing is preselected: the admin picks what the integration needs.
    expect(within(dialog).getAllByRole("checkbox").filter((box) => (box as HTMLInputElement).checked)).toHaveLength(0);
    fireEvent.change(within(dialog).getByLabelText("密钥名称"), { target: { value: "CI" } });
    fireEvent.click(within(dialog).getByText("agent_versions:write"));
    expect(within(dialog).getByText("留空时默认 30 天后失效。")).toBeTruthy();
    const expiry = within(dialog).getByLabelText("失效时间") as HTMLInputElement;
    const days = (new Date(expiry.value).getTime() - Date.now()) / 86_400_000;
    expect(days).toBeGreaterThan(29.9);
    expect(days).toBeLessThan(30.1);
    const create = dialogButton(dialog, "创建密钥");
    expect(create.disabled).toBe(false);
    fireEvent.click(create);

    await waitFor(() => expect(calls("POST", "/keys")).toHaveLength(1));
    const body = JSON.parse(String(calls("POST", "/keys")[0][1]?.body));
    expect(body).toEqual({ name: "CI", scopes: ["agent_versions:write"], permanent: false, expires_at: new Date(expiry.value).toISOString() });
    fireEvent.click(await screen.findByRole("button", { name: "复制密钥" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("bh_live_created"));
    expect(await screen.findByRole("button", { name: "已复制" })).toBeTruthy();
  });

  it("注销成功：卡片消失后焦点回到「现有密钥」标题，而不是掉到页面最外层", async () => {
    renderIntl(<TeamOpenApiPage />);
    await chooseRowAction(MENU, "注销…");
    fireEvent.click(dialogButton(await screen.findByRole("dialog"), "注销"));
    await waitFor(() => expect(screen.queryByText("BI 同步")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { name: "现有密钥" })));
  });

  it("确认框写明后果", async () => {
    renderIntl(<TeamOpenApiPage />);
    await chooseRowAction(MENU, "注销…");
    expect((await screen.findByRole("dialog")).textContent).toContain("立即无法调用");
  });
});

describe("OpenAPI 密钥：卡片「⋯」", () => {
  it("the card has no inline action buttons; the menu is Edit, Disable | Regenerate, Revoke (the last two are danger items)", async () => {
    renderIntl(<TeamOpenApiPage />);
    const menu = await openRowMenu(MENU);
    expect(menuItemNames(menu)).toEqual(["编辑", "禁用", "重新生成…", "注销…"]);
    expect(menuDangerNames(menu)).toEqual(["重新生成…", "注销…"]);
    expect(screen.queryByRole("button", { name: "重新生成" })).toBeNull();
  });

  it("the status badge and the “⋯” trigger share one right-aligned group", async () => {
    renderIntl(<TeamOpenApiPage />);
    const trigger = await screen.findByRole("button", { name: MENU });
    const group = trigger.closest(".ui-card-head-end");
    expect(group?.querySelector(".team-openapi-status")).toBeTruthy();
  });

  it("Disable calls the API directly (reversible, no confirmation); Edit opens the edit dialog", async () => {
    renderIntl(<TeamOpenApiPage />);
    await chooseRowAction(MENU, "禁用", { danger: false });
    await waitFor(() => expect(calls("POST", "/keys/oak_1/disable")).toHaveLength(1));
    await chooseRowAction(MENU, "编辑", { danger: false });
    expect(await screen.findByDisplayValue("BI 同步")).toBeTruthy();
    expect(calls("DELETE", "/keys/oak_1")).toHaveLength(0);
    expect(calls("POST", "/keys/oak_1/rotate")).toHaveLength(0);
  });

  it("Regenerate is a danger item: it states the old key stops working, and only Confirm regenerates", async () => {
    renderIntl(<TeamOpenApiPage />);
    await chooseRowAction(MENU, "重新生成…", { danger: true });
    expect((await screen.findByRole("dialog", { name: ROTATE_TITLE })).textContent).toContain("旧密钥立即失效");
    await expectConfirmGate({
      action: liveCalls("POST", "/keys/oak_1/rotate"),
      dialogName: ROTATE_TITLE,
      confirmName: "重新生成",
      reopen: () => chooseRowAction(MENU, "重新生成…"),
    });
    await waitFor(() => expect(calls("POST", "/keys/oak_1/rotate")).toHaveLength(1));
    expect(await screen.findByText("bh_live_new_token")).toBeTruthy();
  });

  it("Revoke is a danger item: Cancel does not revoke, only Confirm does", async () => {
    renderIntl(<TeamOpenApiPage />);
    await chooseRowAction(MENU, "注销…", { danger: true });
    await expectConfirmGate({
      action: liveCalls("DELETE", "/keys/oak_1"),
      dialogName: REVOKE_TITLE,
      confirmName: "注销",
      reopen: () => chooseRowAction(MENU, "注销…"),
    });
    await waitFor(() => expect(calls("DELETE", "/keys/oak_1")).toHaveLength(1));
  });
});

describe("OpenAPI keys: Agent CI", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("the Agent CI preset selects Agent versions read and write and names the key", async () => {
    renderIntl(<TeamOpenApiPage />);
    fireEvent.click(await screen.findByRole("button", { name: "增加密钥" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(dialogButton(dialog, "Agent CI 预设"));
    const checked = within(dialog).getAllByRole("checkbox").filter((box) => (box as HTMLInputElement).checked);
    expect(checked.map((box) => box.closest("label")?.querySelector("code")?.textContent)).toEqual(["agent_versions:read", "agent_versions:write"]);
    expect((within(dialog).getByLabelText("密钥名称") as HTMLInputElement).value).toBe("Agent CI");
  });

  it("starts with the preset and consumes it from the URL, with no way back to an Agent", async () => {
    window.history.replaceState(null, "", "/team/settings/openapi/?preset=agent_ci&return_to=%2Fagents%2Fmanage%2F");
    renderIntl(<TeamOpenApiPage />);
    const dialog = await screen.findByRole("dialog");
    expect((within(dialog).getByLabelText("密钥名称") as HTMLInputElement).value).toBe("Agent CI");
    expect(screen.queryByRole("link", { name: "返回智能体", hidden: true })).toBeNull();
    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(replaceMock.mock.calls[0][0]).toBe("/team/settings/openapi/?return_to=%2Fagents%2Fmanage%2F");
  });

  it("keeps a typed key name when the interface language changes", async () => {
    window.history.replaceState(null, "", "/team/settings/openapi/?preset=agent_ci");
    const view = renderIntl(<TeamOpenApiPage />);
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("密钥名称"), { target: { value: "Release pipeline" } });
    view.rerender(
      <NextIntlClientProvider locale="en-US" messages={enMessages}>
        <TeamOpenApiPage />
      </NextIntlClientProvider>,
    );
    const nameField = await screen.findByLabelText("Key name");
    expect((nameField as HTMLInputElement).value).toBe("Release pipeline");
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(replaceMock).toHaveBeenCalledTimes(1);
    expect(replaceMock.mock.calls[0][0]).toBe("/team/settings/openapi/");
  });

  it("drops only the preset from the query", () => {
    expect(withoutKeyFormPreset("?preset=agent_ci&return_to=%2Fagents%2F")).toBe("?return_to=%2Fagents%2F");
    expect(withoutKeyFormPreset("?preset=agent_ci")).toBe("");
  });

});
