import "@testing-library/jest-dom/vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import type { MeUser } from "@/lib/auth-api";
import { AccountDeletionControl } from "./account-deletion";

const { request, redirect } = vi.hoisted(() => ({
  request: vi.fn(),
  redirect: vi.fn(),
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  apiJson: (...args: unknown[]) => request(...args),
}));
vi.mock("@/lib/auth-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth-api")>()),
  clearInvalidSessionAndRedirect: (...args: unknown[]) => redirect(...args),
}));

const user = { email: "member@example.com", has_password: true } as MeUser;

beforeEach(() => {
  request.mockReset().mockResolvedValue({ ok: true });
  redirect.mockReset().mockResolvedValue(undefined);
});

describe("Account deletion", () => {
  it("requires email and password, then retires and signs out", async () => {
    renderIntl(<AccountDeletionControl user={user} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Delete account…" }));
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", {
      name: "Permanently retire account",
    });
    expect(confirm).toBeDisabled();
    fireEvent.change(
      within(dialog).getByLabelText("Enter your account email"),
      { target: { value: user.email } },
    );
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("Current password"), {
      target: { value: "synthetic-password" },
    });
    fireEvent.click(confirm);
    await waitFor(() => expect(request).toHaveBeenCalledOnce());
    const [path, options] = request.mock.calls[0];
    expect(path).toBe("/api/v1/profile");
    expect(options.method).toBe("DELETE");
    expect(JSON.parse(options.body)).toEqual({
      confirm_email: user.email,
      password: "synthetic-password",
    });
    await waitFor(() => expect(redirect).toHaveBeenCalledWith("/"));
  });

  it("shows actionable transfer errors and keeps the account signed in", async () => {
    const { ApiError } = await import("@/lib/api-client");
    request.mockRejectedValue(
      new ApiError(409, "internal diagnostic", {
        code: "account_transfer_required",
      }),
    );
    renderIntl(
      <AccountDeletionControl user={{ ...user, has_password: false }} />,
      { locale: "en-US" },
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete account…" }));
    fireEvent.change(screen.getByLabelText("Enter your account email"), {
      target: { value: user.email },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Permanently retire account" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Transfer ownership or administration in team settings",
      ),
    );
    expect(screen.queryByText("internal diagnostic")).not.toBeInTheDocument();
    expect(redirect).not.toHaveBeenCalled();
  });

  it("forgets confirmation fields when cancelled", () => {
    renderIntl(<AccountDeletionControl user={user} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Delete account…" }));
    fireEvent.change(screen.getByLabelText("Current password"), {
      target: { value: "do-not-retain" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete account…" }));
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(request).not.toHaveBeenCalled();
  });
});
