/**
 * Interface-language menu (Header, Language & Paging Spec §1, D1–D3).
 *
 * - One selection menu: "Automatic" (two lines) + the four languages in their own names and a fixed order, each with
 *   `lang`, a leading check on the saved choice.
 * - Switching **finishes saving `ui_language` before reload** (at most 2s): the Gateway's `/me` writes the stored
 *   value back into `NEXT_LOCALE`, so a reload racing the PUT would silently restore the old language.
 * - "Automatic" saves `ui_language=auto` and clears the cookie; the answer language is never written.
 */

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LanguageMenuButton } from "@/components/locale-switcher";
import type { UiLanguagePreference } from "@/lib/use-language-preferences";
import messagesFr from "@/messages/fr-FR.json";
import messagesZh from "@/messages/zh-CN.json";

const setUserSetting = vi.fn();
const setWorkspaceSetting = vi.fn();

vi.mock("@/lib/api", () => ({
  setUserSetting: (...args: unknown[]) => setUserSetting(...args),
  setWorkspaceSetting: (...args: unknown[]) => setWorkspaceSetting(...args),
}));
vi.mock("@/lib/use-space", () => ({ getActiveSpace: () => "ws_test" }));

const reload = vi.fn();

beforeEach(() => {
  setUserSetting.mockReset();
  setWorkspaceSetting.mockReset().mockResolvedValue(undefined);
  reload.mockReset();
  Object.defineProperty(window, "location", {
    value: { ...window.location, reload },
    writable: true,
    configurable: true,
  });
  document.cookie = "NEXT_LOCALE=; path=/; max-age=0";
  Object.defineProperty(navigator, "language", { value: "en-US", configurable: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function renderButton({
  locale = "zh-CN",
  saved = "zh-CN",
  beforeSwitch,
  messages = messagesZh,
}: {
  locale?: string;
  saved?: UiLanguagePreference;
  beforeSwitch?: () => Promise<boolean> | boolean;
  messages?: typeof messagesZh;
} = {}) {
  return render(
    <NextIntlClientProvider locale={locale} messages={messages}>
      <LanguageMenuButton saved={saved} beforeSwitch={beforeSwitch} />
    </NextIntlClientProvider>,
  );
}

function openMenu() {
  fireEvent.click(screen.getByTestId("interface-language-button"));
  return screen.getByTestId("interface-language-menu");
}

describe("options (D1)", () => {
  it("lists Automatic, then the four languages in their own names, fixed order, with lang and one check", () => {
    renderButton();
    const menu = openMenu();
    const options = within(menu).getAllByRole("menuitemradio");
    expect(options.map((option) => option.getAttribute("data-language-option"))).toEqual([
      "auto",
      "zh-CN",
      "en-US",
      "de-DE",
      "fr-FR",
    ]);
    expect(options.slice(1).map((option) => [option.textContent, option.getAttribute("lang")])).toEqual([
      ["中文", "zh-CN"],
      ["English", "en-US"],
      ["Deutsch", "de-DE"],
      ["Français", "fr-FR"],
    ]);
    // "Automatic" names what it resolves to right now (the browser language here).
    expect(options[0].textContent).toBe("自动跟随浏览器 · 当前为 English");
    expect(options.filter((option) => option.getAttribute("aria-checked") === "true")).toEqual([options[1]]);
    expect(within(menu).getByRole("separator")).toBeTruthy();
  });

  it("checks Automatic when the saved preference is auto, and names the trigger with the current interface language", () => {
    renderButton({ saved: "auto" });
    expect(screen.getByTestId("interface-language-button").textContent).toBe("中文");
    const options = within(openMenu()).getAllByRole("menuitemradio");
    expect(options[0].getAttribute("aria-checked")).toBe("true");
  });
});

describe("switching (D3)", () => {
  it("★ writes the cookie, waits for ui_language to save, then reloads once; never writes output_language", async () => {
    let settle: () => void = () => {};
    setUserSetting.mockImplementation(() => new Promise<void>((resolve) => { settle = resolve; }));
    renderButton();
    fireEvent.click(within(openMenu()).getByRole("menuitemradio", { name: "English" }));

    expect(document.cookie).toContain("NEXT_LOCALE=en-US");
    expect(setUserSetting).toHaveBeenCalledWith("ui_language", "en-US");
    // The trigger shows the pending state in place.
    expect(await screen.findAllByText("正在切换到 English…")).not.toHaveLength(0);
    expect(reload).not.toHaveBeenCalled();

    await act(async () => settle());
    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    expect(setUserSetting.mock.calls.map((call) => call[0])).toEqual(["ui_language"]);
  });

  it("reloads after at most two seconds when saving hangs", async () => {
    vi.useFakeTimers();
    setUserSetting.mockImplementation(() => new Promise<void>(() => {}));
    renderButton();
    fireEvent.click(within(openMenu()).getByRole("menuitemradio", { name: "Français" }));
    await vi.advanceTimersByTimeAsync(1900);
    expect(reload).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(200);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("Automatic saves ui_language=auto and clears the language cookie", async () => {
    setUserSetting.mockResolvedValue(undefined);
    document.cookie = "NEXT_LOCALE=zh-CN; path=/";
    renderButton();
    fireEvent.click(within(openMenu()).getAllByRole("menuitemradio")[0]);

    expect(document.cookie).not.toContain("NEXT_LOCALE=zh-CN");
    expect(setUserSetting).toHaveBeenCalledWith("ui_language", "auto");
    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });

  it("Automatic still takes effect when it is saved but a leftover NEXT_LOCALE cookie pins a language", async () => {
    setUserSetting.mockResolvedValue(undefined);
    document.cookie = "NEXT_LOCALE=en-US; path=/";
    renderButton({ saved: "auto" });
    fireEvent.click(within(openMenu()).getAllByRole("menuitemradio")[0]);
    expect(document.cookie).not.toContain("NEXT_LOCALE=en-US");
    expect(setUserSetting).toHaveBeenCalledWith("ui_language", "auto");
    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });

  it("choosing the saved option only closes the menu", async () => {
    renderButton({ saved: "zh-CN" });
    fireEvent.click(within(openMenu()).getByRole("menuitemradio", { name: "中文" }));
    await act(async () => {});
    expect(setUserSetting).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
    expect(screen.queryByTestId("interface-language-menu")).toBeNull();
  });

  it("does nothing when the page declines (unsaved edits kept)", async () => {
    const beforeSwitch = vi.fn().mockResolvedValue(false);
    renderButton({ beforeSwitch });
    fireEvent.click(within(openMenu()).getByRole("menuitemradio", { name: "English" }));
    await vi.waitFor(() => expect(beforeSwitch).toHaveBeenCalled());
    expect(setUserSetting).not.toHaveBeenCalled();
    expect(document.cookie).not.toContain("NEXT_LOCALE=en-US");
    expect(reload).not.toHaveBeenCalled();
  });
});

it("French interface: the trigger's accessible name comes from the French catalog", () => {
  render(
    <NextIntlClientProvider locale="fr-FR" messages={messagesFr}>
      <LanguageMenuButton saved="fr-FR" />
    </NextIntlClientProvider>,
  );
  const label = screen.getByTestId("interface-language-button").getAttribute("aria-label") ?? "";
  expect(label).not.toMatch(/\p{Script=Han}/u);
  expect(label).toBe("Langue de l’interface : Français");
});
