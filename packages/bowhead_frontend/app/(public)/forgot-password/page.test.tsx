import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  forgotPassword: vi.fn(),
}));

vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>(
    "@/lib/auth-api",
  );
  return {
    ...actual,
    forgotPassword: (...args: unknown[]) => mocks.forgotPassword(...args),
  };
});

import ForgotPage from "./page";
import { AuthError as AuthApiError } from "@/lib/auth-api";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ForgotPage errors", () => {
  it("shows a localized rate-limit reason without exposing the backend code", async () => {
    mocks.forgotPassword.mockRejectedValue(
      new AuthApiError(429, "rate_limited", "rate_limited"),
    );

    renderIntl(<ForgotPage />, { locale: "en-US" });
    fireEvent.change(screen.getByPlaceholderText("you@company.com"), {
      target: { value: "member@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(
      await screen.findByText(/Too many reset requests were sent/),
    ).toBeTruthy();
    expect(screen.queryByText(/rate_limited/)).toBeNull();
  });

  it("uses a localized safe fallback for a network failure", async () => {
    mocks.forgotPassword.mockRejectedValue(new Error("Failed to fetch"));

    renderIntl(<ForgotPage />);
    fireEvent.change(screen.getByPlaceholderText("you@company.com"), {
      target: { value: "member@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送重设链接" }));

    expect(await screen.findByText("无法发送密码重置请求，请稍后重试。")).toBeTruthy();
    expect(screen.queryByText(/Failed to fetch/)).toBeNull();
  });

  it("explains when the email service has not been configured", async () => {
    mocks.forgotPassword.mockRejectedValue(
      new AuthApiError(503, "email_not_configured", "email_not_configured"),
    );

    renderIntl(<ForgotPage />);
    fireEvent.change(screen.getByPlaceholderText("you@company.com"), {
      target: { value: "member@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送重设链接" }));

    expect(await screen.findByText("暂时无法发送邮件：这个团队还没有启用邮件服务。请联系团队负责人或管理员，他们可以在成员页为你生成密码重设链接。")).toBeTruthy();
  });

  it("asks the user to retry when delivery is unavailable", async () => {
    mocks.forgotPassword.mockRejectedValue(
      new AuthApiError(503, "email_delivery_unavailable", "email_delivery_unavailable"),
    );

    renderIntl(<ForgotPage />);
    fireEvent.change(screen.getByPlaceholderText("you@company.com"), {
      target: { value: "member@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送重设链接" }));

    expect(await screen.findByText("邮件服务暂时繁忙或不可用，请稍后重试。")).toBeTruthy();
  });

  it("gives the email field an accessible name and reports format errors next to it", async () => {
    renderIntl(<ForgotPage />, { locale: "en-US" });
    const email = screen.getByLabelText("Email");
    fireEvent.change(email, { target: { value: "li.ming@northstar" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    const message = await screen.findByText(/doesn't look like an email address/);
    expect(email).toHaveAttribute("aria-invalid", "true");
    const describedBy = email.getAttribute("aria-describedby") ?? "";
    expect(describedBy.split(" ")).toContain(message.closest("p")?.id);
    expect(document.activeElement).toBe(email);
    expect(mocks.forgotPassword).not.toHaveBeenCalled();
  });

  it("replaces the form with a result card that names the next step", async () => {
    mocks.forgotPassword.mockResolvedValue({ ok: true, message: "ok" });
    renderIntl(<ForgotPage />, { locale: "en-US" });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "member@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    const heading = await screen.findByRole("heading", { name: "Check your email" });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(screen.getByText(/If member@example.com is registered/)).toBeTruthy();
    expect(screen.queryByLabelText("Email")).toBeNull();
    expect(screen.getByRole("link", { name: "Back to sign-in" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Use a different email" }));
    expect(screen.getByLabelText("Email")).toHaveValue("member@example.com");
  });
});
