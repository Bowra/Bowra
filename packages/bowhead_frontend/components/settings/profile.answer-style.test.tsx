/**
 * Answer Style is a **server setting**: Settings must PUT it to user-settings so chat can read it.
 *
 * Every selectable answer style must be accepted by the user-settings API. An unchanged default
 * sends no request and shows no Save button, so verify a nondefault choice reaches the server.
 *
 * Lock the frontend half of the integration: ① select and save a nondefault value → actually send an `answer_style` write request;
 * ② opening the page displays the **server** value (not localStorage).
 */
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const apiVoid = vi.fn();
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api-client")>(),
  apiVoid: (...args: unknown[]) => apiVoid(...args),
}));
const fetchMe = vi.fn();
const resend = vi.fn();
const getUserSettings = vi.fn();
const setUserSetting = vi.fn();

vi.mock("@/lib/auth-api", () => ({
  fetchMe: () => fetchMe(),
  resendVerificationEmail: vi.fn(),
  resendCurrentUserVerificationEmail: () => resend(),
  AuthError: class extends Error {},
}));
vi.mock("@/lib/api", () => ({
  getUserSettings: () => getUserSettings(),
  setUserSetting: (key: string, value: string) => setUserSetting(key, value),
  setWorkspaceSetting: vi.fn(),
}));
vi.mock("@/lib/use-space", () => ({ getActiveSpace: () => "ws_test" }));

import { resetLanguagePreferencesForTests } from "@/lib/use-language-preferences";

import { SettingsProfile } from "./profile";

const me = {
  id: "user_1",
  email: "user@example.com",
  display_name: "User",
  avatar_url: null,
  team_id: "team_1",
  tier: "pro",
  role: "member",
  status: "active",
  email_verified: true,
  has_password: true,
  oauth_providers: [],
  credit_balance: 800,
  credit_quota: 1000,
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  is_team_admin: false,
  workspace_count: 1,
  needs_onboarding: false,
};

beforeEach(() => {
  apiVoid.mockResolvedValue(undefined);
  fetchMe.mockResolvedValue(me);
  getUserSettings.mockResolvedValue({});
  setUserSetting.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const STYLE_LABELS = { concise: "简洁", detailed: "详细（默认）", technical: "技术", business: "业务" } as const;

/** Answer style is a menu button (like the language rows), named by its row label and current value. */
async function chooseStyle(style: keyof typeof STYLE_LABELS) {
  const button = await screen.findByTestId("answer-style-button");
  expect(button).toHaveAccessibleName(expect.stringContaining("答案风格"));
  fireEvent.click(button);
  const menu = await screen.findByTestId("answer-style-button-menu");
  fireEvent.click(within(menu).getByRole("menuitemradio", { name: STYLE_LABELS[style] }));
}

describe("个人设置 · 答案风格", () => {
  it("保存时把选中的风格写进服务端设置", async () => {
    renderIntl(<SettingsProfile />);

    await screen.findByTestId("answer-style-button");
    await chooseStyle("concise");

    // When dirty, the save bar renders the page's single Save button.
    const saveButtons = await screen.findAllByRole("button", { name: "保存" });
    fireEvent.click(saveButtons[saveButtons.length - 1]);

    await waitFor(() =>
      expect(setUserSetting).toHaveBeenCalledWith("answer_style", "concise"),
    );
  });

  it("打开页面时回显服务端已存的风格", async () => {
    getUserSettings.mockResolvedValue({ answer_style: "business" });

    renderIntl(<SettingsProfile />);

    await waitFor(() =>
      expect(screen.getByTestId("answer-style-button")).toHaveTextContent("业务"),
    );
  });
});


describe("个人设置 · 重发验证邮件", () => {
  it("已验证邮箱打开设置即显示已验证，无需重发", async () => {
    renderIntl(<SettingsProfile />);
    expect(await screen.findByText("已验证")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "重发验证邮件" })).toBeNull();
    expect(resend).not.toHaveBeenCalled();
  });

  it("shows sending progress and keeps the confirmation visible", async () => {
    fetchMe.mockResolvedValue({ ...me, email_verified: false });
    let finish!: (value: { ok: boolean }) => void;
    resend.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    renderIntl(<SettingsProfile />);
    fireEvent.click(await screen.findByRole("button", { name: "重发验证邮件" }));
    expect(screen.getByRole("button", { name: "正在发送…" })).toBeDisabled();
    finish({ ok: true });
    expect(await screen.findByText(/已提交发送验证邮件/)).toBeTruthy();
    expect(resend).toHaveBeenCalledOnce();
  });

  it("shows a send failure instead of a success confirmation", async () => {
    fetchMe.mockResolvedValue({ ...me, email_verified: false });
    resend.mockRejectedValueOnce(new Error("邮件服务不可用"));
    renderIntl(<SettingsProfile />);
    fireEvent.click(await screen.findByRole("button", { name: "重发验证邮件" }));
    expect(await screen.findByText(/未能发送验证邮件/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "重发验证邮件" })).toBeEnabled();
  });
});

