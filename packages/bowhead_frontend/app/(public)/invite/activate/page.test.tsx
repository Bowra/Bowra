import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  activateInvite: vi.fn(),
  continueInviteIntent: vi.fn(),
  replace: vi.fn(),
  storeInviteResult: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace }),
}));
vi.mock("@/lib/invite-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/invite-api")>("@/lib/invite-api");
  return {
    ...actual,
    activateInvite: (...args: unknown[]) => mocks.activateInvite(...args),
    continueInviteIntent: (...args: unknown[]) => mocks.continueInviteIntent(...args),
    storeInviteResult: (...args: unknown[]) => mocks.storeInviteResult(...args),
  };
});

import InviteActivatePage from "./page";
import { InviteApiError } from "@/lib/invite-api";

const origLocation = window.location;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.__BOWHEAD_BASE_PATH__ = undefined;
  window.__BOWHEAD_BASE_URL__ = undefined;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: origLocation,
  });
});

describe("InviteActivatePage", () => {
  it("shows only the masked invited email and submits user-owned profile fields", async () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...origLocation,
        pathname: "/ai/apps/bowhead/invite/activate/",
        search: "",
        replace: vi.fn(),
      },
    });
    window.__BOWHEAD_BASE_PATH__ = "/ai/apps/bowhead";
    mocks.continueInviteIntent.mockResolvedValue({
      status: "ready",
      invite_type: "team",
      invited_email_mask: "n***@example.com",
      team: { name: "Strategy" },
      next_action: "activate_password",
    });
    const inviteResult = {
      invite_type: "team",
      next_route: "/invite/ready?state=team_ready",
    };
    mocks.activateInvite.mockResolvedValue({
      ok: true,
      invite_result: inviteResult,
    });

    renderIntl(<InviteActivatePage />, { locale: "en-US" });

    const invited = await screen.findByLabelText("Invited email");
    expect(invited).toHaveValue("n***@example.com");
    expect(invited).toHaveAttribute("readonly");
    expect(document.querySelector("input[type=email]")).toBeNull();
    const name = screen.getByLabelText("Display name") as HTMLInputElement;
    const passwords = document.querySelectorAll("input[type=password]");
    fireEvent.change(name, { target: { value: "New User" } });
    fireEvent.change(passwords[0], { target: { value: "Passw0rd!" } });
    fireEvent.change(passwords[1], { target: { value: "Passw0rd!" } });
    fireEvent.submit(passwords[0].closest("form") as HTMLFormElement);

    await waitFor(() => {
      expect(mocks.activateInvite).toHaveBeenCalledWith({
        display_name: "New User",
        password: "Passw0rd!",
      });
    });
    expect(mocks.storeInviteResult).toHaveBeenCalledWith(inviteResult);
    expect(window.location.replace).toHaveBeenCalledWith(
      "/ai/apps/bowhead/invite/ready/?state=team_ready",
    );
    expect(mocks.replace).not.toHaveBeenCalled();
  });

  it("shows a localized activation error without exposing backend copy", async () => {
    const code = "password_digit_required";
    const message = "RAW_BACKEND_PASSWORD_RULE";
    mocks.continueInviteIntent.mockResolvedValue({
      status: "ready",
      invite_type: "team",
      invited_email_mask: "n***@example.com",
      team: { name: "Strategy" },
      next_action: "activate_password",
    });
    mocks.activateInvite.mockRejectedValue(
      new InviteApiError(422, message, code),
    );

    renderIntl(<InviteActivatePage />, { locale: "en-US" });
    expect(await screen.findByLabelText("Invited email")).toHaveValue("n***@example.com");
    const passwords = document.querySelectorAll("input[type=password]");
    fireEvent.change(passwords[0], { target: { value: "abcdefg1" } });
    fireEvent.change(passwords[1], { target: { value: "abcdefg1" } });
    fireEvent.submit(passwords[0].closest("form") as HTMLFormElement);

    expect(
      await screen.findByText(
        "We could not activate this invitation. Reopen the invitation link or contact an administrator.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(message)).not.toBeInTheDocument();
    expect(screen.queryByText(code)).not.toBeInTheDocument();
  });
});
