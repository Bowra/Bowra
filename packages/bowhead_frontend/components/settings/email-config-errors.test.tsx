import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";
import { ApiError } from "@/lib/api-client";

const apiMocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  getEmailConfig: vi.fn(),
  putEmailConfig: vi.fn(),
  sendTestEmail: vi.fn(),
  deleteEmailConfig: vi.fn(),
}));

vi.mock("@/lib/auth-api", () => ({
  fetchMe: () => apiMocks.fetchMe(),
}));
vi.mock("@/lib/email-config-api", () => ({
  getEmailConfig: () => apiMocks.getEmailConfig(),
  putEmailConfig: (body: unknown) => apiMocks.putEmailConfig(body),
  sendTestEmail: () => apiMocks.sendTestEmail(),
  deleteEmailConfig: () => apiMocks.deleteEmailConfig(),
}));

import { SettingsEmailSection } from "./email-config";

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    save: "保存",
    sendTest: "发送测试邮件",
    hostBlocked:
      "该 SMTP 服务器地址解析到内网、回环或保留地址，被安全策略拦截；当前部署只能使用可从公网访问的邮件服务器。",
    passwordRequired: "填写 SMTP 用户名时必须同时填写密码或授权码。",
    deliveryFailed:
      "测试邮件发送失败，请检查服务器、端口、加密方式、凭据和发件地址后重试。",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    save: "Save",
    sendTest: "Send test email",
    hostBlocked:
      "The SMTP server address resolves to a private, loopback, or reserved address and was blocked by the security policy. This deployment can only use mail servers reachable from the public internet.",
    passwordRequired:
      "An SMTP password or app token is required when a username is provided.",
    deliveryFailed:
      "The test email could not be sent. Check the server, port, encryption, credentials, and From address, then try again.",
  },
] as const;

function renderLocalized(entry: (typeof locales)[number]) {
  return render(
    <NextIntlClientProvider
      locale={entry.locale}
      messages={entry.messages}
    >
      <SettingsEmailSection />
    </NextIntlClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe.each(locales)("email config errors — $locale", (entry) => {
  it("localizes structured SMTP validation issues", async () => {
    apiMocks.fetchMe.mockResolvedValue({ is_team_admin: true });
    apiMocks.getEmailConfig.mockResolvedValue({
      configured: "none",
      version: 0,
    });
    apiMocks.putEmailConfig.mockRejectedValue(
      new ApiError(
        422,
        "RAW_BACKEND_INVALID_SMTP_CONFIG",
        {
          code: "invalid_smtp_config",
          body: {
            detail: {
              issues: [
                {
                  code: "smtp_host_blocked",
                  message: "RAW_SSRF_DIAGNOSTIC",
                },
                {
                  code: "smtp_password_required",
                  message: "RAW_PASSWORD_DIAGNOSTIC",
                },
              ],
            },
          },
        },
      ),
    );

    renderLocalized(entry);
    await screen.findByPlaceholderText("smtp.feishu.cn");
    fireEvent.click(screen.getByRole("button", { name: entry.save }));

    expect(await screen.findByText(
      `${entry.hostBlocked}${entry.locale === "zh-CN" ? "；" : "; "}${entry.passwordRequired}`,
    )).toBeTruthy();
    expect(document.body.textContent).not.toContain("invalid_smtp_config");
    expect(document.body.textContent).not.toContain("RAW_");
  });

  it("renders test error_code and ignores the API fallback message", async () => {
    apiMocks.fetchMe.mockResolvedValue({ is_team_admin: true });
    apiMocks.getEmailConfig.mockResolvedValue({
      configured: "team",
      version: 1,
      config: {
        host: "smtp.example.com",
        port: 465,
        user: "mailer@example.com",
        from_addr: "mailer@example.com",
        from_name: "Bowhead",
        tls: true,
        password_set: true,
      },
    });
    apiMocks.sendTestEmail.mockResolvedValue({
      ok: false,
      source: "team",
      error_code: "email_test_delivery_failed",
      error: "RAW_SMTP_AUTHENTICATION_DIAGNOSTIC",
    });

    renderLocalized(entry);
    await screen.findByPlaceholderText("smtp.feishu.cn");
    fireEvent.click(screen.getByRole("button", { name: entry.sendTest }));

    expect(await screen.findByText(entry.deliveryFailed)).toBeTruthy();
    expect(document.body.textContent).not.toContain(
      "email_test_delivery_failed",
    );
    expect(document.body.textContent).not.toContain("RAW_SMTP");
  });
});
