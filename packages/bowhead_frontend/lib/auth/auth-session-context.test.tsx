import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  revalidateMe: vi.fn(),
  getBootstrapStatus: vi.fn(),
  invalidateMeCache: vi.fn(),
  clearInvalidSessionAndRedirect: vi.fn(),
}));

vi.mock("@/lib/auth-api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/auth-api")>(
    "@/lib/auth-api",
  );
  return {
    ...actual,
    fetchMe: (...args: unknown[]) => api.fetchMe(...args),
    revalidateMe: (...args: unknown[]) => api.revalidateMe(...args),
    getBootstrapStatus: (...args: unknown[]) =>
      api.getBootstrapStatus(...args),
    invalidateMeCache: () => api.invalidateMeCache(),
    clearInvalidSessionAndRedirect: (...args: unknown[]) =>
      api.clearInvalidSessionAndRedirect(...args),
  };
});

import { AuthError, type MeUser } from "@/lib/auth-api";
import {
  AuthSessionProvider,
  useAuthSession,
} from "./auth-session-context";
import { TEAM_CHANGED_EVENT } from "@/lib/team-storage";

const baseUser = {
  id: "u1",
  email: "u@example.com",
  display_name: "User",
  avatar_url: null,
  team_id: "team_1",
  capability_revision: "cap_v1_initial",
  fetched_at: "2026-08-03T00:00:00Z",
  auth_session_generation: "session_generation_0001",
  tier: "pro" as const,
  role: "member",
  status: "active",
  email_verified: true,
  credit_balance: 0,
  credit_quota: 10_000,
  locale: "en",
  timezone: "UTC",
  is_team_admin: false,
  workspace_count: 1,
  needs_onboarding: false,
};

function SessionState() {
  const session = useAuthSession();
  return (
    <div>
      <span data-testid="status">{session.status}</span>
      <span data-testid="email">{session.me?.email ?? ""}</span>
      <span data-testid="planning-capability">
        {session.me?.capabilities?.planning ? "on" : "off"}
      </span>
      <span data-testid="auth-generation">
        {session.me?.auth_session_generation ?? ""}
      </span>
      <button type="button" onClick={session.retry}>
        retry
      </button>
      <button type="button" onClick={() => void session.revalidateCapabilities()}>
        refresh capabilities
      </button>
    </div>
  );
}

const originalLocation = window.location;

function stubLocation(pathname = "/", search = "") {
  const replace = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      ...originalLocation,
      pathname,
      search,
      replace,
    },
  });
  return replace;
}

beforeEach(() => {
  api.fetchMe.mockReset();
  api.revalidateMe.mockReset();
  api.revalidateMe.mockResolvedValue(baseUser);
  api.getBootstrapStatus.mockReset();
  api.invalidateMeCache.mockReset();
  api.clearInvalidSessionAndRedirect.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  window.history.replaceState({}, "", "/");
});

