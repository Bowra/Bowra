import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const bootstrapMock = vi.fn();
const oauthMock = vi.fn();
const loginMock = vi.fn();
const loginCapabilitiesMock = vi.fn();
const diagnosticsCapability = { enabled: false };

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
  }),
}));
vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>("@/lib/auth-api");
  return {
    ...actual,
    getBootstrapStatus: (...args: unknown[]) => bootstrapMock(...args),
    getLoginCapabilities: (...args: unknown[]) => loginCapabilitiesMock(...args),
    loginUser: (...args: unknown[]) => loginMock(...args),
    oauthLoginRedirect: (...args: unknown[]) => oauthMock(...args),
  };
});
vi.mock("@/lib/use-error-diagnostics-capability", () => ({
  useErrorDiagnosticsCapability: (forced?: boolean) =>
    forced ?? diagnosticsCapability.enabled,
}));

import LoginPage from "./page";
import { AuthError as AuthApiError } from "@/lib/auth-api";

beforeEach(() => {
  loginCapabilitiesMock.mockResolvedValue({
    oauth_providers: ["feishu"],
    enterprise_oidc: false,
  });
});

const callbackErrors = [
  [
    "oauth_state_invalid",
    "zh-CN",
    "本次登录授权已失效或已被使用，请从当前页面重新开始。",
  ],
  [
    "oauth_state_invalid",
    "en-US",
    "This sign-in authorization has expired or was already used. Start again from this page.",
  ],
  [
    "oauth_token_exchange_failed",
    "zh-CN",
    "飞书未能完成登录凭据交换，请稍后重试。",
  ],
  [
    "oauth_token_exchange_failed",
    "en-US",
    "Feishu could not complete the credential exchange. Try again later.",
  ],
  [
    "invite_activation_failed",
    "zh-CN",
    "邀请状态已变化，无法完成账号激活。请重新打开邀请链接或联系管理员。",
  ],
  [
    "invite_activation_failed",
    "en-US",
    "The invitation changed before your account could be activated. Reopen the invitation link or contact an administrator.",
  ],
  [
    "seat_exceeded",
    "zh-CN",
    "当前团队没有可用席位，请联系团队管理员扩容或释放席位。",
  ],
  [
    "seat_exceeded",
    "en-US",
    "This team has no available seats. Ask a team administrator to add capacity or free a seat.",
  ],
  [
    "oidc_not_joined",
    "zh-CN",
    "这个企业账号还没有加入 Bowhead。请联系团队负责人邀请你加入，接受邀请后再用企业账号登录。",
  ],
  [
    "oidc_not_joined",
    "en-US",
    "This company account hasn't joined Bowhead yet. Ask your team owner to invite you, then sign in with your company account after you accept.",
  ],
  [
    "oidc_email_missing",
    "zh-CN",
    "企业身份提供商未返回可用的邮箱字段，无法确认登录账号。请联系团队负责人检查 IdP 的 UserInfo 邮箱映射。",
  ],
  [
    "oidc_email_untrusted",
    "en-US",
    "The enterprise identity provider did not mark the email as verified. This deployment accepts only IdP-verified emails. Ask your team owner to check the IdP configuration or email verification policy.",
  ],
  [
    "oidc_user_not_found",
    "zh-CN",
    "该企业邮箱尚未关联 Bowhead 用户。请联系团队负责人邀请你加入。",
  ],
  [
    "oidc_user_inactive",
    "en-US",
    "The Bowhead user associated with this enterprise email is not currently available. Ask your team owner to restore access.",
  ],
  [
    "oidc_subject_missing",
    "zh-CN",
    "企业身份提供商未返回登录所需的唯一标识，无法完成登录。请联系团队负责人检查 IdP 的 UserInfo 配置。",
  ],
] as const;

