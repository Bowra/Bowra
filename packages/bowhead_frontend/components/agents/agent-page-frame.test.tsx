import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentPageFrame, AgentPageHeader } from "./agent-page-frame";

const replace = vi.fn();
const push = vi.fn();
const useAuthSession = vi.fn();

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children, mobileTitle, mobileActions }: { children: React.ReactNode; mobileTitle?: string; mobileActions?: React.ReactNode }) => (
    <div data-mobile-title={mobileTitle}><div data-testid="mobile-actions">{mobileActions}</div>{children}</div>
  ),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ replace, push }),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useAuthSession: () => useAuthSession(),
  useOptionalAuthSession: () => useAuthSession(),
}));

describe("AgentPageFrame", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("renders marketplace content when the rollout capability is active", () => {
    useAuthSession.mockReturnValue({
      me: { capabilities: { subagent_management_v2: true } },
    });

    render(<AgentPageFrame><p>marketplace</p></AgentPageFrame>);

    expect(screen.getByText("marketplace")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("returns members to capability overview when the Agent catalog is unavailable", async () => {
    useAuthSession.mockReturnValue({
      me: { capabilities: { subagent_management_v2: false } },
    });

    render(<AgentPageFrame><p>marketplace</p></AgentPageFrame>);

    expect(screen.queryByText("marketplace")).toBeNull();
    await waitFor(() => {
      expect(replace).toHaveBeenCalledWith("/capabilities/");
    });
  });

  it("keeps an Owner deep link available when the marketplace rollout is off", async () => {
    window.history.replaceState(null, "", "/agents/manage/?agent_id=osa_1");
    useAuthSession.mockReturnValue({ me: { capabilities: { subagent_management_v2: false } } });

    render(<AgentPageFrame allowOwnerDeepLink><p>owner editor</p></AgentPageFrame>);

    expect(await screen.findByText("owner editor")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("titles the phone top bar with the page name, not the application name", async () => {
    useAuthSession.mockReturnValue({ me: { capabilities: { subagent_management_v2: true } } });
    const title = (container: HTMLElement) =>
      container.querySelector("[data-mobile-title]")?.getAttribute("data-mobile-title");

    // Every independent Agent page has its own title.
    for (const [page, key] of [["marketplace", "tab_market"], ["manage", "tab_mine"], ["charts", "charts_title"], ["create", "create_agent"]] as const) {
      const { container, unmount } = render(<AgentPageFrame page={page}><p>x</p></AgentPageFrame>);
      await waitFor(() => expect(title(container)).toBe(key));
      unmount();
    }
  });

  it.each(["member", "audit_admin"])("redirects %s deep links before mounting Dashboard data", async role => {
    useAuthSession.mockReturnValue({ status: "authenticated", me: { team_role: role, capabilities: { subagent_management_v2: true } } });
    render(<AgentPageFrame page="dashboard" adminOnly><p>admin data</p></AgentPageFrame>);
    expect(screen.queryByText("admin data")).toBeNull();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/agents/"));
  });

  it.each(["owner", "team_admin"])("opens the Dashboard for %s", role => {
    useAuthSession.mockReturnValue({ status: "authenticated", me: { team_role: role, capabilities: { subagent_management_v2: true } } });
    render(<AgentPageFrame page="dashboard" adminOnly><p>admin data</p></AgentPageFrame>);
    expect(screen.getByText("admin data")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("renders the management page title and Create Agent action", () => {
    useAuthSession.mockReturnValue({ status: "authenticated", me: { team_role: "member", capabilities: { subagent_management_v2: true } } });
    render(<AgentPageFrame page="manage"><AgentPageHeader page="manage" canCreate description="manage description" /></AgentPageFrame>);

    expect(screen.getByRole("heading", { level: 1, name: "tab_mine" })).toBeTruthy();
    expect(screen.getByText("manage description")).toBeTruthy();
    const create = screen.getAllByRole("link", { name: "create_agent" });
    expect(create.map((link) => link.getAttribute("href"))).toEqual(["/agents/new/", "/agents/new/"]);
    expect(create[1].className).toContain("settings-button--primary");
    // Members see no ⋯: both entries keep their existing owner/admin visibility.
    expect(screen.queryByRole("button", { name: "more_actions" })).toBeNull();
  });

  it("moves Agent overview and built-in configuration into the header ⋯ for owners", async () => {
    useAuthSession.mockReturnValue({ status: "authenticated", me: { team_role: "owner", capabilities: { subagent_management_v2: true, subagent_creator: true } } });
    render(<AgentPageFrame><AgentPageHeader page="marketplace" canCreate={false} description="d" /></AgentPageFrame>);

    expect(screen.queryByRole("link", { name: "create_agent" })).toBeNull();
    const triggers = screen.getAllByRole("button", { name: "more_actions" });
    fireEvent.click(triggers[triggers.length - 1]);
    const menu = await screen.findByRole("menu", { name: "more_actions" });
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["menu_overview", "manage_builtins"]);
    const builtins = within(menu).getByRole("menuitem", { name: "manage_builtins" });
    // Real links: modified clicks and "open in new tab" keep working.
    expect(builtins.tagName).toBe("A");
    expect(builtins.getAttribute("href")).toBe("/team/settings/subagents/");
    expect(within(menu).getByRole("menuitem", { name: "menu_overview" }).getAttribute("href")).toBe("/agents/dashboard/");
    fireEvent.click(builtins);
    expect(push).toHaveBeenCalledWith("/team/settings/subagents/");
  });
});
