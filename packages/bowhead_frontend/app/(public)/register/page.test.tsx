/**
 * §C (real-auth) — register page wiring (Task #8 FE): a requires_verification response swaps the
 * form for the verify notice and does NOT route into the app.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const pushMock = vi.fn();
const replaceMock = vi.fn();
const searchParamsMock = vi.hoisted(() => ({ value: new URLSearchParams() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  useSearchParams: () => searchParamsMock.value,
}));
vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  // Keys render as-is; the not-sent notice also shows its address and reference so tests can read them.
  useTranslations: () => (k: string, p?: { email?: string; ref?: string }) =>
    p && typeof p.ref === "string" ? `${k}|${p.email}|${p.ref}` : k,
}));

const registerMock = vi.fn();
const resendMock = vi.fn();
vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>("@/lib/auth-api");
  return {
    ...actual,
    registerUser: (...a: unknown[]) => registerMock(...a),
    resendVerificationEmail: (...a: unknown[]) => resendMock(...a),
  };
});
const capabilitiesMock = vi.fn();
vi.mock("@/lib/invite-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/invite-api")>("@/lib/invite-api");
  return {
    ...actual,
    getAuthCapabilities: (...a: unknown[]) => capabilitiesMock(...a),
  };
});

import RegisterPage from "./page";
import { AuthError as AuthApiError } from "@/lib/auth-api";

const origLocation = window.location;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  searchParamsMock.value = new URLSearchParams();
  window.sessionStorage.clear();
  window.__BOWHEAD_BASE_PATH__ = undefined;
  window.__BOWHEAD_BASE_URL__ = undefined;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: origLocation,
  });
  window.history.replaceState(null, "", "/");
  capabilitiesMock.mockResolvedValue({
    public_registration: true,
    invite_activation: true,
  });
});

function _fillAndSubmit() {
  fireEvent.change(screen.getByPlaceholderText("you@company.com"), {
    target: { value: "u@x.com" },
  });
  const pws = document.querySelectorAll("input[type=password]");
  fireEvent.change(pws[0], { target: { value: "password1" } });
  fireEvent.change(pws[1], { target: { value: "password1" } });
  fireEvent.submit((pws[0] as HTMLInputElement).closest("form") as HTMLFormElement);
}

describe("RegisterPage email-verify wiring", () => {
  it("requires_verification → renders the verify notice, does NOT push to /", async () => {
    registerMock.mockResolvedValue({
      requires_verification: true,
      user: { id: "u", email: "u@x.com", tier: "ultra", email_verified: false },
    });
    capabilitiesMock.mockResolvedValue({ public_registration: true });
    render(<RegisterPage />);
    await screen.findByPlaceholderText("you@company.com");
    _fillAndSubmit();
    // Verification email sent: result card (title + description + resend), the registration form is gone
    expect(await screen.findByRole("heading", { name: "verify.sent_title" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "verify.resend" })).toBeTruthy();
    expect(screen.queryByPlaceholderText("you@company.com")).toBeNull();
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("auto-login enters the app through the configured runtime prefix", async () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...origLocation,
        pathname: "/ai/apps/bowhead/register/",
        search: "",
        replace: vi.fn(),
      },
    });
    window.__BOWHEAD_BASE_PATH__ = "/ai/apps/bowhead";
    registerMock.mockResolvedValue({ token: "jwt", user: { id: "u", email: "u@x.com", tier: "ultra" } });
    capabilitiesMock.mockResolvedValue({ public_registration: true });
    render(<RegisterPage />);
    await screen.findByPlaceholderText("you@company.com");
    _fillAndSubmit();
    await waitFor(() => expect(window.location.replace).toHaveBeenCalledWith(
      "/ai/apps/bowhead/",
    ));
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("redirects legacy invite parameters to the dedicated invite flow", async () => {
    searchParamsMock.value = new URLSearchParams("source=invite&token=invite-token");
    window.history.replaceState(null, "", "/register?source=invite&token=invite-token");

    render(<RegisterPage />);
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/invite/?token=invite-token");
    });
    expect(registerMock).not.toHaveBeenCalled();
  });

  // ── feedback-triage-2026-08-24: customer report “邀请用户，用户点开邮件链接，无法进入下一步” ──
  //
  // Root cause was a **redirect loop**: /invite/ used the legacy branch with activation off; Create Profile
  // pushed to `/register/?source=invite&token=…`, while this page originally **unconditionally** bounced back to `/invite/`.
  // Both changed in the same commit (942693276), which broke its own legacy fallback.
  // 116 production capabilities were exactly {invite_activation:false, public_registration:false}.

  it("★ 激活流关闭时不得再弹回 /invite —— 那会和 invite 页互相弹成死循环", async () => {
    capabilitiesMock.mockResolvedValue({
      public_registration: false,
      invite_activation: false,
    });
    searchParamsMock.value = new URLSearchParams("source=invite&token=invite-token");
    window.history.replaceState(null, "", "/register?source=invite&token=invite-token");

    render(<RegisterPage />);
    await waitFor(() => expect(capabilitiesMock).toHaveBeenCalled());
    // Form must really render (this is the legacy invitation-registration landing).
    await screen.findByPlaceholderText("you@company.com");
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("★ legacy 邀请注册必须把 invite_token 一起发出去", async () => {
    capabilitiesMock.mockResolvedValue({
      public_registration: false,
      invite_activation: false,
    });
    registerMock.mockResolvedValue({ requires_verification: false });
    searchParamsMock.value = new URLSearchParams("source=invite&token=invite-token");
    window.history.replaceState(null, "", "/register?source=invite&token=invite-token");

    render(<RegisterPage />);
    await screen.findByPlaceholderText("you@company.com");
    _fillAndSubmit();
    await waitFor(() => expect(registerMock).toHaveBeenCalled());
    // Without token, invite_only backend rejects; user also would not join the team specified by the invitation.
    expect(registerMock.mock.calls[0][0]).toMatchObject({ invite_token: "invite-token" });
  });

  it("激活流开着时仍然让路给 /invite 的 intent 流（单一入口，原意图不变）", async () => {
    capabilitiesMock.mockResolvedValue({
      public_registration: false,
      invite_activation: true,
    });
    searchParamsMock.value = new URLSearchParams("source=invite&token=invite-token");
    window.history.replaceState(null, "", "/register?source=invite&token=invite-token");

    render(<RegisterPage />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/invite/?token=invite-token"));
    expect(registerMock).not.toHaveBeenCalled();
  });

  it("checks the confirmation on blur and keeps field errors next to the fields", async () => {
    capabilitiesMock.mockResolvedValue({ public_registration: true });
    render(<RegisterPage />);
    await screen.findByPlaceholderText("you@company.com");
    const passwords = document.querySelectorAll("input[type=password]");
    fireEvent.change(passwords[0], { target: { value: "Northstar2026" } });
    fireEvent.change(passwords[1], { target: { value: "Northstar2025" } });
    fireEvent.blur(passwords[1]);
    const mismatch = await screen.findByText("error.password_mismatch");
    expect(passwords[1].getAttribute("aria-invalid")).toBe("true");
    expect(passwords[1].getAttribute("aria-describedby")).toContain(mismatch.closest("p")?.id);

    fireEvent.submit((passwords[0] as HTMLInputElement).closest("form") as HTMLFormElement);
    // Empty email: the error shows under the email field, focus moves to the first invalid field, nothing is submitted
    expect(await screen.findByText("field_error.email_required")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByPlaceholderText("you@company.com"));
    expect(registerMock).not.toHaveBeenCalled();
  });

  it("maps an already-registered email to a sentence instead of the backend text", async () => {
    capabilitiesMock.mockResolvedValue({ public_registration: true });
    registerMock.mockRejectedValue(new AuthApiError(409, "generic", undefined, "email already registered"));
    render(<RegisterPage />);
    await screen.findByPlaceholderText("you@company.com");
    _fillAndSubmit();
    expect(await screen.findByText("error.register_email_taken")).toBeTruthy();
    expect(document.body.textContent).not.toContain("email already registered");
  });
});

describe("RegisterPage verification mail outcome", () => {
  async function registerWith(response: Record<string, unknown>) {
    registerMock.mockResolvedValue({
      requires_verification: true,
      user: { id: "u", email: "u@x.com", tier: "ultra", email_verified: false },
      ...response,
    });
    capabilitiesMock.mockResolvedValue({ public_registration: true });
    render(<RegisterPage />);
    await screen.findByPlaceholderText("you@company.com");
    _fillAndSubmit();
  }

  it("queued keeps the check-your-email card without a warning", async () => {
    await registerWith({ verification_mail: { status: "queued" }, verification_receipt: "rcpt" });
    expect(await screen.findByRole("heading", { name: "verify.sent_title" })).toBeTruthy();
    expect(screen.getByText("verify.pending_message")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("not_sent says the account exists, names the settings to check and the reference", async () => {
    await registerWith({
      verification_mail: { status: "not_sent", reason: "email_not_configured", reference: "req123" },
      verification_receipt: "rcpt",
    });
    expect(await screen.findByRole("heading", { name: "verify.account_ready_title" })).toBeTruthy();
    // Never claim a mail was sent when the server says it was not.
    expect(screen.queryByText("verify.pending_message")).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("verify.not_sent_email_settings|u@x.com|req123");
  });

  it("an unaddressable link asks the administrator to check the application address", async () => {
    await registerWith({
      verification_mail: { status: "not_sent", reason: "public_entry_required", reference: "req9" },
    });
    expect((await screen.findByRole("alert")).textContent).toContain("verify.not_sent_app_address|u@x.com|req9");
  });

  it("a receipt-backed resend shows the real outcome", async () => {
    resendMock.mockResolvedValueOnce({ ok: true, verification_mail: { status: "not_sent", reason: "delivery_unavailable", reference: "req2" } });
    await registerWith({
      verification_mail: { status: "not_sent", reason: "public_entry_required", reference: "req1" },
      verification_receipt: "rcpt",
    });
    await screen.findByRole("heading", { name: "verify.account_ready_title" });
    fireEvent.click(screen.getByRole("button", { name: "verify.resend" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("verify.not_sent_email_settings|u@x.com|req2"));
    expect(resendMock).toHaveBeenCalledWith("u@x.com", "rcpt");

    resendMock.mockResolvedValueOnce({ ok: true, verification_mail: { status: "queued" } });
    cleanup();
    await registerWith({
      verification_mail: { status: "not_sent", reason: "email_not_configured", reference: "req3" },
      verification_receipt: "rcpt",
    });
    await screen.findByRole("heading", { name: "verify.account_ready_title" });
    fireEvent.click(screen.getByRole("button", { name: "verify.resend" }));
    expect(await screen.findByRole("heading", { name: "verify.sent_title" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a resend without an outcome (expired receipt) replaces not_sent with a neutral notice", async () => {
    resendMock.mockResolvedValueOnce({ ok: true });
    await registerWith({
      verification_mail: { status: "not_sent", reason: "email_not_configured", reference: "req4" },
      verification_receipt: "expired",
    });
    await screen.findByRole("heading", { name: "verify.account_ready_title" });
    fireEvent.click(screen.getByRole("button", { name: "verify.resend" }));
    const notice = await screen.findByText("verify.resend_requested");
    expect(notice.closest("[role]")?.getAttribute("role")).toBe("status");
    // The old warning and its reference are gone; nothing claims the mail was sent.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/req4/)).toBeNull();
    expect(screen.getByRole("heading", { name: "verify.account_ready_title" })).toBeTruthy();
    expect(screen.queryByText("verify.pending_message")).toBeNull();
    // Inside the resend cooldown the button keeps the neutral label instead of "Sent".
    await waitFor(() => expect(screen.getByRole("button", { name: "verify.resend" }).hasAttribute("disabled")).toBe(true));
    expect(screen.queryByRole("button", { name: "verify.resent" })).toBeNull();
  });

  it("a receipt-backed resend inside the cooldown asks the user to wait a minute", async () => {
    resendMock.mockRejectedValueOnce(new AuthApiError(429, "too frequent", "resend_too_frequent"));
    await registerWith({
      verification_mail: { status: "not_sent", reason: "email_not_configured", reference: "req5" },
      verification_receipt: "rcpt",
    });
    await screen.findByRole("heading", { name: "verify.account_ready_title" });
    fireEvent.click(screen.getByRole("button", { name: "verify.resend" }));
    expect(await screen.findByText("verify.resend_too_frequent")).toBeTruthy();
    expect(screen.queryByText("verify.resend_failed")).toBeNull();
  });
});
