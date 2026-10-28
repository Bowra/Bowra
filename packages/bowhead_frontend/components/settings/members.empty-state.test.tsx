import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchMe, type MeUser } from "@/lib/auth-api";
import messages from "@/messages/zh-CN.json";

import { SettingsMembers } from "./members";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("@/lib/auth-api", () => ({
  fetchMe: vi.fn(),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push, replace: vi.fn() }),
}));
vi.mock("@/lib/use-space", () => ({
  useSpace: () => ({
    items: [],
    activeId: null,
    activeName: "（无空间）",
    loading: false,
    error: null,
    setActiveId: vi.fn(),
    refresh: vi.fn(),
  }),
}));

const member: MeUser = {
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
  capabilities: {},
  credit_balance: 0,
  credit_quota: 0,
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  is_team_admin: false,
  workspace_count: 0,
  needs_onboarding: false,
};

function renderMembers() {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <SettingsMembers />
    </NextIntlClientProvider>,
  );
}

describe("SettingsMembers empty space actions", () => {
  beforeEach(() => {
    push.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("does not send a member to team-only space settings", async () => {
    vi.mocked(fetchMe).mockResolvedValue(member);

    renderMembers();

    expect(
      await screen.findByText(
        "请联系团队负责人或管理员创建空间，或邀请你加入空间。",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "打开空间" })).toBeNull();
  });

  it("lets a team owner open space management through app routing", async () => {
    vi.mocked(fetchMe).mockResolvedValue({
      ...member,
      team_role: "owner",
      is_team_admin: true,
    });

    renderMembers();

    const openSpaces = await screen.findByRole("button", {
      name: "打开空间",
    });
    fireEvent.click(openSpaces);

    await waitFor(() =>
      expect(push).toHaveBeenCalledWith(
        "/team/settings/spaces/",
      ),
    );
  });
});
