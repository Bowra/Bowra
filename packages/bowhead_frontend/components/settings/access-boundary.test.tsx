import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { SettingsAccessBoundary } from "./access-boundary";
import { fetchMe, type MeUser } from "@/lib/auth-api";

vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn() }));

const baseMe: MeUser = {
  id: "u_member",
  email: "member@example.com",
  display_name: "Member",
  avatar_url: null,
  team_id: "team_1",
  tier: "pro",
  role: "user",
  team_role: "member",
  status: "active",
  email_verified: true,
  credit_balance: 0,
  credit_quota: 0,
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  is_team_admin: false,
  workspace_count: 1,
  needs_onboarding: false,
};

describe("SettingsAccessBoundary", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("does not mount protected people content for member role", async () => {
    vi.mocked(fetchMe).mockResolvedValue(baseMe);

    renderIntl(
      <SettingsAccessBoundary tab="people">
        <div>protected sentinel</div>
      </SettingsAccessBoundary>,
    );

    await screen.findByText("你没有访问此设置页的权限");
    expect(screen.queryByText("protected sentinel")).toBeNull();
    // Announce the hint explaining who can access via role=alert, and attach the UI-refresh hook
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("此页面需要");
    expect(alert).toHaveClass("ui-access-state-body");
  });

  it("keeps the system Agent configuration page team-admin only", async () => {
    vi.mocked(fetchMe).mockResolvedValue(baseMe);

    renderIntl(
      <SettingsAccessBoundary tab="subagents">
        <div>protected sentinel</div>
      </SettingsAccessBoundary>,
    );

    await screen.findByText("你没有访问此设置页的权限");
    expect(screen.queryByText("protected sentinel")).toBeNull();
  });

  it("shows retry and sign-in recovery when permissions cannot be verified", async () => {
    vi.mocked(fetchMe).mockRejectedValue(new Error("network down"));

    renderIntl(
      <SettingsAccessBoundary tab="people">
        <div>protected sentinel</div>
      </SettingsAccessBoundary>,
    );

    await screen.findByText("暂时无法确认你的权限");
    expect(screen.getByText("重试")).toBeTruthy();
    expect(screen.getByText("重新登录")).toBeTruthy();
    expect(screen.queryByText("protected sentinel")).toBeNull();
  });

  it("retries permission checks", async () => {
    vi.mocked(fetchMe)
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce({ ...baseMe, team_role: "team_admin", is_team_admin: true });

    renderIntl(
      <SettingsAccessBoundary tab="people">
        <div>protected sentinel</div>
      </SettingsAccessBoundary>,
    );

    await screen.findByText("暂时无法确认你的权限");
    fireEvent.click(screen.getByText("重试"));

    await waitFor(() => expect(fetchMe).toHaveBeenCalledTimes(2));
    await screen.findByText("protected sentinel");
  });

  it("does not mount OIDC settings for a team admin", async () => {
    vi.mocked(fetchMe).mockResolvedValue({
      ...baseMe,
      team_role: "team_admin",
      is_team_admin: true,
    });

    renderIntl(
      <SettingsAccessBoundary tab="oidc">
        <div>protected sentinel</div>
      </SettingsAccessBoundary>,
    );

    await screen.findByText("你没有访问此设置页的权限");
    expect(screen.queryByText("protected sentinel")).toBeNull();
  });

  it("allows a team admin to manage public entries", async () => {
    vi.mocked(fetchMe).mockResolvedValue({
      ...baseMe,
      team_role: "team_admin",
      is_team_admin: true,
    });

    renderIntl(
      <SettingsAccessBoundary tab="public-entries">
        <div>protected sentinel</div>
      </SettingsAccessBoundary>,
    );

    await screen.findByText("protected sentinel");
  });

  it("mounts email service settings for team admins", async () => {
    vi.mocked(fetchMe).mockResolvedValue({
      ...baseMe,
      team_role: "team_admin",
      is_team_admin: true,
    });

    renderIntl(
      <SettingsAccessBoundary tab="email-service">
        <div>protected sentinel</div>
      </SettingsAccessBoundary>,
    );

    await screen.findByText("protected sentinel");
  });

  it("does not mount email service settings for members", async () => {
    vi.mocked(fetchMe).mockResolvedValue(baseMe);

    renderIntl(
      <SettingsAccessBoundary tab="email-service">
        <div>protected sentinel</div>
      </SettingsAccessBoundary>,
    );

    await screen.findByText("你没有访问此设置页的权限");
    expect(screen.queryByText("protected sentinel")).toBeNull();
  });

  it.each([
    [{ access_requests: true }, true],
    [{ access_requests: false }, false],
  ])("with capabilities %j shows who can help: %s, never a request button", async (capabilities, shown) => {
    vi.mocked(fetchMe).mockResolvedValue({ ...baseMe, capabilities });

    renderIntl(
      <SettingsAccessBoundary tab="people">
        <div>protected sentinel</div>
      </SettingsAccessBoundary>,
    );

    await screen.findByText("你没有访问此设置页的权限");
    expect(Boolean(screen.queryByTestId("access-who-can-help-team"))).toBe(shown);
    expect(screen.queryByRole("button", { name: "申请访问" })).toBeNull();
  });
});
