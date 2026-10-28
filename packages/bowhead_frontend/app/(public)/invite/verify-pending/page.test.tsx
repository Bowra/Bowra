import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  readInvitePreview: vi.fn(),
  readInviteResult: vi.fn(),
  readInviteVerifyEmail: vi.fn(),
  resendVerificationEmail: vi.fn(),
  searchParams: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => mocks.searchParams,
}));

vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>("@/lib/auth-api");
  return {
    ...actual,
    resendVerificationEmail: (...args: unknown[]) => mocks.resendVerificationEmail(...args),
  };
});

vi.mock("@/lib/invite-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/invite-api")>("@/lib/invite-api");
  return {
    ...actual,
    readInvitePreview: () => mocks.readInvitePreview(),
    readInviteResult: () => mocks.readInviteResult(),
    readInviteVerifyEmail: () => mocks.readInviteVerifyEmail(),
  };
});

import InviteVerifyPendingPage from "./page";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mocks.searchParams = new URLSearchParams();
});

describe("InviteVerifyPendingPage", () => {
  it("links login back to profile setup and then the invite ready route", () => {
    mocks.readInviteResult.mockReturnValue({
      invite_type: "workspace",
      next_route: "/invite/verify-pending",
      ready_route: "/invite/ready?state=workspace_ready",
      workspace: { id: "ws_1", name: "Market Lab", role: "member" },
      team: { name: "Datarc Strategy" },
    });
    mocks.readInvitePreview.mockReturnValue({
      status: "valid",
      invite_type: "workspace",
      invited_email_mask: "a***@datarc.test",
      team: { name: "Datarc Strategy" },
      workspace: { name: "Market Lab", role: "member" },
      viewer_context: { state: "anonymous" },
    });
    mocks.readInviteVerifyEmail.mockReturnValue("analyst@datarc.test");

    renderIntl(<InviteVerifyPendingPage />, { locale: "en-US" });

    const href = screen.getByText("Go to sign in").closest("a")?.getAttribute("href");
    expect(href).toBeTruthy();
    const next = new URL(`http://bowhead.test${href}`).searchParams.get("next");
    expect(next).toBe(
      "/onboarding/profile/?source=invite&next=%2Finvite%2Fready%3Fstate%3Dworkspace_ready",
    );
  });

  it("uses URL next and lets users enter email when storage is empty", () => {
    mocks.searchParams = new URLSearchParams("next=%2Finvite%2Fready%3Fstate%3Dworkspace_ready");
    mocks.readInviteResult.mockReturnValue(null);
    mocks.readInvitePreview.mockReturnValue(null);
    mocks.readInviteVerifyEmail.mockReturnValue("");

    renderIntl(<InviteVerifyPendingPage />, { locale: "en-US" });

    expect(screen.getByText("Space access pending verification")).toBeTruthy();
    expect(screen.getByPlaceholderText("you@company.com")).toBeTruthy();
    const href = screen.getByText("Go to sign in").closest("a")?.getAttribute("href");
    const next = new URL(`http://bowhead.test${href}`).searchParams.get("next");
    expect(next).toBe(
      "/onboarding/profile/?source=invite&next=%2Finvite%2Fready%3Fstate%3Dworkspace_ready",
    );
  });
});
