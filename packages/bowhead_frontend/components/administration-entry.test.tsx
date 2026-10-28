import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { fetchMe, type MeUser } from "@/lib/auth-api";
import { AdministrationEntry } from "./administration-entry";

const push = vi.fn();
const replace = vi.fn();
const appRouter = { push, replace };

vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/shell/Topbar", () => ({ Topbar: () => null }));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => appRouter,
}));
vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn() }));
// The two telemetry channels were consolidated into `@/lib/metrics` (2026-08-08); interactions use trackInteraction.
vi.mock("@/lib/metrics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/metrics")>()),
  trackInteraction: vi.fn(),
}));

const USER: MeUser = {
  id: "usr_1",
  email: "user@example.com",
  display_name: "User",
  avatar_url: null,
  team_id: "team_1",
  tier: "pro",
  role: "user",
  team_role: "member",
  status: "active",
  email_verified: true,
  capabilities: {},
  credit_balance: 0,
  credit_quota: 0,
  locale: "zh-CN",
  timezone: "Asia/Singapore",
  is_team_admin: false,
  workspace_count: 1,
  needs_onboarding: false,
};

describe("AdministrationEntry", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("redirects an owner to the first permitted real destination", async () => {
    vi.mocked(fetchMe).mockResolvedValue({
      ...USER,
      team_role: "owner",
      is_team_admin: true,
    });
    renderIntl(<AdministrationEntry />);

    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("/admin/turns/"),
    );
    expect(screen.queryByTestId("administration-hub")).toBeNull();
  });


  it("redirects an audit admin to Audit", async () => {
    vi.mocked(fetchMe).mockResolvedValue({
      ...USER,
      team_role: "audit_admin",
    });
    renderIntl(<AdministrationEntry />);

    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("/admin/audit/"),
    );
  });

  it("shows an explicit denial to members", async () => {
    vi.mocked(fetchMe).mockResolvedValue(USER);
    renderIntl(<AdministrationEntry />);

    expect((await screen.findByRole("alert")).textContent).toBe(
      "你没有可用的管理入口。",
    );
    expect(replace).not.toHaveBeenCalled();
  });

  it("recovers from a transient identity failure", async () => {
    vi.mocked(fetchMe)
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({
        ...USER,
        team_role: "owner",
        is_team_admin: true,
      });
    renderIntl(<AdministrationEntry />);

    await screen.findByRole("alert");
    screen.getByRole("button", { name: "重试" }).click();

    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("/admin/turns/"),
    );
    expect(fetchMe).toHaveBeenCalledTimes(2);
  });
});
