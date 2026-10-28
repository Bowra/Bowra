/**
 * P2 batch 4: the settings sections live in the sidebar's settings content, so SettingsShell renders neither
 * its 210 column nor the mobile <select>, starts the sidebar expanded and tells the shell which tab it shows.
 */
import type { ReactNode } from "react";
import { act, cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { fetchMe, type MeUser } from "@/lib/auth-api";

const shell = vi.hoisted(() => ({ props: null as Record<string, unknown> | null }));
const navigation = vi.hoisted(() => ({ search: "" }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(navigation.search) }));

vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn() }));
vi.mock("@/lib/invite-api", () => ({ getAuthCapabilities: vi.fn(async () => null) }));
vi.mock("@/lib/api", () => ({ fetchScheduledJobs: vi.fn(async () => ({ items: [] })) }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/components/admin-shell", () => ({
  AdminShell: ({ children, ...props }: { children: ReactNode } & Record<string, unknown>) => {
    shell.props = props;
    return <div>{children}</div>;
  },
}));

import { SettingsShell } from "./settings-shell";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  shell.props = null;
  navigation.search = "";
  delete window.__BOWHEAD_BASE_PATH__;
});

describe("SettingsShell section navigation", () => {
  it("keeps built-in configuration in the Agents navigation context", async () => {
    vi.mocked(fetchMe).mockResolvedValue({
      id: "u1", team_role: "owner", is_team_admin: true,
      capabilities: { subagent_management_v2: true },
    } as MeUser);
    renderIntl(<SettingsShell active="subagents"><div>content</div></SettingsShell>);
    await screen.findByText("content");
    expect(shell.props).toMatchObject({ settingsActiveTab: "subagents", mobileTitle: "智能体" });
    // Built-in Agents are a team setting (polish spec D1): the page breadcrumb leads to team settings.
    expect(screen.getByRole("link", { name: "团队设置" }).getAttribute("href")).toBe("/team/settings/");
    expect(screen.queryByRole("link", { name: "← 返回智能体" })).toBeNull();
  });

  it("uses the page title for the top bar so it matches the page H1", async () => {
    vi.mocked(fetchMe).mockResolvedValue({ id: "u1", team_role: "owner", is_team_admin: true } as MeUser);
    renderIntl(<SettingsShell active="workflows" title="团队技能与工作流"><div>content</div></SettingsShell>);
    await screen.findByText("content");
    expect(shell.props).toMatchObject({ settingsActiveTab: "workflows", mobileTitle: "团队技能与工作流" });
  });

  it("leaves section navigation to the sidebar", async () => {
    vi.mocked(fetchMe).mockResolvedValue({ id: "u1", team_role: "owner", is_team_admin: true } as MeUser);
    renderIntl(<SettingsShell active="profile"><div>content</div></SettingsShell>);
    await act(async () => {});
    expect(screen.getByText("content")).toBeTruthy();
    expect(shell.props).toMatchObject({ settingsActiveTab: "profile" });
  });

  it.each(["tools", "capabilities", "workflows", "subagents"] as const)("returns %s management to the Capability Center overview", async (active) => {
    navigation.search = "capability_return=team";
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    vi.mocked(fetchMe).mockResolvedValue({ id: "u1", team_role: "owner", capabilities: { subagent_management_v2: true } } as MeUser);
    renderIntl(<SettingsShell active={active}><div>content</div></SettingsShell>);
    await screen.findByText("content");
    expect(screen.getByRole("link", { name: "返回概览" }).getAttribute("href")).toBe("/bowhead/capabilities/");
  });

  it("returns team Skill maintenance to the originating personal filter", async () => {
    navigation.search = "capability_return=my_skills";
    vi.mocked(fetchMe).mockResolvedValue({ id: "u1", team_role: "owner" } as MeUser);
    renderIntl(<SettingsShell active="workflows"><div>content</div></SettingsShell>);
    await screen.findByText("content");
    expect(screen.getByRole("link", { name: "返回我的技能" }).getAttribute("href")).toBe("/skills/?scope=mine");
  });

  it("uses the resource catalog when a return token is not recognized", async () => {
    navigation.search = "capability_return=https%3A%2F%2Funtrusted.example";
    vi.mocked(fetchMe).mockResolvedValue({ id: "u1", team_role: "owner" } as MeUser);
    renderIntl(<SettingsShell active="tools"><div>content</div></SettingsShell>);
    await screen.findByText("content");
    expect(screen.getByRole("link", { name: "返回工具" }).getAttribute("href")).toBe("/capabilities/?type=tools");
  });
});
