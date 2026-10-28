import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  fetchWithRefresh: vi.fn(),
  readInviteResult: vi.fn(),
  replace: vi.fn(),
  searchParams: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace }),
  useSearchParams: () => mocks.searchParams,
}));

vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>("@/lib/auth-api");
  return {
    ...actual,
    fetchWithRefresh: (...args: unknown[]) => mocks.fetchWithRefresh(...args),
  };
});

vi.mock("@/lib/invite-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/invite-api")>("@/lib/invite-api");
  return {
    ...actual,
    readInviteResult: () => mocks.readInviteResult(),
  };
});

import InviteProfileOnboardingPage from "./page";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mocks.searchParams = new URLSearchParams();
  window.history.pushState({}, "", "/onboarding/profile");
});

describe("InviteProfileOnboardingPage", () => {
  it("falls back to the stored ready route when URL next is unsafe", async () => {
    mocks.searchParams = new URLSearchParams("next=%2Fbad%5Cpath");
    mocks.readInviteResult.mockReturnValue({
      invite_type: "workspace",
      next_route: "/onboarding/profile?source=invite",
      ready_route: "/invite/ready?state=workspace_ready",
      workspace: { id: "ws_1", name: "Market Lab", role: "member" },
    });
    mocks.fetchWithRefresh.mockImplementation((_url: unknown, init?: { method?: string }) => {
      if (init?.method === "PATCH") {
        return Promise.resolve(new Response(null, { status: 200 }));
      }
      return Promise.resolve(jsonResponse({ display_name: "Ada", preferences: { theme: "light" } }));
    });

    renderIntl(<InviteProfileOnboardingPage />, { locale: "en-US" });

    await waitFor(() => expect(mocks.fetchWithRefresh).toHaveBeenCalledWith("/api/v1/profile/me"));
    await screen.findByDisplayValue("Ada");
    fireEvent.click(screen.getByText("Continue"));

    await waitFor(() => {
      expect(mocks.fetchWithRefresh).toHaveBeenCalledWith(
        "/api/v1/profile/me",
        expect.objectContaining({ method: "PATCH" }),
      );
      expect(mocks.replace).toHaveBeenCalledWith("/invite/ready/?state=workspace_ready");
    });
  });

  it("infers workspace invite context from the URL when stored result is missing", async () => {
    mocks.searchParams = new URLSearchParams("next=%2Finvite%2Fready%3Fstate%3Dworkspace_ready");
    mocks.readInviteResult.mockReturnValue(null);
    mocks.fetchWithRefresh.mockImplementation((_url: unknown, init?: { method?: string }) => {
      if (init?.method === "PATCH") {
        return Promise.resolve(new Response(null, { status: 200 }));
      }
      return Promise.resolve(jsonResponse({ display_name: "Ada", preferences: {} }));
    });

    renderIntl(<InviteProfileOnboardingPage />, { locale: "en-US" });

    await screen.findByText("Space invite");
    fireEvent.click(screen.getByText("Skip"));

    await waitFor(() => {
      expect(mocks.fetchWithRefresh).toHaveBeenCalledWith(
        "/api/v1/profile/me",
        expect.objectContaining({
          method: "PATCH",
          body: expect.stringContaining('"invite_type":"workspace"'),
        }),
      );
      expect(mocks.replace).toHaveBeenCalledWith("/invite/ready/?state=workspace_ready");
    });
  });

  it("renders the invite profile step in Chinese", async () => {
    mocks.readInviteResult.mockReturnValue({
      invite_type: "workspace",
      next_route: "/onboarding/profile?source=invite",
      ready_route: "/invite/ready?state=workspace_ready",
      workspace: { id: "ws_1", name: "Market Lab", role: "member" },
    });
    mocks.fetchWithRefresh.mockResolvedValue(
      jsonResponse({ display_name: "Ada", preferences: {} }),
    );

    renderIntl(<InviteProfileOnboardingPage />, { locale: "zh-CN" });

    expect(await screen.findByText("让 Bowhead 更贴合你的工作")).toBeTruthy();
    expect(screen.queryByText("Create your profile")).toBeNull();
  });
});
