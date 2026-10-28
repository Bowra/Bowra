import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  fetchTeams: vi.fn(),
  fetchWorkspaces: vi.fn(),
  readInviteResult: vi.fn(),
  replace: vi.fn(),
  searchParams: new URLSearchParams(),
  setLastTeam: vi.fn(),
  switchActiveSpace: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace }),
  useSearchParams: () => mocks.searchParams,
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchWorkspaces: (...args: unknown[]) => mocks.fetchWorkspaces(...args),
  };
});

vi.mock("@/lib/invite-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/invite-api")>("@/lib/invite-api");
  return {
    ...actual,
    readInviteResult: () => mocks.readInviteResult(),
  };
});

vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>("@/lib/auth-api");
  return {
    ...actual,
    fetchTeams: (...args: unknown[]) => mocks.fetchTeams(...args),
    setLastTeam: (...args: unknown[]) => mocks.setLastTeam(...args),
  };
});

vi.mock("@/lib/use-space", async () => {
  const actual = await vi.importActual<typeof import("@/lib/use-space")>("@/lib/use-space");
  return {
    ...actual,
    switchActiveSpace: (...args: unknown[]) => mocks.switchActiveSpace(...args),
  };
});

import InviteReadyPage from "./page";

const origLocation = window.location;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mocks.searchParams = new URLSearchParams();
  window.__BOWHEAD_BASE_PATH__ = undefined;
  window.__BOWHEAD_BASE_URL__ = undefined;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: origLocation,
  });
});

describe("InviteReadyPage", () => {
  it("activates the invited workspace before entering Bowhead", () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...origLocation,
        pathname: "/ai/apps/bowhead/invite/ready/",
        search: "?state=workspace_ready",
        replace: vi.fn(),
      },
    });
    window.__BOWHEAD_BASE_PATH__ = "/ai/apps/bowhead";
    mocks.searchParams = new URLSearchParams("state=workspace_ready");
    mocks.readInviteResult.mockReturnValue({
      invite_type: "workspace",
      next_step: "workspace_ready",
      next_route: "/onboarding/profile?source=invite",
      ready_route: "/invite/ready?state=workspace_ready",
      workspace: { id: "ws_1", name: "Market Lab", role: "member" },
      team: { name: "Datarc Strategy" },
    });

    renderIntl(<InviteReadyPage />, { locale: "en-US" });

    fireEvent.click(screen.getByText("Enter space"));
    expect(mocks.switchActiveSpace).toHaveBeenCalledWith("ws_1");
    expect(window.location.replace).toHaveBeenCalledWith("/ai/apps/bowhead/");
    expect(mocks.replace).not.toHaveBeenCalled();
    expect(mocks.fetchWorkspaces).not.toHaveBeenCalled();
  });

  it("switches to the invited team only when the user chooses the ready CTA", async () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...origLocation,
        pathname: "/ai/apps/bowhead/invite/ready/",
        search: "?state=workspace_ready",
        replace: vi.fn(),
      },
    });
    window.__BOWHEAD_BASE_PATH__ = "/ai/apps/bowhead";
    mocks.searchParams = new URLSearchParams("state=workspace_ready");
    mocks.readInviteResult.mockReturnValue({
      invite_type: "workspace",
      next_step: "workspace_ready",
      next_route: "/onboarding/profile?source=invite",
      ready_route: "/invite/ready?state=workspace_ready",
      workspace: { id: "ws_1", name: "Market Lab", role: "member" },
      team: { id: "team_invited", name: "Datarc Strategy" },
    });
    mocks.fetchTeams.mockResolvedValue({
      active_team_id: "team_original",
      items: [],
    });
    mocks.setLastTeam.mockResolvedValue({ ok: "true", team_id: "team_invited" });

    renderIntl(<InviteReadyPage />, { locale: "en-US" });

    expect(mocks.setLastTeam).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Enter space"));

    await waitFor(() => expect(mocks.setLastTeam).toHaveBeenCalledWith("team_invited"));
    expect(mocks.switchActiveSpace).toHaveBeenCalledWith("ws_1");
    expect(window.location.replace).toHaveBeenCalledWith("/ai/apps/bowhead/");
    expect(mocks.replace).not.toHaveBeenCalled();
  });
});
