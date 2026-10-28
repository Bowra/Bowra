/**
 * email-config sprint M2 — SettingsEmailSection UI smoke.
 *
 * Verify admin gating (not rendered for non-admins), three-state status, form saving, and test-button disabled-when-none.
 */

import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import messages from "@/messages/zh-CN.json";

const fetchMe = vi.fn();
const getEmailConfig = vi.fn();
const putEmailConfig = vi.fn();
const sendTestEmail = vi.fn();
const deleteEmailConfig = vi.fn();

vi.mock("@/lib/auth-api", () => ({ fetchMe: () => fetchMe() }));
vi.mock("@/lib/email-config-api", () => ({
  getEmailConfig: () => getEmailConfig(),
  putEmailConfig: (b: unknown) => putEmailConfig(b),
  sendTestEmail: () => sendTestEmail(),
  deleteEmailConfig: () => deleteEmailConfig(),
}));

import { SettingsEmailSection } from "./email-config";

const renderEmail = () => render(
  <NextIntlClientProvider locale="zh-CN" messages={messages}>
    <SettingsEmailSection />
  </NextIntlClientProvider>,
);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SettingsEmailSection", () => {
  it("shows a loading skeleton while confirming identity and during the first read", async () => {
    fetchMe.mockReturnValue(new Promise(() => {}));
    const identity = renderEmail();
    expect(identity.container.querySelector(".ui-skeleton[role='status']")).not.toBeNull();
    identity.unmount();

    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockReturnValue(new Promise(() => {}));
    const loading = renderEmail();
    await waitFor(() => expect(getEmailConfig).toHaveBeenCalled());
    expect(loading.container.querySelector(".ui-skeleton[role='status']")).not.toBeNull();
  });

  it("renders nothing for non-admin", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: false });
    const { container } = renderEmail();
    await waitFor(() => expect(fetchMe).toHaveBeenCalled());
    expect(container.textContent).not.toContain("邮件服务");
  });

  it("shows 'not configured' warning state for admin", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockResolvedValue({ configured: "none", version: 0 });
    renderEmail();
    await waitFor(() => expect(screen.getByText("未配置")).toBeTruthy());
    // Warning copy states the consequences
    expect(screen.getByText(/邀请新成员、邮箱验证/)).toBeTruthy();
  });

  it("shows env-inherited state without leaking host", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockResolvedValue({ configured: "env", version: 0 });
    renderEmail();
    await waitFor(() => expect(screen.getByText("继承实例默认")).toBeTruthy());
    expect(
      screen.getByText(
        "当前团队未设置 SMTP，正在使用实例默认发信配置。为保护部署信息，本页不显示实例参数。",
      ),
    ).toBeTruthy();
  });

  it("shows the form and saves config", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockResolvedValue({ configured: "none", version: 0 });
    putEmailConfig.mockResolvedValue({ ok: true });
    renderEmail();
    await waitFor(() => expect(screen.getByPlaceholderText("smtp.feishu.cn")).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText("smtp.feishu.cn"), {
      target: { value: "smtp.x.com" },
    });
    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(putEmailConfig).toHaveBeenCalled());
    expect(putEmailConfig.mock.calls[0][0].host).toBe("smtp.x.com");
  });

  it("applies Feishu preset as SSL/TLS on port 465", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockResolvedValue({ configured: "none", version: 0 });
    putEmailConfig.mockResolvedValue({ ok: true });
    renderEmail();
    await waitFor(() => expect(screen.getByPlaceholderText("smtp.feishu.cn")).toBeTruthy());
    fireEvent.click(screen.getByText("飞书/Lark 预设"));

    expect(screen.getByPlaceholderText("smtp.feishu.cn")).toHaveValue("smtp.feishu.cn");
    expect(screen.getByLabelText("端口")).toHaveValue(465);
    expect(screen.getByLabelText("加密方式")).toHaveValue("ssl");

    fireEvent.change(screen.getByLabelText("SMTP 用户名"), {
      target: { value: "bowhead@datarc.cn" },
    });
    expect(screen.getByLabelText("发件地址")).toHaveValue("bowhead@datarc.cn");

    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(putEmailConfig).toHaveBeenCalled());
    expect(putEmailConfig.mock.calls[0][0]).toMatchObject({
      host: "smtp.feishu.cn",
      port: 465,
      user: "bowhead@datarc.cn",
      from_addr: "bowhead@datarc.cn",
      tls: true,
    });
  });

  it("does not overwrite SMTP username when From address diverges", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockResolvedValue({ configured: "none", version: 0 });
    putEmailConfig.mockResolvedValue({ ok: true });
    renderEmail();
    await waitFor(() => expect(screen.getByPlaceholderText("smtp.feishu.cn")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("SMTP 用户名"), {
      target: { value: "apikey" },
    });
    expect(screen.getByLabelText("发件地址")).toHaveValue("apikey");

    fireEvent.change(screen.getByLabelText("发件地址"), {
      target: { value: "news@company.com" },
    });
    expect(screen.getByLabelText("SMTP 用户名")).toHaveValue("apikey");

    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => expect(putEmailConfig).toHaveBeenCalled());
    expect(putEmailConfig.mock.calls[0][0]).toMatchObject({
      user: "apikey",
      from_addr: "news@company.com",
    });
  });

  it("keeps existing 465 configs displayed as SSL/TLS even if legacy tls is false", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockResolvedValue({
      configured: "team",
      version: 1,
      config: {
        host: "smtp.feishu.cn",
        port: 465,
        user: "bowhead@datarc.cn",
        from_addr: "bowhead@datarc.cn",
        from_name: "Bowhead",
        tls: false,
        password_set: true,
      },
    });
    renderEmail();
    await waitFor(() => expect(screen.getByText("已配置(团队)")).toBeTruthy());
    expect(screen.getByLabelText("加密方式")).toHaveValue("ssl");
  });

  it("refreshes configured state when save succeeds but test request fails", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig
      .mockResolvedValueOnce({ configured: "none", version: 0 })
      .mockResolvedValueOnce({
        configured: "team",
        version: 1,
        config: {
          host: "smtp.feishu.cn",
          port: 465,
          user: "bowhead@datarc.cn",
          from_addr: "bowhead@datarc.cn",
          from_name: "Bowhead",
          tls: true,
          password_set: true,
        },
      });
    putEmailConfig.mockResolvedValue({ ok: true });
    sendTestEmail.mockRejectedValue(new Error("boom"));

    renderEmail();
    await waitFor(() => expect(screen.getByPlaceholderText("smtp.feishu.cn")).toBeTruthy());
    fireEvent.click(screen.getByText("飞书/Lark 预设"));
    fireEvent.change(screen.getByLabelText("SMTP 用户名"), {
      target: { value: "bowhead@datarc.cn" },
    });
    fireEvent.change(screen.getByLabelText("密码 / 授权码"), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByText("保存并测试"));

    await waitFor(() => expect(putEmailConfig).toHaveBeenCalled());
    await waitFor(() => expect(sendTestEmail).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("已配置(团队)")).toBeTruthy());
    expect(screen.getByText("已保存")).toBeTruthy();
    expect(
      // A thrown Error is not a browser transport failure (ed6520128), so the generic copy applies.
      screen.getByText("邮件配置操作未完成，请稍后重试；如问题持续，请联系系统管理员。"),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain("boom");
  });

  it("blocks unauthenticated-encryption mode when SMTP username is present", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockResolvedValue({ configured: "none", version: 0 });
    renderEmail();
    await waitFor(() => expect(screen.getByPlaceholderText("smtp.feishu.cn")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("SMTP 用户名"), {
      target: { value: "bowhead@datarc.cn" },
    });
    fireEvent.change(screen.getByLabelText("加密方式"), {
      target: { value: "plain" },
    });
    fireEvent.click(screen.getByText("保存"));

    expect(putEmailConfig).not.toHaveBeenCalled();
    expect(screen.getByText("不加密模式不能使用 SMTP 用户名/密码。请选择 SSL/TLS 或 STARTTLS。")).toBeTruthy();
  });

  it("blocks unauthenticated-encryption mode when SMTP password is present", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockResolvedValue({ configured: "none", version: 0 });
    renderEmail();
    await waitFor(() => expect(screen.getByPlaceholderText("smtp.feishu.cn")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("密码 / 授权码"), {
      target: { value: "secret" },
    });
    fireEvent.change(screen.getByLabelText("加密方式"), {
      target: { value: "plain" },
    });
    fireEvent.click(screen.getByText("保存"));

    expect(putEmailConfig).not.toHaveBeenCalled();
    expect(screen.getByText("不加密模式不能使用 SMTP 用户名/密码。请选择 SSL/TLS 或 STARTTLS。")).toBeTruthy();
  });

  it("test button disabled when not configured", async () => {
    fetchMe.mockResolvedValue({ is_team_admin: true });
    getEmailConfig.mockResolvedValue({ configured: "none", version: 0 });
    renderEmail();
    await waitFor(() => expect(screen.getByPlaceholderText("smtp.feishu.cn")).toBeTruthy());
    const testBtn = screen.getByText("发送测试邮件") as HTMLButtonElement;
    expect(testBtn.disabled).toBe(true);
  });
});
