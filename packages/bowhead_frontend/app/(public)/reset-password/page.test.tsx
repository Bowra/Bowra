import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  completeCredentialRecovery: vi.fn(),
  exchangeCredentialRecovery: vi.fn(),
  push: vi.fn(),
  resetPassword: vi.fn(),
  searchParams: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push, replace: mocks.push }),
  useSearchParams: () => mocks.searchParams,
}));
vi.mock("@/lib/auth-api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/auth-api")>("@/lib/auth-api");
  return {
    ...actual,
    completeCredentialRecovery: (...args: unknown[]) =>
      mocks.completeCredentialRecovery(...args),
    exchangeCredentialRecovery: (...args: unknown[]) =>
      mocks.exchangeCredentialRecovery(...args),
    resetPassword: (...args: unknown[]) => mocks.resetPassword(...args),
  };
});

import ResetPage from "./page";
import { AuthError as AuthApiError } from "@/lib/auth-api";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
  mocks.searchParams = new URLSearchParams();
  window.history.replaceState(null, "", "/reset-password/");
});

function fillPasswords(first: string, second = first) {
  const passwords = document.querySelectorAll("input[type=password]");
  fireEvent.change(passwords[0], { target: { value: first } });
  fireEvent.change(passwords[1], { target: { value: second } });
  return passwords[0] as HTMLInputElement;
}

