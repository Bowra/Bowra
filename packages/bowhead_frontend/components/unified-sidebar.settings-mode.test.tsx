/**
 * P2 batch 4: the one sidebar shows settings content on settings and administration routes, and the
 * expanded sidebar is persistent from 1024 with an automatic icon rail while the workbench canvas is open
 * below 1280.
 */
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { MeUser } from "@/lib/auth-api";
import { fetchApprovals } from "@/lib/api";
import { useRequestSidebarAutoCompact } from "@/lib/sidebar-auto-compact";
import { requestSidebarOverlay } from "@/lib/sidebar-overlay-request";
import { resetSettingsNavCountsForTests } from "@/lib/settings-nav-counts";
import { UnifiedSidebar } from "./unified-sidebar";

const { nav, me } = vi.hoisted(() => ({
  nav: { pathname: "/", search: "" },
  me: { current: null as unknown },
}));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useSearchParams: () => new URLSearchParams(nav.search),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ status: "authenticated", me: me.current }),
}));
vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn() }));
vi.mock("@/lib/use-sessions", () => ({
  useSessionList: () => ({ groups: [], archivedCount: 0 }),
}));
vi.mock("@/lib/canvas-navigation-guard", () => ({
  hasCanvasUnsavedChanges: () => false,
  confirmCanvasNavigation: () => Promise.resolve(true),
}));
vi.mock("@/components/user-menu", () => ({
  UserMenu: () => <button aria-label="account-menu">A</button>,
  administrationIcon: () => null,
}));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: [{ id: "ws_1", name: "零售经营分析", desc: "", role: "member" }], activeId: "ws_1" }),
}));
vi.mock("@/lib/api", () => ({
  pinSession: vi.fn(),
  unpinSession: vi.fn(),
  renameSession: vi.fn(),
  fetchApprovals: vi.fn(() => Promise.resolve([])),
  fetchScheduledJobs: vi.fn(() => Promise.resolve({ items: [] })),
}));
vi.mock("@/lib/access-requests-api", () => ({ listAccessRequests: vi.fn(() => Promise.resolve({ items: [] })) }));
vi.mock("@/lib/invite-api", () => ({
  getAuthCapabilities: vi.fn(() => Promise.resolve({ deployment_mode: "saas", team_openapi: false })),
}));

const owner = {
  id: "u_1",
  email: "a@example.com",
  display_name: "A",
  team_role: "owner",
  is_team_admin: true,
  capabilities: {},
} as unknown as MeUser;

function memStorage(): Storage {
  let store: Record<string, string> = {};
  return {
    getItem: (key) => store[key] ?? null,
    setItem: (key, value) => {
      store[key] = String(value);
    },
    removeItem: (key) => {
      delete store[key];
    },
    clear: () => {
      store = {};
    },
    key: (index) => Object.keys(store)[index] ?? null,
    get length() {
      return Object.keys(store).length;
    },
  };
}

function setViewport(width: number) {
  Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
}

function presentation() {
  return screen.getByTestId("unified-sidebar").getAttribute("data-presentation");
}

function CanvasOpen() {
  useRequestSidebarAutoCompact(true);
  return null;
}