describe("Profile settings: grouped cards and save bar", () => {
  it("groups rows into four titled cards and saves edits through the single save bar", async () => {
    const { container } = renderIntl(<SettingsProfile />);
    await screen.findByTestId("answer-style-button");
    expect(container.querySelectorAll("section.ui-group > .ui-group-card")).toHaveLength(4);
    expect(container.querySelector(".legacy-profile-section")).toBeNull();
    expect(screen.queryByRole("region", { name: "有未保存的更改" })).toBeNull();

    await chooseStyle("concise");
    const bar = await screen.findByRole("region", { name: "有未保存的更改" });
    // No inline Save: the page has just the save bar's single Save button.
    expect(screen.getAllByRole("button", { name: "保存" })).toHaveLength(1);
    fireEvent.click(within(bar).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(setUserSetting).toHaveBeenCalledWith("answer_style", "concise"));
  });

  it("「放弃」把显示名称与答案风格恢复到已保存的值，保存栏消失、不发请求", async () => {
    renderIntl(<SettingsProfile />);
    const name = await screen.findByLabelText("显示名称");
    fireEvent.change(name, { target: { value: "新名字" } });
    await chooseStyle("technical");
    const bar = await screen.findByRole("region", { name: "有未保存的更改" });
    fireEvent.click(within(bar).getByRole("button", { name: "放弃" }));
    expect(name).toHaveValue("User");
    expect(screen.getByTestId("answer-style-button")).toHaveTextContent("详细（默认）");
    expect(screen.queryByRole("region", { name: "有未保存的更改" })).toBeNull();
    expect(setUserSetting).not.toHaveBeenCalled();
  });
});

// Header, Language & Paging Spec §1b (D2, D3): two rows under General preferences — Interface language (menu
// button, reloads once) and Answer language (output_language, saves at once). With unsaved edits in the save bar,
// switching the interface language asks first because the reload would drop them.
describe("个人设置 · 界面语言与回答语言", () => {
  beforeEach(() => {
    resetLanguagePreferencesForTests();
    getUserSettings.mockResolvedValue({ ui_language: "zh-CN", output_language: "auto" });
  });

  it("shows both rows with their hints, the answer row as a deep-link target", async () => {
    renderIntl(<SettingsProfile />);
    const interfaceButton = await screen.findByTestId("interface-language-button");
    expect(interfaceButton).toHaveAccessibleName("界面语言 中文");
    expect(screen.getByText("按钮、菜单和页面文字。选择后页面刷新一次。")).toBeInTheDocument();
    const answerRow = document.getElementById("answer-language")!;
    expect(within(answerRow).getByText("回答语言")).toBeInTheDocument();
    expect(within(answerRow).getByText("AI 回答使用的语言，与界面语言分开设置。")).toBeInTheDocument();
    await waitFor(() => expect(within(answerRow).getByTestId("answer-language-button")).toHaveTextContent("跟随提问"));
    // The old cycling "中/En/De/Fr" text button and its wrong tooltip are gone.
    expect(screen.queryByText("界面 + 输出语言")).toBeNull();
    expect(document.querySelector(".v2-topbar-locale")).toBeNull();
  });

  it("confirms discarding unsaved edits before switching the interface language", async () => {
    renderIntl(<SettingsProfile />);
    await chooseStyle("concise");
    fireEvent.click(await screen.findByTestId("interface-language-button"));
    fireEvent.click(within(screen.getByTestId("interface-language-menu")).getByRole("menuitemradio", { name: "English" }));

    const dialog = await screen.findByRole("dialog", { name: "放弃未保存的更改？" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(setUserSetting).not.toHaveBeenCalledWith("ui_language", expect.anything());
  });
});


describe("profile partial saves", () => {
  it("keeps a confirmed name change when the answer-style update fails", async () => {
    setUserSetting.mockRejectedValueOnce(new Error("private response"));
    renderIntl(<SettingsProfile />);
    fireEvent.change(await screen.findByLabelText("显示名称"), { target: { value: "Updated" } });
    await chooseStyle("concise");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText(/姓名已保存。.*回答风格的保存结果尚未确认/)).toBeTruthy();
    expect(screen.getByLabelText("显示名称")).toHaveValue("Updated");
    expect(screen.queryByText(/private response/)).toBeNull();
    // A subsequent save only repeats the unresolved setting, not the acknowledged name write.
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(setUserSetting).toHaveBeenCalledTimes(2));
    expect(apiVoid).toHaveBeenCalledOnce();
  });
});

describe("Profile settings: after Coach edits", () => {
  it("saves the choice at once, outside the profile save bar", async () => {
    renderIntl(<SettingsProfile />);
    const button = await screen.findByTestId("coach-after-edit-button");
    await waitFor(() => expect(button).toBeEnabled());
    expect(button.textContent).toContain("自动检查");
    fireEvent.click(button);
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "先问我" }));
    await waitFor(() => expect(setUserSetting).toHaveBeenCalledWith("agent_coach_after_edit", "ask"));
    expect(screen.queryByRole("region", { name: "有未保存的更改" })).toBeNull();
  });
});
