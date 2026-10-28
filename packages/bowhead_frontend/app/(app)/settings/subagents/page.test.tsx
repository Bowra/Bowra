import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import LegacyRedirect from "./page";

const replace = vi.fn();
const useAuthSession = vi.fn();

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ replace }),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useAuthSession: () => useAuthSession(),
}));

describe("legacy subagent settings redirect", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("opens unified governance while the marketplace rollout is active", async () => {
    useAuthSession.mockReturnValue({
      status: "authenticated",
      me: {
        is_team_admin: true,
        team_role: "owner",
        capabilities: { subagent_management_v2: true },
      },
    });

    render(<LegacyRedirect />);

    await waitFor(() => {
      expect(replace).toHaveBeenCalledWith("/agents/manage/governance/");
    });
  });

  it("opens personal management for a member while the rollout is active", async () => {
    useAuthSession.mockReturnValue({
      status: "authenticated",
      me: {
        is_team_admin: false,
        team_role: "member",
        capabilities: { subagent_management_v2: true },
      },
    });

    render(<LegacyRedirect />);

    await waitFor(() => {
      expect(replace).toHaveBeenCalledWith("/agents/manage/");
    });
  });

  it("preserves the legacy manager while the rollout gate is off", async () => {
    useAuthSession.mockReturnValue({
      status: "authenticated",
      me: { capabilities: { subagent_management_v2: false } },
    });

    render(<LegacyRedirect />);

    await waitFor(() => {
      expect(replace).toHaveBeenCalledWith("/team/settings/subagents/");
    });
  });

  it("waits for the authenticated session before redirecting", () => {
    useAuthSession.mockReturnValue({ status: "loading", me: null });

    render(<LegacyRedirect />);

    expect(replace).not.toHaveBeenCalled();
  });
  it("preserves the Owner object and editor section in an old settings link", async () => {
    useAuthSession.mockReturnValue({ status: "authenticated", me: { team_role: "member", capabilities: {} } });
    window.history.replaceState(null, "", "/settings/subagents/?agent_id=osa_1&section=history");
    render(<LegacyRedirect />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/agents/manage/?agent_id=osa_1&section=history"));
  });

});