describe("UnifiedSidebar settings mode and docking (P2 batch 4)", () => {
  beforeEach(() => {
    me.current = owner;
    nav.pathname = "/";
    nav.search = "";
    Object.defineProperty(window, "localStorage", { value: memStorage(), configurable: true });
    Object.defineProperty(window, "sessionStorage", { value: memStorage(), configurable: true });
    delete document.documentElement.dataset.sidebarMode;
    setViewport(1440);
  });
  afterEach(() => {
    cleanup();
    resetSettingsNavCountsForTests();
    vi.mocked(fetchApprovals).mockClear();
  });

  it("shows the settings content on a team settings route", () => {
    nav.pathname = "/team/settings/people/";
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    expect(screen.getByRole("link", { name: "返回工作" })).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "设置导航" })).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: "主导航" })).toBeNull();
    expect(screen.getByRole("link", { current: "page" }).textContent).toBe("成员");
  });

  it("highlights the page's own settings tab when the page reports it", () => {
    nav.pathname = "/settings/workflows/";
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} settingsActiveTab="workflows" />);
    expect(screen.getByRole("link", { current: "page" }).textContent).toBe("技能与流程");
  });

  it.each([
    ["/capabilities/", "type=tools", "tools_catalog", "工具"],
    ["/team/settings/tools/", "capability_return=team", "tools_catalog", "工具"],
    ["/team/settings/capabilities/", "", "capabilities_catalog", "插件"],
    ["/skills/", "scope=mine", "skills", "技能与流程"],
    ["/team/settings/workflows/", "", "skills", "技能与流程"],
    ["/connections/", "", "connections", "我的连接"],
    ["/team/settings/scheduled/", "bind=osa_1", "scheduled", "定时任务"],
  ])("keeps primary navigation and selects the resource for %s", (path, search, resource, label) => {
    me.current = { ...owner, capabilities: { subagent_management_v2: true, personal_mcp: true } };
    nav.pathname = `/bowhead${path}`;
    nav.search = search;
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeTruthy();
    for (const id of ["new_work", "reports", "library", "agents", "capabilities"]) {
      expect(screen.getByTestId(`unified-nav-${id}`)).toBeTruthy();
    }
    const selected = screen.getByTestId(`unified-nav-${resource}`);
    expect(selected.getAttribute("aria-current")).toBe("page");
    expect(selected.textContent).toBe(label);
    expect(screen.getByTestId("unified-nav-capabilities").getAttribute("href")).toBe("/bowhead/capabilities/");
    delete window.__BOWHEAD_BASE_PATH__;
  });

  it("highlights an administration page and keeps the work content on work routes", () => {
    nav.pathname = "/admin/turns/";
    const view = renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    expect(screen.getByRole("link", { current: "page" }).textContent).toBe("运行追踪");
    view.unmount();
    nav.pathname = "/files/";
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "返回工作" })).toBeNull();
    expect(window.sessionStorage.getItem("bowhead.nav.last_work_route")).toBe("/files/");
  });

  it("offers one rail button per settings group in the icon rail, each opening its destinations", async () => {
    window.localStorage.setItem("bowhead.sidebar.mode.u_1", "compact");
    nav.pathname = "/settings/profile/";
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    expect(presentation()).toBe("compact");
    expect(screen.getByRole("link", { name: "返回工作" })).toBeTruthy();
    const personal = screen.getByRole("button", { name: "个人，当前页面所在分组" });
    fireEvent.click(personal);
    const menu = await screen.findByRole("menu", { name: /个人/ });
    expect(Array.from(menu.querySelectorAll("a")).map((a) => a.textContent)).toEqual([
      "账号",
      "安全",
      "外观",
      "记忆",
      "导出",
    ]);
    expect(screen.getByRole("button", { name: "团队 · 成员与空间" })).toBeTruthy();
  });

  it("is expanded by default from 1024", () => {
    setViewport(1100);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    expect(presentation()).toBe("expanded");
  });

  it("collapses to the icon rail while the canvas is open below 1280, and the expand control opens the overlay", async () => {
    setViewport(1240);
    renderIntl(
      <>
        <UnifiedSidebar onNewSession={() => "created"} />
        <CanvasOpen />
      </>,
    );
    await waitFor(() => expect(presentation()).toBe("compact"));
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "展开侧栏" }));
    });
    expect(presentation()).toBe("overlay");
  });

  it("does not collapse for the canvas from 1280 up", () => {
    setViewport(1300);
    renderIntl(
      <>
        <UnifiedSidebar onNewSession={() => "created"} />
        <CanvasOpen />
      </>,
    );
    expect(presentation()).toBe("expanded");
  });

  it("hides the sidebar at 768–1023 and opens it as an overlay from the top bar, returning focus there", async () => {
    setViewport(900);
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    expect(presentation()).toBe("drawer");
    act(() => {
      expect(requestSidebarOverlay(opener)).toBe(true);
    });
    expect(presentation()).toBe("overlay");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(presentation()).toBe("drawer"));
    await waitFor(() => expect(document.activeElement).toBe(opener));
    opener.remove();
  });

  it("does not mount the settings data where the rail is hidden (phone width)", async () => {
    setViewport(390);
    nav.pathname = "/settings/profile/";
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    await act(async () => {});
    expect(fetchApprovals).not.toHaveBeenCalled();
    expect(screen.queryByRole("navigation", { name: "设置导航" })).toBeNull();
  });
});