describe("AuthSessionProvider", () => {
  it("owns one session request and exposes the authenticated user", async () => {
    api.fetchMe.mockResolvedValue(baseUser);

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );

    expect(screen.getByTestId("status").textContent).toBe("loading");
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe(
        "authenticated",
      ),
    );
    expect(screen.getByTestId("email").textContent).toBe("u@example.com");
    expect(api.fetchMe).toHaveBeenCalledOnce();
  });

  it("refreshes capabilities without replacing the authenticated tree", async () => {
    let resolveRefresh!: (user: MeUser) => void;
    api.fetchMe.mockResolvedValue(baseUser);
    api.revalidateMe.mockReturnValue(new Promise((resolve) => {
      resolveRefresh = resolve;
    }));

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );
    await screen.findByText("u@example.com");
    const status = screen.getByTestId("status");
    fireEvent.click(screen.getByRole("button", { name: "refresh capabilities" }));
    expect(status.textContent).toBe("authenticated");

    resolveRefresh({
      ...baseUser,
      capabilities: {
        planning: true,
      },
    });

    await waitFor(() => expect(screen.getByTestId("planning-capability").textContent).toBe("on"));
    expect(screen.getByTestId("status")).toBe(status);
  });

  it("adopts an authoritative new auth generation for the same identity", async () => {
    api.fetchMe.mockResolvedValue(baseUser);
    api.revalidateMe.mockResolvedValue({
      ...baseUser,
      auth_session_generation: "session_generation_stale",
      capabilities: {
        planning: true,
      },
    });

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );
    await screen.findByText("u@example.com");
    fireEvent.click(screen.getByRole("button", { name: "refresh capabilities" }));

    await waitFor(() => expect(api.revalidateMe).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.getByTestId("planning-capability").textContent).toBe("on"));
    expect(screen.getByTestId("auth-generation").textContent).toBe("session_generation_stale");
  });

  it("invalidates and reloads identity after a team switch", async () => {
    api.fetchMe
      .mockResolvedValueOnce(baseUser)
      .mockResolvedValueOnce({
        ...baseUser,
        team_id: "team_2",
        email: "next@example.com",
      });

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );
    await screen.findByText("u@example.com");

    window.dispatchEvent(new CustomEvent(TEAM_CHANGED_EVENT));

    await screen.findByText("next@example.com");
    expect(api.invalidateMeCache).toHaveBeenCalledOnce();
    expect(api.fetchMe).toHaveBeenCalledTimes(2);
  });

  it("invalidates the failed attempt before a manual retry", async () => {
    api.fetchMe
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce(baseUser);

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("error"),
    );

    fireEvent.click(screen.getByRole("button", { name: "retry" }));

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe(
        "authenticated",
      ),
    );
    expect(api.invalidateMeCache).toHaveBeenCalledOnce();
    expect(api.fetchMe).toHaveBeenCalledTimes(2);
  });

  it("keeps the fallback mounted while a final 401 redirects", async () => {
    window.history.replaceState({}, "", "/reports/?report=1");
    api.fetchMe.mockRejectedValue(
      new AuthError(401, "unauthorized"),
    );

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe(
        "redirecting",
      ),
    );
    expect(api.clearInvalidSessionAndRedirect).toHaveBeenCalledWith(
      "/reports/?report=1",
    );
  });

  it("redirects users without team membership to access-required", async () => {
    const replace = stubLocation();
    api.fetchMe.mockResolvedValue({
      ...baseUser,
      membership_required: true,
    });

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe(
        "redirecting",
      ),
    );
    expect(replace).toHaveBeenCalledWith("/access-required/");
  });

  it("redirects an unactivated local installation to activation", async () => {
    const replace = stubLocation();
    api.fetchMe.mockRejectedValue(new AuthError(421, "entry required"));
    api.getBootstrapStatus.mockResolvedValue({
      required: true,
      deployment_mode: "local",
    });

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe(
        "redirecting",
      ),
    );
    expect(replace).toHaveBeenCalledWith("/activate/");
  });

  it("ignores an older team identity that resolves after the latest switch", async () => {
    let resolveFirstSwitch!: (user: typeof baseUser) => void;
    let resolveSecondSwitch!: (user: typeof baseUser) => void;
    const firstSwitch = new Promise<typeof baseUser>((resolve) => {
      resolveFirstSwitch = resolve;
    });
    const secondSwitch = new Promise<typeof baseUser>((resolve) => {
      resolveSecondSwitch = resolve;
    });
    api.fetchMe
      .mockResolvedValueOnce(baseUser)
      .mockReturnValueOnce(firstSwitch)
      .mockReturnValueOnce(secondSwitch);

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );
    await screen.findByText("u@example.com");

    window.dispatchEvent(new CustomEvent(TEAM_CHANGED_EVENT));
    window.dispatchEvent(new CustomEvent(TEAM_CHANGED_EVENT));
    resolveSecondSwitch({
      ...baseUser,
      team_id: "team_3",
      email: "latest@example.com",
    });
    await screen.findByText("latest@example.com");

    resolveFirstSwitch({
      ...baseUser,
      team_id: "team_2",
      email: "stale@example.com",
    });
    await Promise.resolve();

    expect(screen.queryByText("stale@example.com")).toBeNull();
    expect(screen.getByText("latest@example.com")).toBeTruthy();
    expect(api.fetchMe).toHaveBeenCalledTimes(3);
  });

  it("authenticates even when User Timing telemetry throws", async () => {
    vi.stubGlobal("performance", {
      mark: vi.fn(),
      measure: vi.fn(() => {
        throw new Error("telemetry unavailable");
      }),
    });
    api.fetchMe.mockResolvedValue(baseUser);

    render(
      <AuthSessionProvider>
        <SessionState />
      </AuthSessionProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe(
        "authenticated",
      ),
    );
    expect(screen.getByTestId("email").textContent).toBe("u@example.com");
  });
});
