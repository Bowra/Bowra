/**
 * Settings › Account › Answer language (Header, Language & Paging Spec D2, §1b): writes `output_language` only,
 * lists allowed values (Deutsch only with the German-answer switch), saves at once with a Toast, and a team lock
 * disables the control with its reason kept on a second line.
 */

import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { resetLanguagePreferencesForTests } from "@/lib/use-language-preferences";
import { AnswerLanguageMenuButton } from "./answer-language-menu";

const getUserSettings = vi.fn();
const setUserSetting = vi.fn();
const apiJson = vi.fn();
const toast = vi.fn();
const session = vi.hoisted(() => ({ me: { capabilities: {} as Record<string, unknown> } }));

vi.mock("@/lib/api", () => ({
  getUserSettings: () => getUserSettings(),
  setUserSetting: (...args: unknown[]) => setUserSetting(...args),
}));
vi.mock("@/lib/api-client", () => ({ apiJson: (...args: unknown[]) => apiJson(...args) }));
vi.mock("@/lib/use-toast", () => ({ toast: (...args: unknown[]) => toast(...args) }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => session }));

function policy(value: unknown) {
  return { value: value === null ? null : JSON.stringify(value) };
}

beforeEach(() => {
  resetLanguagePreferencesForTests();
  toast.mockReset();
  getUserSettings.mockReset().mockResolvedValue({ output_language: "auto", ui_language: "zh-CN" });
  setUserSetting.mockReset().mockResolvedValue(undefined);
  apiJson.mockReset().mockResolvedValue(policy(null));
  session.me = { capabilities: {} };
});
afterEach(cleanup);

async function renderRow(locale: "zh-CN" | "en-US" = "zh-CN") {
  renderIntl(<AnswerLanguageMenuButton />, { locale });
  const button = screen.getByTestId("answer-language-button");
  await waitFor(() => expect(button.textContent).not.toBe("…"));
  return button;
}

describe("answer language row", () => {
  it("uses Title Case for the English option and sentence case for the selected value", async () => {
    const button = await renderRow("en-US");
    expect(button.textContent).toBe("Match my question");
    fireEvent.click(button);
    const option = screen.getByRole("menuitemradio", { name: /^Match My Question/ });
    expect(option.getAttribute("aria-checked")).toBe("true");
    expect(option.textContent).toContain("Answers in the language you ask in");
  });

  it("shows Match my question for an unset preference and saves a language immediately, without touching ui_language", async () => {
    const button = await renderRow();
    expect(button.textContent).toBe("跟随提问");
    fireEvent.click(button);
    const menu = screen.getByTestId("answer-language-menu");
    const options = within(menu).getAllByRole("menuitemradio");
    // German answers are off in this deployment: no Deutsch option.
    expect(options.map((option) => option.getAttribute("data-answer-option"))).toEqual(["auto", "zh-CN", "en-US", "fr-FR"]);
    expect(options[0].getAttribute("aria-checked")).toBe("true");

    await act(async () => {
      fireEvent.click(within(menu).getByRole("menuitemradio", { name: "English" }));
    });
    expect(setUserSetting).toHaveBeenCalledWith("output_language", "en-US");
    expect(setUserSetting).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(button.textContent).toBe("English"));
    expect(toast).toHaveBeenCalledWith({ kind: "success", text: "已更新回答语言" });
  });

  it("offers Deutsch while the German-answer switch is on", async () => {
    session.me = { capabilities: { german_ai_output: true } };
    const button = await renderRow();
    fireEvent.click(button);
    expect(within(screen.getByTestId("answer-language-menu")).getByRole("menuitemradio", { name: "Deutsch" })).toBeTruthy();
  });

  it("keeps an explicit follow_input preference checked as Match my question without rewriting it", async () => {
    getUserSettings.mockResolvedValue({ output_language: "follow_input" });
    const button = await renderRow();
    expect(button.textContent).toBe("跟随提问");
    fireEvent.click(button);
    const follow = within(screen.getByTestId("answer-language-menu")).getAllByRole("menuitemradio")[0];
    expect(follow.getAttribute("aria-checked")).toBe("true");
    await act(async () => {
      fireEvent.click(follow);
    });
    expect(setUserSetting).not.toHaveBeenCalled();
  });

  it("a team lock disables the control and keeps the reason visible", async () => {
    apiJson.mockResolvedValue(policy({ version: 1, defaults: { output_language: "en-US" }, locks: { output_language: true } }));
    getUserSettings.mockResolvedValue({ output_language: "zh-CN" });
    const button = await renderRow();
    await waitFor(() => expect(button.getAttribute("aria-disabled")).toBe("true"));
    expect(button.textContent).toBe("English");
    const reason = screen.getByTestId("answer-language-locked");
    expect(reason.textContent).toBe("团队已固定回答语言为 English，由团队负责人设置。");
    expect(button.getAttribute("aria-describedby")).toBe(reason.id);
    fireEvent.click(button);
    expect(screen.queryByTestId("answer-language-menu")).toBeNull();
  });
});
