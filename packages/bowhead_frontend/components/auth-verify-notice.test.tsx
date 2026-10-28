/**
 * §C (real-auth) — AuthVerifyNotice (Task #8 FE): resend wiring + sent/error states.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AuthVerifyNotice } from "./auth-verify-notice";

const resendMock = vi.fn();
vi.mock("@/lib/auth-api", () => ({
  resendVerificationEmail: (...a: unknown[]) => resendMock(...a),
}));
vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AuthVerifyNotice", () => {
  it("resend button calls resendVerificationEmail(email) and shows the resent confirmation", async () => {
    resendMock.mockResolvedValue({ ok: true });
    render(<AuthVerifyNotice email="u@x.com" />);
    fireEvent.click(screen.getByText("verify.resend"));
    await waitFor(() => expect(resendMock).toHaveBeenCalledWith("u@x.com"));
    await waitFor(() => expect(screen.getByText("verify.resent")).toBeTruthy());
  });

  it("shows the error copy when resend throws (transport failure)", async () => {
    resendMock.mockRejectedValue(new Error("network"));
    render(<AuthVerifyNotice email="u@x.com" />);
    fireEvent.click(screen.getByText("verify.resend"));
    await waitFor(() => expect(screen.getByText("verify.resend_failed")).toBeTruthy());
  });
});
