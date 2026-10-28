import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";
import { SecurityError } from "@/lib/security-api";

const fetchMe = vi.fn();
const changePassword = vi.fn();
const getUserSettings = vi.fn();
const setUserSetting = vi.fn();
const fetchActiveSessions = vi.fn();
getUserSettings.mockResolvedValue({});
fetchActiveSessions.mockResolvedValue({ items: [], current_identified: true });

vi.mock("@/lib/auth-api", () => ({
  fetchMe: () => fetchMe(),
  logoutEverywhere: vi.fn(),
  resendVerificationEmail: vi.fn(),
}));
vi.mock("@/lib/security-api", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("@/lib/security-api")
  >();
  return {
    ...actual,
    changePassword: (payload: unknown) => changePassword(payload),
    fetchActiveSessions: () => fetchActiveSessions(),
  };
});
vi.mock("@/lib/api", () => ({
  getUserSettings: () => getUserSettings(),
  setUserSetting: (key: string, value: string) => setUserSetting(key, value),
}));

import { SettingsSecurity } from "./security";

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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  getUserSettings.mockResolvedValue({});
  fetchActiveSessions.mockResolvedValue({ items: [], current_identified: true });
});

describe("SettingsSecurity password change", () => {
  it("starts collapsed and focuses the current password when expanded", async () => {
    fetchMe.mockResolvedValue(me);

    renderIntl(<SettingsSecurity />);

    const trigger = await screen.findByRole("button", {
      name: "修改密码",
      expanded: false,
    });
    expect(screen.queryByLabelText("当前密码")).not.toBeInTheDocument();

    fireEvent.click(trigger);

    const currentPassword = await screen.findByLabelText("当前密码");
    const panelId = trigger.getAttribute("aria-controls");
    expect(panelId).toBeTruthy();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(currentPassword.closest("[role=region]")).toHaveAttribute("id", panelId);
    await waitFor(() => expect(currentPassword).toHaveFocus());
  });

  it("cancels with cleared fields and returns focus to the trigger", async () => {
    fetchMe.mockResolvedValue(me);

    renderIntl(<SettingsSecurity />);

    const trigger = await screen.findByRole("button", {
      name: "修改密码",
      expanded: false,
    });
    fireEvent.click(trigger);
    fireEvent.change(await screen.findByLabelText("当前密码"), {
      target: { value: "OldPassword123" },
    });

    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(screen.queryByLabelText("当前密码")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());

    fireEvent.click(trigger);
    expect(await screen.findByLabelText("当前密码")).toHaveValue("");
  });

  it("changes the password with the current password and two matching new entries", async () => {
    fetchMe.mockResolvedValue(me);
    changePassword.mockResolvedValue({
      ok: true,
      access_token: "token",
      other_devices_kicked: 1,
    });

    renderIntl(<SettingsSecurity />);

    fireEvent.click(await screen.findByRole("button", {
      name: "修改密码",
      expanded: false,
    }));
    const formRegion = await screen.findByRole("region", { name: "修改密码" });
    fireEvent.change(within(formRegion).getByLabelText("当前密码"), {
      target: { value: "OldPassword123" },
    });
    fireEvent.change(within(formRegion).getByLabelText("新密码"), {
      target: { value: "NewPassword456" },
    });
    fireEvent.change(within(formRegion).getByLabelText("确认新密码"), {
      target: { value: "NewPassword456" },
    });
    fireEvent.click(within(formRegion).getByRole("button", { name: "修改密码" }));

    await waitFor(() => {
      expect(changePassword).toHaveBeenCalledWith({
        old_password: "OldPassword123",
        new_password: "NewPassword456",
      });
    });
    expect(
      await screen.findByText(/密码已修改 · 其他 1 个设备已被踢出/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("当前密码")).not.toBeInTheDocument();
  });

  it("keeps the form expanded when the current password is wrong", async () => {
    fetchMe.mockResolvedValue(me);
    changePassword.mockRejectedValue(
      new SecurityError(401, "wrong_old_password", "wrong password"),
    );

    renderIntl(<SettingsSecurity />);

    fireEvent.click(await screen.findByRole("button", {
      name: "修改密码",
      expanded: false,
    }));
    const formRegion = await screen.findByRole("region", { name: "修改密码" });
    fireEvent.change(within(formRegion).getByLabelText("当前密码"), {
      target: { value: "WrongPassword123" },
    });
    fireEvent.change(within(formRegion).getByLabelText("新密码"), {
      target: { value: "NewPassword456" },
    });
    fireEvent.change(within(formRegion).getByLabelText("确认新密码"), {
      target: { value: "NewPassword456" },
    });
    fireEvent.click(within(formRegion).getByRole("button", { name: "修改密码" }));

    expect(await within(formRegion).findByRole("alert")).toHaveTextContent(
      "当前密码错误",
    );
    expect(screen.getByLabelText("当前密码")).toHaveValue("WrongPassword123");
  });

  it("keeps email reset as the fallback when the current password is forgotten", async () => {
    fetchMe.mockResolvedValue(me);

    renderIntl(<SettingsSecurity />);

    const link = await screen.findByRole("link", {
      name: "忘记当前密码？通过邮件重置 →",
    });
    expect(link).toHaveAttribute("href", "/forgot-password/");
  });

  it("shows the provider guidance instead of a password form for OAuth-only accounts", async () => {
    fetchMe.mockResolvedValue({
      ...me,
      has_password: false,
      oauth_providers: ["feishu"],
    });

    renderIntl(<SettingsSecurity />);

    expect(
      await screen.findByText("通过 OAuth 登录，没有本地密码"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "修改密码" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("当前密码")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", {
        name: "忘记当前密码？通过邮件重置 →",
      }),
    ).not.toBeInTheDocument();
  });
});

describe("SettingsSecurity reset link glyph", () => {
  it("renders → after the email reset link", async () => {
    fetchMe.mockResolvedValue(me);
    renderIntl(<SettingsSecurity />);
    const message = catalogText("zh-CN", "settings.profile.reset_password");
    const link = await screen.findByRole("link", { name: (name) => name.startsWith(message) });
    expectRefreshedGlyph(link, "→", "end", message);
  });
});