beforeEach(() => {
  diagnosticsCapability.enabled = false;
  bootstrapMock.mockResolvedValue({
    required: false,
    deployment_mode: "local",
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/login/");
});

describe("LoginPage error localization", () => {
  it.each([
    [
      "zh-CN",
      "当前地址还没有登记为公开入口，部分链接可能使用默认地址。如需登记，请联系团队负责人。",
      "查看技术详情",
    ],
    [
      "en-US",
      "This address isn't registered as a public entry yet, so some links may use the default address. To register it, contact your team owner.",
      "View technical details",
    ],
  ] as const)(
    "shows a localized 421 reason without technical details by default in %s",
    async (locale, message, detailsLabel) => {
      loginMock.mockRejectedValue(
        new AuthApiError(
          421,
          "generic",
          "public_entry_not_registered",
          "browser public URL is not registered",
          "req_public_entry_1",
        ),
      );
      renderIntl(<LoginPage />, { locale });

      fireEvent.change(document.querySelector("input[type=email]") as HTMLInputElement, {
        target: { value: "u@example.com" },
      });
      const password = document.querySelector("input[type=password]") as HTMLInputElement;
      fireEvent.change(password, { target: { value: "password1" } });
      fireEvent.submit(password.closest("form") as HTMLFormElement);

      await waitFor(() => {
        expect(screen.getByTestId("auth-error-message")).toHaveTextContent(message);
      });
      // Technical details only appear when error diagnostics are on; by default only the user-facing sentence shows.
      expect(screen.queryByText(detailsLabel)).toBeNull();
      expect(screen.queryByTestId("auth-error-diagnostics")).toBeNull();
      expect(document.body).not.toHaveTextContent("browser public URL is not registered");
      expect(document.body).not.toHaveTextContent("req_public_entry_1");
    },
  );

  it.each([
    [
      "zh-CN",
      "当前地址还没有登记为公开入口，部分链接可能使用默认地址。如需登记，请联系团队负责人。",
      "查看技术详情",
      "复制诊断信息",
    ],
    [
      "en-US",
      "This address isn't registered as a public entry yet, so some links may use the default address. To register it, contact your team owner.",
      "View technical details",
      "Copy diagnostic information",
    ],
  ] as const)(
    "uses the common PublicEntry diagnostic panel in %s when the pilot is enabled",
    async (locale, message, detailsLabel, copyLabel) => {
      diagnosticsCapability.enabled = true;
      const requestId = "0123456789abcdef0123456789abcdef";
      loginMock.mockRejectedValue(
        new AuthApiError(
          421,
          "generic",
          "public_entry_not_registered",
          "browser public URL is not registered",
          requestId,
          {
            schemaVersion: 1,
            disclosure: "public-neutral",
            status: 421,
            code: "public_entry_not_registered",
            diagnosticCode: "public_entry_registry_miss",
            requestId,
            retryable: false,
            observedAt: "2026-07-29T10:30:00.000Z",
          },
        ),
      );
      renderIntl(<LoginPage />, { locale });

      fireEvent.change(
        document.querySelector("input[type=email]") as HTMLInputElement,
        { target: { value: "u@example.com" } },
      );
      const password = document.querySelector(
        "input[type=password]",
      ) as HTMLInputElement;
      fireEvent.change(password, { target: { value: "password1" } });
      fireEvent.submit(password.closest("form") as HTMLFormElement);

      await waitFor(() =>
        expect(screen.getByRole("alert")).toHaveTextContent(message),
      );
      expect(screen.getByText(detailsLabel)).toBeTruthy();
      expect(
        screen.getByRole("button", { name: copyLabel }),
      ).toBeTruthy();
      expect(screen.getByTestId("error-diagnostics")).toHaveTextContent(
        "public_entry_registry_miss",
      );
      expect(screen.getByTestId("error-diagnostics")).toHaveTextContent(
        requestId,
      );
      expect(document.body).not.toHaveTextContent(
        "browser public URL is not registered",
      );
    },
  );

  it.each(callbackErrors)(
    "shows localized %s in %s and removes the machine code from the URL",
    async (code, locale, message) => {
      window.history.pushState(
        {},
        "",
        `/login/?oauth_error=${code}&oauth_provider=feishu`,
      );

      renderIntl(<LoginPage />, { locale });

      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      expect(screen.queryByText(code)).toBeNull();
      expect(window.location.search).toBe("");
    },
  );

  it("keeps enterprise OAuth available after an unmatched OIDC callback", async () => {
    window.history.pushState({}, "", "/login/?oauth_error=oidc_not_joined");
    loginCapabilitiesMock.mockResolvedValue({
      oauth_providers: ["feishu"],
      enterprise_oidc: true,
    });

    renderIntl(<LoginPage />, { locale: "en-US" });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This company account hasn't joined Bowhead yet. Ask your team owner to invite you, then sign in with your company account after you accept.",
    );
    expect(
      await screen.findByRole("button", { name: "Sign in with your company account" }),
    ).toBeTruthy();
  });

  it.each([
    [
      "zh-CN",
      "无法完成飞书登录，请稍后重试；如问题持续，请联系管理员。",
    ],
    [
      "en-US",
      "We could not complete Feishu sign-in. Try again later. If the problem continues, contact an administrator.",
    ],
  ] as const)(
    "uses the localized safe fallback for an unknown callback code in %s",
    async (locale, message) => {
      window.history.pushState(
        {},
        "",
        "/login/?oauth_error=internal_provider_exception&oauth_provider=feishu",
      );

      renderIntl(<LoginPage />, { locale });

      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      expect(screen.queryByText("internal_provider_exception")).toBeNull();
    },
  );

  it("maps a structured OAuth start error without exposing its raw message", async () => {
    oauthMock.mockRejectedValue(
      Object.assign(new Error("upstream-secret-detail"), {
        reasonCode: "oauth_not_configured",
      }),
    );
    renderIntl(<LoginPage />, { locale: "en-US" });

    fireEvent.click(await screen.findByRole("button", { name: "Sign in with Feishu" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Feishu sign-in has not been configured. Choose another method or contact your system administrator.",
      );
    });
    expect(screen.queryByText(/upstream-secret-detail/)).toBeNull();
  });
});