describe("ResetPage credential recovery", () => {
  it("keeps an admin exchange alive when URL cleanup updates search params", async () => {
    let resolveExchange:
      | ((value: {
          ok: boolean;
          status: "ready";
        }) => void)
      | undefined;
    mocks.searchParams = new URLSearchParams(
      "flow=admin&token=raw-recovery-token",
    );
    window.history.replaceState(
      null,
      "",
      "/reset-password/?flow=admin&token=raw-recovery-token",
    );
    mocks.exchangeCredentialRecovery.mockReturnValue(
      new Promise((resolve) => {
        resolveExchange = resolve;
      }),
    );
    mocks.completeCredentialRecovery.mockResolvedValue({
      ok: true,
      login_required: true,
    });

    renderIntl(<ResetPage />);

    await waitFor(() => {
      expect(mocks.exchangeCredentialRecovery).toHaveBeenCalledWith(
        "raw-recovery-token",
      );
    });
    expect(window.location.search).toBe("");
    // While checking, the unusable form is not shown
    expect(screen.getByRole("heading", { name: "正在检查链接" })).toBeTruthy();
    expect(document.querySelectorAll("input[type=password]")).toHaveLength(0);

    mocks.searchParams = new URLSearchParams();
    expect(resolveExchange).toBeTypeOf("function");
    resolveExchange?.({
      ok: true,
      status: "ready",
    });

    const submit = await screen.findByRole("button", { name: "保存新密码" });
    expect(submit).toBeEnabled();
    expect(mocks.exchangeCredentialRecovery).toHaveBeenCalledTimes(1);
    const first = fillPasswords("NewPassw0rd!");
    fireEvent.submit(first.closest("form") as HTMLFormElement);

    await waitFor(() => {
      expect(mocks.completeCredentialRecovery).toHaveBeenCalledWith(
        "NewPassw0rd!",
      );
    });
    // An admin-generated recovery link requires signing in afterwards: the button says "Go to sign-in" and its target matches
    const heading = await screen.findByRole("heading", { name: "密码已更新" });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(screen.getByText("请使用新密码登录。")).toBeTruthy();
    expect(screen.getByRole("link", { name: "去登录" })).toHaveAttribute("href", "/login/");
  });

  it("finishes a self-service reset with a result card instead of an automatic redirect", async () => {
    mocks.searchParams = new URLSearchParams(
      "flow=self_service&token=email-reset-jwt",
    );
    window.history.replaceState(
      null,
      "",
      "/reset-password/?flow=self_service&token=email-reset-jwt",
    );
    mocks.resetPassword.mockResolvedValue({ ok: true });

    renderIntl(<ResetPage />);

    const submit = await screen.findByRole("button", { name: "保存新密码" });
    expect(submit).toBeEnabled();
    expect(window.location.search).toBe("");
    expect(mocks.exchangeCredentialRecovery).not.toHaveBeenCalled();
    const first = fillPasswords("NewPassw0rd!");
    fireEvent.submit(first.closest("form") as HTMLFormElement);

    await waitFor(() => {
      expect(mocks.resetPassword).toHaveBeenCalledWith(
        "email-reset-jwt",
        "NewPassw0rd!",
      );
    });
    expect(await screen.findByRole("heading", { name: "密码已更新" })).toBeTruthy();
    expect(screen.getByText("你已使用新密码登录。")).toBeTruthy();
    expect(screen.getByRole("link", { name: "进入 Bowhead" })).toHaveAttribute("href", "/");
    await new Promise((resolve) => setTimeout(resolve, 2000));
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it.each([
    ["an old link without flow", "token=old-reset-token"],
    ["an unknown flow", "flow=legacy&token=reset-token"],
    ["an empty token", "flow=self_service"],
  ])("shows only a result card for %s without calling a recovery API", async (_case, query) => {
    mocks.searchParams = new URLSearchParams(query);
    window.history.replaceState(null, "", `/reset-password/?${query}`);

    renderIntl(<ResetPage />);

    expect(await screen.findByRole("heading", { name: "链接已失效" })).toBeTruthy();
    expect(screen.getByText(/重设链接只能用一次，且会过期/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "重新申请重设链接" })).toHaveAttribute(
      "href",
      "/forgot-password/",
    );
    expect(document.querySelectorAll("input")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "保存新密码" })).toBeNull();
    expect(window.location.search).toBe("");
    expect(mocks.exchangeCredentialRecovery).not.toHaveBeenCalled();
    expect(mocks.resetPassword).not.toHaveBeenCalled();
    expect(mocks.completeCredentialRecovery).not.toHaveBeenCalled();
  });

  it("shows an English recovery action without exposing an expired-link code", async () => {
    mocks.searchParams = new URLSearchParams(
      "flow=admin&token=expired-recovery-token",
    );
    mocks.exchangeCredentialRecovery.mockRejectedValue(
      new AuthApiError(
        410,
        "recovery_invalid_or_expired",
        "recovery_invalid_or_expired",
      ),
    );

    renderIntl(<ResetPage />, { locale: "en-US" });

    expect(
      await screen.findByRole("heading", { name: "This link no longer works" }),
    ).toBeTruthy();
    expect(screen.getByText(/invalid, expired, revoked, or already used/i)).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Request a new reset link" }),
    ).toBeTruthy();
    expect(document.querySelectorAll("input")).toHaveLength(0);
    expect(screen.queryByText(/recovery_invalid_or_expired/)).toBeNull();
  });

  it("switches to the result card when the HttpOnly intent has expired on submit", async () => {
    mocks.searchParams = new URLSearchParams(
      "flow=admin&token=ready-recovery-token",
    );
    mocks.exchangeCredentialRecovery.mockResolvedValue({
      ok: true,
      status: "ready",
    });
    mocks.completeCredentialRecovery.mockRejectedValue(
      new AuthApiError(
        401,
        "recovery_intent_missing",
        "recovery_intent_missing",
      ),
    );

    renderIntl(<ResetPage />);

    await screen.findByRole("button", { name: "保存新密码" });
    const first = fillPasswords("NewPassw0rd!");
    fireEvent.submit(first.closest("form") as HTMLFormElement);

    expect(await screen.findByText(/密码重置会话已失效/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "链接已失效" })).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "重新申请重设链接" }),
    ).toBeTruthy();
    expect(document.querySelectorAll("input")).toHaveLength(0);
    expect(screen.queryByText(/recovery_intent_missing/)).toBeNull();
  });

  it("reports unmet password rules and a mismatch next to the fields", async () => {
    mocks.searchParams = new URLSearchParams(
      "flow=self_service&token=email-reset-jwt",
    );
    renderIntl(<ResetPage />, { locale: "en-US" });
    await screen.findByRole("button", { name: "Save new password" });

    const newPassword = screen.getByLabelText("New password");
    const confirm = screen.getByLabelText("Confirm password");
    fireEvent.change(newPassword, { target: { value: "abcdefgh" } });
    fireEvent.change(confirm, { target: { value: "abcdefgx" } });
    fireEvent.blur(confirm);
    const mismatch = await screen.findByText("The passwords don't match");
    expect(confirm).toHaveAttribute("aria-invalid", "true");
    expect(confirm.getAttribute("aria-describedby")).toContain(mismatch.closest("p")?.id);

    fireEvent.submit(newPassword.closest("form") as HTMLFormElement);
    const rules = await screen.findByText("The password doesn't meet the requirements above yet");
    const describedBy = (newPassword.getAttribute("aria-describedby") ?? "").split(" ");
    expect(describedBy).toContain(rules.closest("p")?.id);
    // The rule list itself is described-by the input so screen readers hear which rules are still missing
    expect(describedBy.some((id) => document.getElementById(id)?.textContent?.includes("Contains a number"))).toBe(true);
    expect(document.activeElement).toBe(newPassword);
    expect(mocks.resetPassword).not.toHaveBeenCalled();
  });
});
