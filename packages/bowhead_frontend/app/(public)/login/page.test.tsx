/**
 * §C (real-auth) — login page wiring (Task #8 FE): a 403 email_not_verified surfaces the verify
 * notice (not a generic error) and does NOT route into the app.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushMock }) }));
vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  useTranslations: () => (k: string) => k,
}));

const diagnosticsCapability = { enabled: false };
vi.mock("@/lib/use-error-diagnostics-capability", () => ({
  useErrorDiagnosticsCapability: (forced?: boolean) =>
    forced ?? diagnosticsCapability.enabled,
}));

const loginMock = vi.fn();
const oauthMock = vi.fn();
const bootstrapMock = vi.fn();
const loginCapabilitiesMock = vi.fn();
vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>("@/lib/auth-api");
  return {
    ...actual,
    getBootstrapStatus: (...a: unknown[]) => bootstrapMock(...a),
    getLoginCapabilities: (...a: unknown[]) => loginCapabilitiesMock(...a),
    loginUser: (...a: unknown[]) => loginMock(...a),
    oauthLoginRedirect: (...a: unknown[]) => oauthMock(...a),
    resendVerificationEmail: vi.fn(async () => ({ ok: true })),
  };
});

const authCapabilitiesMock = vi.fn();
vi.mock("@/lib/invite-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/invite-api")>("@/lib/invite-api");
  return {
    ...actual,
    getAuthCapabilities: (...a: unknown[]) => authCapabilitiesMock(...a),
  };
});

// Production 116 shape: saas + invite_only + password only.
const SAAS_INVITE_ONLY = {
  deployment_mode: "saas",
  account_creation_policy: "invite_only",
  email_verification_policy: "not_required",
  auth_methods: ["password"],
  public_registration: false,
  bootstrap_required: false,
  invite_activation: false,
};

import LoginPage from "./page";
import { AuthError as AuthApiError } from "@/lib/auth-api";

const origLocation = window.location;

beforeEach(() => {
  diagnosticsCapability.enabled = false;
  bootstrapMock.mockResolvedValue({
    required: false,
    deployment_mode: "local",
  });
  loginCapabilitiesMock.mockResolvedValue({
    oauth_providers: ["feishu"],
    enterprise_oidc: false,
  });
  authCapabilitiesMock.mockResolvedValue(SAAS_INVITE_ONLY);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
  window.__BOWHEAD_BASE_PATH__ = undefined;
  window.__BOWHEAD_BASE_URL__ = undefined;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: origLocation,
  });
  window.history.pushState({}, "", "/login");
});

function _fillAndSubmit() {
  fireEvent.change(screen.getByPlaceholderText("you@company.com"), {
    target: { value: "u@x.com" },
  });
  const pw = document.querySelector("input[type=password]") as HTMLInputElement;
  fireEvent.change(pw, { target: { value: "password1" } });
  fireEvent.submit(pw.closest("form") as HTMLFormElement);
}

describe("LoginPage email-verify wiring", () => {
  it("空实例按运行时前缀直接进入激活页", async () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...origLocation,
        pathname: "/ai/apps/bowhead/login/",
        search: "",
        replace: vi.fn(),
      },
    });
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: null,
      teamId: null,
      baseUrl: "https://example.com/ai/apps/bowhead",
      basePath: "/ai/apps/bowhead",
      matchSource: "trusted_proxy_bootstrap",
    };
    bootstrapMock.mockResolvedValue({
      required: true,
      deployment_mode: "local",
    });

    render(<LoginPage />);

    await waitFor(() => expect(window.location.replace).toHaveBeenCalledWith(
      "/ai/apps/bowhead/activate/",
    ));
  });

  it("403 email_not_verified → renders the verify notice, does NOT push to /", async () => {
    loginMock.mockRejectedValue(
      Object.assign(new Error("nope"), { status: 403, reasonCode: "email_not_verified" }),
    );
    render(<LoginPage />);
    _fillAndSubmit();
    await waitFor(() => expect(screen.getByTestId("auth-verify-notice")).toBeTruthy());
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("421 shows a readable public-entry message and hides technical details by default", async () => {
    loginMock.mockRejectedValue(
      new AuthApiError(
        421,
        "generic",
        "public_entry_not_registered",
        "browser public URL is not registered",
        "req_public_entry_1",
      ),
    );
    render(<LoginPage />);
    _fillAndSubmit();

    await waitFor(() => {
      expect(screen.getByTestId("auth-error-message").textContent).toBe(
        "error.public_entry_not_registered",
      );
    });
    expect(screen.queryByTestId("auth-error-diagnostics")).toBeNull();
    expect(document.body.textContent).not.toContain("browser public URL is not registered");
    expect(document.body.textContent).not.toContain("req_public_entry_1");
  });

  it("shows collapsed technical details only when error diagnostics are enabled", async () => {
    diagnosticsCapability.enabled = true;
    loginMock.mockRejectedValue(
      new AuthApiError(
        421,
        "generic",
        "public_entry_not_registered",
        "browser public URL is not registered",
        "req_public_entry_1",
      ),
    );
    render(<LoginPage />);
    _fillAndSubmit();

    const details = await screen.findByTestId("auth-error-diagnostics") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    fireEvent.click(screen.getByText("diagnostics.summary"));
    expect(details.open).toBe(true);
    expect(details.textContent).toContain("421");
    expect(details.textContent).toContain("public_entry_not_registered");
    expect(details.textContent).toContain("req_public_entry_1");
    expect(details.textContent).toContain("browser public URL is not registered");
    expect(screen.queryByTestId("error-notice")).toBeNull();
  });

  it("network failures are not mislabeled as HTTP errors or expanded from TypeError.message", async () => {
    diagnosticsCapability.enabled = true;
    loginMock.mockRejectedValue(new TypeError("Failed to fetch"));
    render(<LoginPage />);
    _fillAndSubmit();

    await waitFor(() => {
      expect(screen.getByTestId("auth-error-message").textContent).toBe(
        "error.login_network_error",
      );
    });
    const details = screen.getByTestId("auth-error-diagnostics");
    expect(details.textContent).toContain("service_unreachable");
    expect(details.textContent).not.toContain("Failed to fetch");
    expect(details.textContent).not.toContain("HTTP");
  });

  it.each([new TypeError("Invalid login state"), new Error("Internal login exception")])(
    "keeps an unknown login failure separate from transport diagnostics: %s",
    async (cause) => {
      diagnosticsCapability.enabled = true;
      loginMock.mockRejectedValue(cause);
      render(<LoginPage />);
      _fillAndSubmit();

      await waitFor(() => {
        expect(screen.getByTestId("auth-error-message").textContent).toBe("error.login_failed");
      });
      expect(document.body.textContent).not.toContain("service_unreachable");
      expect(document.body.textContent).not.toContain(cause.message);
      expect(screen.queryByTestId("auth-error-diagnostics")).toBeNull();
    },
  );

  it("uses the common disclosure and fixed copy bundle when the runtime pilot is enabled", async () => {
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
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<LoginPage />);
    _fillAndSubmit();

    await waitFor(() =>
      expect(screen.getByTestId("error-notice")).toBeTruthy(),
    );
    expect(screen.getByTestId("error-diagnostics").textContent).toContain(
      "public_entry_registry_miss",
    );
    expect(screen.getByTestId("error-diagnostics").textContent).toContain(
      requestId,
    );
    expect(document.body.textContent).not.toContain(
      "browser public URL is not registered",
    );

    fireEvent.click(
      screen.getByRole("button", { name: "diagnostics.copy" }),
    );
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toContain("surface: login");
    expect(copied).toContain("http_status: 421");
    expect(copied).toContain("diagnostic_code: public_entry_registry_miss");
    expect(copied).toContain(`request_id: ${requestId}`);
    expect(copied).not.toContain("browser public URL is not registered");
  });

  it("successful login enters the app through the configured runtime prefix", async () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...origLocation,
        pathname: "/ai/apps/bowhead/login/",
        search: "",
        replace: vi.fn(),
      },
    });
    window.__BOWHEAD_BASE_PATH__ = "/ai/apps/bowhead";
    loginMock.mockResolvedValue({ token: "jwt", user: { id: "u", email: "u@x.com", tier: "ultra" } });
    render(<LoginPage />);
    _fillAndSubmit();
    await waitFor(() => expect(window.location.replace).toHaveBeenCalledWith(
      "/ai/apps/bowhead/",
    ));
    expect(pushMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("auth-verify-notice")).toBeNull();
  });

  it("OAuth login preserves invite next path", async () => {
    window.history.pushState({}, "", "/login?next=%2Finvite%3Faccept%3D1");
    oauthMock.mockResolvedValue({ redirect_url: "/oauth-provider" });
    render(<LoginPage />);
    fireEvent.click((await screen.findByText("login.oauth_feishu")).closest("button") as HTMLButtonElement);
    await waitFor(() => expect(oauthMock).toHaveBeenCalledWith("feishu", "/invite?accept=1"));
  });

  it("only renders configured third-party sign-in methods", async () => {
    loginCapabilitiesMock.mockResolvedValue({
      oauth_providers: ["feishu"],
      enterprise_oidc: false,
    });

    render(<LoginPage />);

    expect(await screen.findByRole("button", { name: "login.oauth_feishu" })).toBeTruthy();
  });

  it("hides the third-party section when no sign-in capability is available", async () => {
    loginCapabilitiesMock.mockResolvedValue({
      oauth_providers: [],
      enterprise_oidc: false,
    });

    render(<LoginPage />);

    await waitFor(() => expect(loginCapabilitiesMock).toHaveBeenCalled());
    expect(screen.queryByText("login.divider_or")).toBeNull();
    expect(screen.queryByRole("button", { name: "login.oauth_feishu" })).toBeNull();
  });

  it("binds OAuth state to the server-side invite intent", async () => {
    window.history.pushState(
      {},
      "",
      "/login?flow=invite&next=%2Finvite%2F%3Fresume%3D1",
    );
    oauthMock.mockResolvedValue({ redirect_url: "/oauth-provider" });
    render(<LoginPage />);
    fireEvent.click((await screen.findByText("login.oauth_feishu")).closest("button") as HTMLButtonElement);
    await waitFor(() => {
      expect(oauthMock).toHaveBeenCalledWith(
        "feishu",
        "/invite/?resume=1",
        "invite",
      );
    });
  });

  it("preserves invite context in the registration link with a trailing slash", async () => {
    bootstrapMock.mockResolvedValue({
      required: false,
      deployment_mode: "cloud",
    });
    window.history.pushState(
      {},
      "",
      "/login?flow=invite&next=%2Finvite%2F%3Fresume%3D1",
    );

    render(<LoginPage />);

    const link = await screen.findByText("login.register_link");
    expect(link.closest("a")?.getAttribute("href")).toBe(
      "/register/?source=invite",
    );
  });

  it("register link preserves invite token from login next", async () => {
    window.history.pushState(
      {},
      "",
      "/login?next=%2Finvite%3Ftoken%3Dinvite-token%26accept%3D1",
    );
    render(<LoginPage />);
    await waitFor(() => {
      expect(screen.getByText("login.register_link").closest("a")?.getAttribute("href"))
        .toBe("/register/?source=invite&token=invite-token");
    });
  });

  it("saas + invite_only + password only: no single sign-on divider and no dead register link", async () => {
    bootstrapMock.mockResolvedValue({ required: false, deployment_mode: "saas" });
    loginCapabilitiesMock.mockResolvedValue({ oauth_providers: [], enterprise_oidc: false });
    render(<LoginPage />);

    expect(await screen.findByText("login.no_account_invite_only")).toBeTruthy();
    expect(screen.queryByText("login.divider_or")).toBeNull();
    expect(screen.queryByText("login.register_link")).toBeNull();
  });

  it("puts single sign-on above the email form", async () => {
    loginCapabilitiesMock.mockResolvedValue({ oauth_providers: ["feishu"], enterprise_oidc: false });
    render(<LoginPage />);
    const feishu = await screen.findByRole("button", { name: "login.oauth_feishu" });
    const email = screen.getByPlaceholderText("you@company.com");
    const divider = screen.getByText("login.divider_or");
    // Tab order follows the visible sign-in order.
    expect(feishu.compareDocumentPosition(divider) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(divider.compareDocumentPosition(email) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("local deployment shows neither a register link nor the invite hint", async () => {
    authCapabilitiesMock.mockResolvedValue({
      ...SAAS_INVITE_ONLY,
      deployment_mode: "local",
    });
    render(<LoginPage />);
    await waitFor(() => expect(authCapabilitiesMock).toHaveBeenCalled());
    await screen.findByText("login.oauth_feishu");
    expect(screen.queryByText("login.register_link")).toBeNull();
    expect(screen.queryByText("login.no_account_invite_only")).toBeNull();
  });

  it("shows the register link when public registration is open", async () => {
    authCapabilitiesMock.mockResolvedValue({
      ...SAAS_INVITE_ONLY,
      account_creation_policy: "open",
      public_registration: true,
    });
    render(<LoginPage />);
    const link = await screen.findByText("login.register_link");
    expect(link.closest("a")?.getAttribute("href")).toBe("/register/");
    expect(screen.queryByText("login.no_account_invite_only")).toBeNull();
  });

  it("hides the register link when capabilities cannot be loaded", async () => {
    authCapabilitiesMock.mockRejectedValue(new Error("offline"));
    render(<LoginPage />);
    await waitFor(() => expect(authCapabilitiesMock).toHaveBeenCalled());
    await screen.findByText("login.oauth_feishu");
    expect(screen.queryByText("login.register_link")).toBeNull();
    expect(screen.queryByText("login.no_account_invite_only")).toBeNull();
  });

  it("unsafe login next falls back to /", async () => {
    window.history.pushState({}, "", "/login?next=%2F%2Fevil.example");
    oauthMock.mockResolvedValue({ redirect_url: "/oauth-provider" });
    render(<LoginPage />);
    fireEvent.click((await screen.findByText("login.oauth_feishu")).closest("button") as HTMLButtonElement);
    await waitFor(() => expect(oauthMock).toHaveBeenCalledWith("feishu", "/"));
  });

  it("reports empty fields next to them instead of the browser bubble", async () => {
    render(<LoginPage />);
    const email = screen.getByPlaceholderText("you@company.com");
    fireEvent.submit(email.closest("form") as HTMLFormElement);

    const emailError = await screen.findByText("field_error.email_required");
    const passwordError = screen.getByText("field_error.password_required");
    const password = document.querySelector("input[type=password]") as HTMLInputElement;
    expect(email.closest("form")?.noValidate).toBe(true);
    expect(email.getAttribute("aria-invalid")).toBe("true");
    expect(email.getAttribute("aria-describedby")).toBe(emailError.closest("p")?.id);
    expect(password.getAttribute("aria-describedby")).toBe(passwordError.closest("p")?.id);
    expect(document.activeElement).toBe(email);
    expect(loginMock).not.toHaveBeenCalled();
  });

  it("keeps the email, clears the password and returns focus to it after wrong credentials", async () => {
    loginMock.mockRejectedValue(new AuthApiError(401, "generic", "invalid_credentials", "invalid credentials"));
    render(<LoginPage />);
    _fillAndSubmit();

    expect((await screen.findByRole("alert")).textContent).toContain("error.invalid_credentials");
    const password = document.querySelector("input[type=password]") as HTMLInputElement;
    await waitFor(() => expect(document.activeElement).toBe(password));
    expect(password.value).toBe("");
    expect((screen.getByPlaceholderText("you@company.com") as HTMLInputElement).value).toBe("u@x.com");
  });

  it("shows the unregistered public entry notice as neutral information, not a block", async () => {
    loginMock.mockRejectedValue(
      new AuthApiError(421, "generic", "public_entry_not_registered", "browser public URL is not registered"),
    );
    render(<LoginPage />);
    _fillAndSubmit();
    const notice = await screen.findByRole("status");
    expect(notice.textContent).toContain("error.public_entry_not_registered");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("turns the single sign-on button into a progress state while redirecting", async () => {
    let resolveRedirect: ((value: { redirect_url: string }) => void) | undefined;
    oauthMock.mockReturnValue(new Promise((resolve) => { resolveRedirect = resolve; }));
    render(<LoginPage />);
    fireEvent.click(await screen.findByRole("button", { name: "login.oauth_feishu" }));
    const busy = await screen.findByRole("button", { name: "login.sso_redirecting" });
    expect(busy.getAttribute("aria-busy")).toBe("true");
    expect((busy as HTMLButtonElement).disabled).toBe(true);
    expect(resolveRedirect).toBeTypeOf("function");
  });
});
