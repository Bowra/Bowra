/**
 * Tests for useAuthRedirect — exercised through a tiny harness component
 * (no renderHook needed; the hook returns simple state we assert via DOM).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { AuthError } from "@/lib/auth-api";
import { useAuthRedirect } from "./use-auth-redirect";

const fetchMeMock = vi.fn();
const bootstrapStatusMock = vi.fn();

vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>(
    "@/lib/auth-api",
  );
  return {
    ...actual,
    fetchMe: (...args: unknown[]) => fetchMeMock(...args),
    getBootstrapStatus: (...args: unknown[]) => bootstrapStatusMock(...args),
  };
});

const baseUser = {
  id: "u1",
  email: "u@example.com",
  display_name: null,
  avatar_url: null,
  team_id: "org1",
  tier: "ultra" as const,
  role: "member",
  status: "active",
  email_verified: true,
  credit_balance: 0,
  credit_quota: 0,
  locale: "en",
  timezone: "UTC",
  is_team_admin: false,
  workspace_count: 1,
  needs_onboarding: false,
};

function Harness({
  requireTeamAdmin,
  allowedTeamRoles,
  requiredCapabilities,
}: {
  requireTeamAdmin?: boolean;
  allowedTeamRoles?: NonNullable<Parameters<typeof useAuthRedirect>[0]>["allowedTeamRoles"];
  requiredCapabilities?: string[];
}) {
  const r = useAuthRedirect({ requireTeamAdmin, allowedTeamRoles, requiredCapabilities });
  return (
    <div>
      <span data-testid="status">{r.status}</span>
      <span data-testid="admin">{String(r.isTeamAdmin)}</span>
      <span data-testid="err">{r.error ?? ""}</span>
    </div>
  );
}

const origLocation = window.location;

beforeEach(() => {
  fetchMeMock.mockReset();
  bootstrapStatusMock.mockReset();
  // jsdom: stub location.replace so we can observe redirect calls.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      ...origLocation,
      pathname: "/admin/credits",
      search: "",
      replace: vi.fn(),
    },
  });
});

afterEach(() => {
  cleanup();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: origLocation,
  });
});

describe("useAuthRedirect", () => {
  it("authed user → status=authed", async () => {
    fetchMeMock.mockResolvedValue({ ...baseUser, is_team_admin: false });
    render(<Harness />);
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authed"),
    );
    expect(screen.getByTestId("admin").textContent).toBe("false");
  });

  it("requireTeamAdmin + non-admin → status=not_admin", async () => {
    fetchMeMock.mockResolvedValue({ ...baseUser, is_team_admin: false });
    render(<Harness requireTeamAdmin />);
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("not_admin"),
    );
  });

  it("requireTeamAdmin + admin → status=authed", async () => {
    fetchMeMock.mockResolvedValue({ ...baseUser, is_team_admin: true });
    render(<Harness requireTeamAdmin />);
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authed"),
    );
    expect(screen.getByTestId("admin").textContent).toBe("true");
  });

  it("requiredCapabilities allows team admin with matching credit capability", async () => {
    fetchMeMock.mockResolvedValue({
      ...baseUser,
      team_role: "team_admin",
      credit_capabilities: {
        credits_read: true,
        credit_grant: true,
        credit_user_quota_write: true,
        credit_user_tier_write: true,
        credit_period_reset: true,
        credit_team_quota_write: true,
        credit_attention_dismiss: true,
      },
    });
    render(<Harness requiredCapabilities={["credits_read", "credit:credit_grant"]} />);
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authed"),
    );
  });

  it("requiredCapabilities blocks when credit capability is missing", async () => {
    fetchMeMock.mockResolvedValue({
      ...baseUser,
      team_role: "audit_admin",
      credit_capabilities: {
        credits_read: false,
        credit_grant: false,
        credit_user_quota_write: false,
        credit_user_tier_write: false,
        credit_period_reset: false,
        credit_team_quota_write: false,
        credit_attention_dismiss: false,
      },
    });
    render(<Harness requiredCapabilities={["credit_user_tier_write"]} />);
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("not_admin"),
    );
  });

  it("401 → redirects to /login/?next=<currentPath>", async () => {
    fetchMeMock.mockRejectedValue(new AuthError(401, "unauthorized"));
    render(<Harness />);
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("redirecting"),
    );
    await waitFor(() => expect(window.location.replace).toHaveBeenCalledWith(
      "/login/?next=" + encodeURIComponent("/admin/credits"),
    ));
  });

  it("non-401 error → status=error with message", async () => {
    fetchMeMock.mockRejectedValue(new Error("network down"));
    render(<Harness />);
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("error"),
    );
    expect(screen.getByTestId("err").textContent).toBe("network down");
    expect(window.location.replace).not.toHaveBeenCalled();
  });

  it("421 on a pristine local instance → redirects to activation", async () => {
    fetchMeMock.mockRejectedValue(
      new AuthError(421, "browser public URL is not registered"),
    );
    bootstrapStatusMock.mockResolvedValue({
      required: true,
      deployment_mode: "local",
      license_team_ids: [],
    });

    render(<Harness />);

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("redirecting"),
    );
    expect(window.location.replace).toHaveBeenCalledWith("/activate/");
  });

  it("421 on an initialized instance remains an entry error", async () => {
    fetchMeMock.mockRejectedValue(
      new AuthError(421, "browser public URL is not registered"),
    );
    bootstrapStatusMock.mockRejectedValue(
      new AuthError(421, "browser public URL is not registered"),
    );

    render(<Harness />);

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("error"),
    );
    expect(screen.getByTestId("err").textContent).toBe(
      "browser public URL is not registered",
    );
    expect(window.location.replace).not.toHaveBeenCalled();
  });
});
