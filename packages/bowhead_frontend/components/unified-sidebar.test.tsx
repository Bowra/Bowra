import { PRODUCT_NAME } from "@/lib/product-identity";
import {
  act,
  cleanup,
  createEvent,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { fetchMe } from "@/lib/auth-api";
import { useRequestSidebarAutoCompact } from "@/lib/sidebar-auto-compact";
import { UnifiedSidebar } from "./unified-sidebar";

const push = vi.fn();
const { navigationState, useSessionsMock, sessionListPaging } = vi.hoisted(() => ({
  navigationState: { pathname: "/", search: "" },
  /** Paging fields the mocked useSessionList adds to its state (empty = single page). */
  sessionListPaging: {} as Record<string, unknown>,
  useSessionsMock: vi.fn(
    (
      _refreshKey = 0,
      _enabled = true,
      _workspaceId?: string,
    ) => [],
  ),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => navigationState.pathname,
  useSearchParams: () => new URLSearchParams(navigationState.search),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push, replace: vi.fn() }),
}));
vi.mock("@/lib/auth-api", () => ({
  fetchMe: vi.fn(),
}));
vi.mock("@/lib/use-sessions", () => ({
  useSessions: useSessionsMock,
  useSessionList: (
    refreshKey?: number,
    enabled?: boolean,
  ) => ({
    groups: useSessionsMock(refreshKey, enabled),
    archivedCount: 0,
    ...sessionListPaging,
  }),
}));
vi.mock("@/lib/canvas-navigation-guard", () => ({
  hasCanvasUnsavedChanges: () => false,
  confirmCanvasNavigation: () => Promise.resolve(true),
}));
vi.mock("@/components/user-menu", () => ({
  UserMenu: ({
    onBeforePersonalSettingsNavigate,
  }: {
    onBeforePersonalSettingsNavigate?: () => unknown;
  }) => (
    <>
      <button aria-label="account-menu">A</button>
      <button
        aria-label="open-personal-settings"
        onClick={() => onBeforePersonalSettingsNavigate?.()}
      >
        settings
      </button>
    </>
  ),
}));
vi.mock("@/lib/api", () => ({
  pinSession: vi.fn(),
  unpinSession: vi.fn(),
  renameSession: vi.fn(),
}));

const sessions = {
  groups: [
    {
      id: "today",
      label: "TODAY",
      items: [
        {
          id: "s_1",
          title: "Q3 营收复盘",
          intents: [],
          time: "2m",
          state: "done" as const,
          pinned: true,
          unread: true,
        },
      ],
    },
  ],
  currentSessionId: "s_1",
  onPick: vi.fn(),
  onTogglePin: vi.fn(),
};

function setViewport(width: number) {
  Object.defineProperty(window, "innerWidth", {
    value: width,
    configurable: true,
  });
}

/** Width in the narrow-desktop band (1024–1279) where an open results canvas forces the icon rail. */
const NARROW_DESKTOP_WIDTH = 1100;

function AutoCompactRequest() {
  useRequestSidebarAutoCompact(true);
  return null;
}

/** Narrow desktop with the results canvas open: icon rail, and expanding opens the overlay (P2 batch 4). */
function narrowDesktop() {
  setViewport(NARROW_DESKTOP_WIDTH);
  renderIntl(<AutoCompactRequest />);
}

/** Docked icon rail: the member's remembered desktop mode is compact. */
function compactRail() {
  setViewport(1280);
  document.documentElement.dataset.sidebarMode = "compact";
  window.localStorage.setItem("bowhead.sidebar.mode.usr_1", "compact");
}

function installMemStorage() {
  let store: Record<string, string> = {};
  const storage: Storage = {
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
  Object.defineProperty(window, "localStorage", {
    value: storage,
    configurable: true,
  });
}

describe("UnifiedSidebar", () => {
  beforeEach(() => {
    installMemStorage();
    delete document.documentElement.dataset.sidebarMode;
    delete document.documentElement.dataset.bowheadInputModality;
    push.mockReset();
    navigationState.pathname = "/";
    navigationState.search = "";
    useSessionsMock.mockClear();
    for (const key of Object.keys(sessionListPaging)) delete sessionListPaging[key];
    vi.mocked(fetchMe).mockResolvedValue({
      id: "usr_1",
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
      timezone: "Asia/Singapore",
      is_team_admin: false,
      workspace_count: 1,
      needs_onboarding: false,
    });
  });

  afterEach(cleanup);

  afterEach(() => {
    delete window.__BOWHEAD_BASE_PATH__;
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("ends the page-supplied list with the older-sessions row and loads the next batch", async () => {
    setViewport(1600);
    const onLoadMore = vi.fn();
    const olderGroups = [
      ...sessions.groups,
      {
        id: "older",
        label: "OLDER",
        items: [{ id: "s_old", title: "门店客流归因", intents: [], time: "40d", state: "done" as const }],
      },
    ];
    renderIntl(
      <UnifiedSidebar
        onNewSession={() => "created"}
        sessions={{
          ...sessions,
          groups: olderGroups,
          olderSessions: {
            hasMore: true,
            loadingMore: false,
            loadMoreError: false,
            olderPagesLoaded: 0,
            onLoadMore,
          },
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "更早的会话" }));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
    // The pinned session sits in the pinned section only.
    expect(screen.getAllByText("Q3 营收复盘")).toHaveLength(1);
    await waitFor(() => expect(fetchMe).toHaveBeenCalled());
  });

  it("pages its own session list when no page supplies one", async () => {
    setViewport(1600);
    const loadMore = vi.fn();
    useSessionsMock.mockReturnValue(sessions.groups as never);
    Object.assign(sessionListPaging, {
      hasMore: false,
      loadingMore: false,
      loadMoreError: false,
      olderPagesLoaded: 1,
      loadMore,
    });
    const view = renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    expect(screen.getByText("没有更早的会话了")).toBeTruthy();
    view.unmount();

    Object.assign(sessionListPaging, { hasMore: true });
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    fireEvent.click(screen.getByRole("button", { name: "更早的会话" }));
    expect(loadMore).toHaveBeenCalledTimes(1);
    useSessionsMock.mockReset().mockImplementation(() => []);
    await waitFor(() => expect(fetchMe).toHaveBeenCalled());
  });

  it("renders the accepted IA and work recovery in expanded mode", async () => {
    setViewport(1600);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "expanded",
    );
    expect(screen.getByText("新工作")).toBeTruthy();
    expect(screen.queryByText("新建分析")).toBeNull();
    expect(screen.queryByText("对话")).toBeNull();
    expect(screen.getByText("我的报告")).toBeTruthy();
    expect(screen.getByText("资料库")).toBeTruthy();
    expect(screen.getByText("能力中心")).toBeTruthy();
    expect(screen.getByText("Q3 营收复盘")).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: PRODUCT_NAME })
        .querySelector("svg")
        ?.getAttribute("viewBox"),
    ).toBe("200 380 850 480");

    fireEvent.click(screen.getByRole("button", { name: "展开资料库" }));
    expect(screen.getByText("文件")).toBeTruthy();
    expect(screen.getByText("知识库")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起资料库" }));
    expect(screen.queryByText("文件")).toBeNull();
    await waitFor(() => expect(fetchMe).toHaveBeenCalled());
  });

  it("models New work as the active home action", async () => {
    const onNewSession = vi.fn(() => Promise.resolve("created" as const));
    setViewport(1600);
    renderIntl(
      <UnifiedSidebar
        onNewSession={onNewSession}
        sessions={{ ...sessions, currentSessionId: null }}
      />,
    );

    const action = screen.getByTestId("unified-nav-new_work");
    expect(action.tagName).toBe("BUTTON");
    expect(action.getAttribute("href")).toBeNull();
    expect(action.getAttribute("aria-current")).toBe("page");
    expect(action.classList.contains("is-active")).toBe(true);
    expect(screen.getAllByRole("button", { name: "新工作" })).toHaveLength(1);

    fireEvent.click(action);
    await waitFor(() => expect(onNewSession).toHaveBeenCalledOnce());
  });

  it("shows pending feedback and prevents duplicate New work requests", async () => {
    let finish: ((result: "created") => void) | undefined;
    const onNewSession = vi.fn(
      () =>
        new Promise<"created">((resolve) => {
          finish = resolve;
        }),
    );
    setViewport(1600);
    renderIntl(<UnifiedSidebar onNewSession={onNewSession} sessions={sessions} />);

    const action = screen.getByTestId("unified-nav-new_work");
    fireEvent.click(action);

    await waitFor(() =>
      expect(action.getAttribute("aria-busy")).toBe("true"),
    );
    expect((action as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(action);
    expect(onNewSession).toHaveBeenCalledOnce();

    finish?.("created");
    await waitFor(() =>
      expect((action as HTMLButtonElement).disabled).toBe(false),
    );
  });

  it("keeps New work pending while a document navigation starts", async () => {
    setViewport(1600);
    renderIntl(
      <UnifiedSidebar onNewSession={() => "navigating"} sessions={sessions} />,
    );

    const action = screen.getByTestId("unified-nav-new_work");
    fireEvent.click(action);

    await waitFor(() =>
      expect(action.getAttribute("aria-busy")).toBe("true"),
    );
    expect((action as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows New work as pending while the shell navigates after a leave confirmation", () => {
    setViewport(1600);
    renderIntl(
      <UnifiedSidebar onNewSession={() => "cancelled"} sessions={sessions} navigationPending />,
    );

    const action = screen.getByTestId("unified-nav-new_work");
    expect(action.getAttribute("aria-busy")).toBe("true");
    expect((action as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps only the current session active on the home workspace", () => {
    setViewport(1600);
    renderIntl(
      <UnifiedSidebar
        onNewSession={() => "created"}
        sessions={sessions}
      />,
    );

    const action = screen.getByTestId("unified-nav-new_work");
    expect(action.getAttribute("aria-current")).toBeNull();
    expect(action.classList.contains("is-active")).toBe(false);
    expect(
      screen.getByText("Q3 营收复盘").closest("button")?.getAttribute(
        "aria-current",
      ),
    ).toBe("page");
  });

  it("keeps Recent sessions current while a historical session is open", () => {
    compactRail();
    renderIntl(
      <UnifiedSidebar
        onNewSession={() => "created"}
        sessions={sessions}
      />,
    );

    const recent = screen.getByRole("button", { name: "最近会话" });
    expect(recent.getAttribute("aria-current")).toBe("page");
    expect(recent.getAttribute("aria-expanded")).toBe("false");
    expect(recent.classList.contains("is-active")).toBe(true);

    fireEvent.click(recent);
    expect(recent.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(recent);
    expect(recent.getAttribute("aria-expanded")).toBe("false");
    expect(recent.getAttribute("aria-current")).toBe("page");
    expect(recent.classList.contains("is-active")).toBe(true);
  });

  it("keeps only Recent sessions current while its compact panel is open", () => {
    compactRail();
    renderIntl(
      <UnifiedSidebar
        onNewSession={() => "created"}
        sessions={{ ...sessions, currentSessionId: null }}
      />,
    );

    const newWork = screen.getByTestId("unified-nav-new_work");
    const recent = screen.getByRole("button", { name: "最近会话" });
    expect(newWork.getAttribute("aria-current")).toBe("page");

    fireEvent.click(recent);

    expect(recent.getAttribute("aria-expanded")).toBe("true");
    expect(recent.getAttribute("aria-current")).toBe("page");
    expect(recent.classList.contains("is-active")).toBe(true);
    expect(newWork.getAttribute("aria-current")).toBeNull();
    expect(newWork.classList.contains("is-active")).toBe(false);

    fireEvent.click(recent);

    expect(recent.getAttribute("aria-current")).toBeNull();
    expect(recent.classList.contains("is-active")).toBe(false);
    expect(newWork.getAttribute("aria-current")).toBe("page");
    expect(newWork.classList.contains("is-active")).toBe(true);
  });

  it("does not mark New work active away from the home workspace", () => {
    navigationState.pathname = "/reports/";
    setViewport(1600);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    const action = screen.getByTestId("unified-nav-new_work");
    expect(action.getAttribute("aria-current")).toBeNull();
    expect(action.classList.contains("is-active")).toBe(false);
    expect(
      screen.getByTestId("unified-nav-reports").getAttribute("aria-current"),
    ).toBe("page");
  });

  it("keeps the overlay open when New work is cancelled", async () => {
    narrowDesktop();
    renderIntl(
      <UnifiedSidebar
        onNewSession={() => Promise.resolve("cancelled")}
        sessions={sessions}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "展开侧栏" }));
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "overlay",
    );

    fireEvent.click(screen.getByTestId("unified-nav-new_work"));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "overlay",
    );
  });

  it("uses the 16 / 20 icon tiers in expanded and compact navigation", () => {
    setViewport(1600);
    const { unmount } = renderIntl(
      <UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />,
    );

    expect(
      screen
        .getByRole("button", { name: "收起侧栏" })
        .querySelector("svg")
        ?.getAttribute("width"),
    ).toBe("16");
    expect(
      screen
        .getByRole("button", { name: "新工作" })
        .querySelector("svg")
        ?.getAttribute("width"),
    ).toBe("20");

    unmount();
    compactRail();
    renderIntl(
      <UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />,
    );

    for (const name of ["搜索全部工作", "新工作", "最近会话"]) {
      expect(
        screen
          .getByRole("button", { name })
          .querySelector("svg")
          ?.getAttribute("width"),
      ).toBe("20");
    }
    for (const name of ["展开侧栏"]) {
      expect(
        screen
          .getByRole("button", { name })
          .querySelector("svg")
          ?.getAttribute("width"),
      ).toBe("16");
    }
  });

  it("uses compact as the dense-page default, then persists an explicit expansion", async () => {
    setViewport(1600);
    const { unmount } = renderIntl(
      <UnifiedSidebar
        onNewSession={() => "created"}
        sessions={sessions}
        preferCompactDesktop
      />,
    );

    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "compact",
    );
    expect(
      screen
        .getByTestId("unified-sidebar")
        .querySelector(".unified-sidebar-version"),
    ).toBeNull();
    await waitFor(() => expect(fetchMe).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "展开侧栏" }));
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "expanded",
    );
    await waitFor(() =>
      expect(
        window.localStorage.getItem("bowhead.sidebar.mode.usr_1"),
      ).toBe("expanded"),
    );

    unmount();
    renderIntl(
      <UnifiedSidebar
        onNewSession={() => "created"}
        sessions={sessions}
        preferCompactDesktop
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("unified-sidebar").dataset.presentation,
      ).toBe("expanded"),
    );
  });

  it("moves focus between the desktop collapse and expand controls", async () => {
    setViewport(1600);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    await waitFor(() => expect(fetchMe).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "收起侧栏" }));
    const expand = await screen.findByRole("button", { name: "展开侧栏" });
    await waitFor(() => expect(expand).toBe(document.activeElement));

    fireEvent.click(expand);
    const collapse = await screen.findByRole("button", { name: "收起侧栏" });
    await waitFor(() => expect(collapse).toBe(document.activeElement));
  });

  it("tracks pointer and keyboard modality for focus-ring styling", () => {
    setViewport(1600);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    const collapse = screen.getByRole("button", { name: "收起侧栏" });
    fireEvent.pointerDown(collapse);
    expect(
      document.documentElement.dataset.bowheadInputModality,
    ).toBe("pointer");

    fireEvent.keyDown(collapse, { key: "Tab" });
    expect(
      document.documentElement.dataset.bowheadInputModality,
    ).toBe("keyboard");
  });

  it("persists a desktop toggle made before identity finishes loading", async () => {
    let resolveMe:
      | ((user: Awaited<ReturnType<typeof fetchMe>>) => void)
      | undefined;
    vi.mocked(fetchMe).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveMe = resolve;
      }),
    );
    setViewport(1600);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "收起侧栏" }));
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "compact",
    );
    expect(
      window.localStorage.getItem("bowhead.sidebar.mode.usr_1"),
    ).toBeNull();

    await act(async () => {
      resolveMe?.({
        id: "usr_1",
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
        timezone: "Asia/Singapore",
        is_team_admin: false,
        workspace_count: 1,
        needs_onboarding: false,
      });
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(
        window.localStorage.getItem("bowhead.sidebar.mode.usr_1"),
      ).toBe("compact"),
    );
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "compact",
    );
  });

  it("restores an explicit desktop preference on pages with dense navigation", async () => {
    setViewport(1600);
    window.localStorage.setItem(
      "bowhead.sidebar.mode.current",
      "expanded",
    );
    window.localStorage.setItem(
      "bowhead.sidebar.mode.usr_1",
      "expanded",
    );

    renderIntl(
      <UnifiedSidebar
        onNewSession={() => "created"}
        sessions={sessions}
        preferCompactDesktop
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByTestId("unified-sidebar").dataset.presentation,
      ).toBe("expanded"),
    );
    expect(
      window.localStorage.getItem("bowhead.sidebar.mode.current"),
    ).toBe("expanded");
    expect(
      window.localStorage.getItem("bowhead.sidebar.mode.usr_1"),
    ).toBe("expanded");
  });

  it("opens the capability overview directly from the expanded sidebar", async () => {
    setViewport(1600);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    const capabilities = screen.getByTestId("unified-nav-capabilities");
    expect(capabilities.tagName).toBe("A");
    expect(capabilities.getAttribute("href")).toBe("/capabilities/");

    fireEvent.click(capabilities);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/capabilities/"));
  });

  it.each([false, true])("keeps Agent management in the primary navigation with compact mode %s", async (compact) => {
    const identity = await vi.mocked(fetchMe)();
    vi.mocked(fetchMe).mockResolvedValue({ ...identity, capabilities: { subagent_management_v2: true } });
    navigationState.pathname = "/bowhead/agents/manage/shared/";
    navigationState.search = "";
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    Object.defineProperty(window, "innerWidth", { value: 1440, configurable: true });
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} preferCompactDesktop={compact} />);
    const entry = await screen.findByTestId("unified-nav-agents");
    expect(screen.getByTestId("unified-nav-reports")).toBeTruthy();
    expect(screen.getByTestId("unified-nav-library")).toBeTruthy();
    expect(screen.getByTestId("unified-nav-capabilities")).toBeTruthy();
    if (compact) {
      fireEvent.click(entry);
      expect(await screen.findByRole("menuitem", { name: "我的智能体" })).toHaveProperty("href", expect.stringContaining("/bowhead/agents/manage/hired/"));
      fireEvent.click(screen.getByRole("menuitem", { name: "我的智能体" }));
    } else {
      expect(entry.getAttribute("href")).toBe("/bowhead/agents/manage/hired/");
      expect(screen.getByTestId("unified-nav-my_agents").getAttribute("aria-current")).toBe("page");
      fireEvent.click(entry);
    }
    await waitFor(() => expect(push).toHaveBeenCalledWith("/agents/manage/hired/"));
  });

  it("keeps an existing Owner deep link selected when the market is unavailable", async () => {
    navigationState.pathname = "/agents/manage/";
    navigationState.search = "agent_id=osa_owned";
    setViewport(1440);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);
    const entry = await screen.findByTestId("unified-nav-agents");
    expect(entry.getAttribute("href")).toBe("/agents/manage/?agent_id=osa_owned");
    expect(entry.getAttribute("aria-current")).toBe("page");
  });

  it("资料库只展开子项、不进概览页（知识库导航 discovery #3）", () => {
    setViewport(1600);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    const library = screen.getByTestId("unified-nav-library");
    expect(library.tagName).toBe("BUTTON");
    expect(library.getAttribute("href")).toBeNull();
    const before = library.getAttribute("aria-expanded");
    fireEvent.click(library);
    expect(push).not.toHaveBeenCalled();
    expect(library.getAttribute("aria-expanded")).not.toBe(before);
    expect(screen.getAllByRole("link", { name: "知识库" }).length).toBeGreaterThan(0);
  });

  it("puts the group overview before children in compact flyouts", () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
    // Library does not enter an overview page; the overlay lists only subitems.
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["文件", "知识库"]);
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
  });

  it("opens the capability overview from its compact resource menu", async () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);
    fireEvent.click(screen.getByRole("button", { name: "能力中心" }));
    const entry = screen.getByRole("menuitem", { name: "能力中心" });
    expect(entry.getAttribute("href")).toBe("/capabilities/");
    fireEvent.click(entry);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/capabilities/"));
  });

  it("announces a compact group that contains the current page", () => {
    navigationState.pathname = "/files/";
    compactRail();
    renderIntl(
      <UnifiedSidebar
        onNewSession={() => "created"}
        sessions={sessions}
      />,
    );

    const library = screen.getByRole("button", {
      name: "资料库，当前页面所在分组",
    });
    expect(library.classList.contains("is-active")).toBe(true);
    expect(library.getAttribute("aria-current")).toBeNull();
    expect(library.getAttribute("aria-expanded")).toBe("false");
  });

  it("uses compact + overlay without changing the app content width", () => {
    narrowDesktop();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "compact",
    );
    fireEvent.click(screen.getByRole("button", { name: "展开侧栏" }));
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "overlay",
    );
    const scrim = document.querySelector<HTMLButtonElement>(
      ".unified-sidebar-scrim",
    );
    expect(scrim?.tabIndex).toBe(-1);
    expect(scrim?.getAttribute("aria-hidden")).toBe("true");
  });

  it("gives the visible overlay panel the dialog semantics", async () => {
    narrowDesktop();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(
      screen.getByRole("button", { name: "展开侧栏" }),
      { detail: 1 },
    );

    const sidebar = screen.getByTestId("unified-sidebar");
    const dialog = screen.getByRole("dialog", { name: PRODUCT_NAME });
    expect(sidebar.getAttribute("role")).toBeNull();
    expect(dialog.classList.contains("unified-sidebar-expanded")).toBe(true);
    await waitFor(() => expect(dialog).toBe(document.activeElement));
  });

  it("focuses the collapse control after a keyboard open and closes on Escape", async () => {
    narrowDesktop();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "展开侧栏" }), {
      detail: 0,
    });
    const collapse = screen.getByRole("button", { name: "收起侧栏" });
    await waitFor(() => expect(collapse).toBe(document.activeElement));

    fireEvent.keyDown(collapse, { key: "Escape" });
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "compact",
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "展开侧栏" })).toBe(
        document.activeElement,
      ),
    );
  });

  it("still toggles the overlay with the platform shortcut from inside it", async () => {
    narrowDesktop();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "展开侧栏" }), {
      detail: 0,
    });
    const collapse = screen.getByRole("button", { name: "收起侧栏" });
    await waitFor(() => expect(collapse).toBe(document.activeElement));

    // The overlay is a modal dialog; panel keypresses do not bubble to window, so the panel forwards shortcuts.
    fireEvent.keyDown(collapse, { key: "\\", ctrlKey: true });
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "compact",
    );
  });

  it("closes the narrow desktop overlay before personal settings opens", () => {
    narrowDesktop();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "展开侧栏" }));
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "overlay",
    );

    fireEvent.click(
      screen.getByRole("button", { name: "open-personal-settings" }),
    );

    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "compact",
    );
  });

  it("does not reopen a stale overlay after crossing responsive bands", async () => {
    narrowDesktop();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "展开侧栏" }));
    expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
      "overlay",
    );

    setViewport(1600);
    fireEvent(window, new Event("resize"));
    await waitFor(() =>
      expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
        "expanded",
      ),
    );

    setViewport(NARROW_DESKTOP_WIDTH);
    fireEvent(window, new Event("resize"));
    await waitFor(() =>
      expect(screen.getByTestId("unified-sidebar").dataset.presentation).toBe(
        "compact",
      ),
    );
  });

  it("supports arrow-key navigation in compact destination menus", () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
    const menu = screen.getByRole("menu", { name: "资料库" });
    const items = screen.getAllByRole("menuitem");
    items[0]?.focus();
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(items[1]).toBe(document.activeElement);
    fireEvent.keyDown(menu, { key: "End" });
    expect(items.at(-1)).toBe(document.activeElement);
  });

  it("does not paint a menu-item focus state after a pointer-open", async () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(
      screen.getByRole("button", { name: "资料库" }),
      { detail: 1 },
    );
    const menu = screen.getByRole("menu", { name: "资料库" });
    const items = screen.getAllByRole("menuitem");
    await waitFor(() => expect(menu).toBe(document.activeElement));
    expect(items[0]).not.toBe(document.activeElement);

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(items[0]).toBe(document.activeElement);
  });

  it("does not focus a recent-work row after a pointer-open", async () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(
      screen.getByRole("button", { name: "最近会话" }),
      { detail: 1 },
    );
    const dialog = screen.getByRole("dialog", { name: "最近会话" });
    await waitFor(() => expect(dialog).toBe(document.activeElement));
    expect(screen.getByText("Q3 营收复盘").closest("button")).not.toBe(
      document.activeElement,
    );
  });

  it("renders canonical links under the runtime base path", () => {
    setViewport(1600);
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    navigationState.pathname = "/bowhead/reports/";
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    expect(
      screen.getByTestId("unified-nav-reports").getAttribute("href"),
    ).toBe("/bowhead/reports/");
    expect(
      screen.getByTestId("unified-nav-capabilities").getAttribute("href"),
    ).toBe("/bowhead/capabilities/");
    expect(
      screen.getByTestId("unified-nav-reports").getAttribute("aria-current"),
    ).toBe("page");
  });

  it("preserves modified-click browser navigation", () => {
    setViewport(1600);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    const reports = screen.getByTestId("unified-nav-reports");
    const event = createEvent.click(reports, { ctrlKey: true, button: 0 });
    fireEvent(reports, event);

    expect(event.defaultPrevented).toBe(false);
    expect(push).not.toHaveBeenCalled();
  });

  it("does not fetch desktop sessions while the rail is hidden on mobile", () => {
    setViewport(390);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} />);

    expect(useSessionsMock).toHaveBeenCalledWith(0, false);
  });

  it("closes a compact flyout before opening another sidebar surface", () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "最近会话" }));
    expect(
      screen.getByRole("dialog", { name: "最近会话" }),
    ).toBeTruthy();
    expect(screen.queryByRole("searchbox")).toBeNull();

    fireEvent.pointerDown(screen.getByRole("button", { name: "account-menu" }));
    expect(
      screen.queryByRole("dialog", { name: "最近会话" }),
    ).toBeNull();
  });

  it("clears a compact search before showing recent sessions", () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "搜索全部工作" }));
    fireEvent.change(
      screen.getByRole("searchbox", {
        name: "搜索会话、报告、文件和知识…",
      }),
      { target: { value: "收入" } },
    );

    fireEvent.click(screen.getByRole("button", { name: "最近会话" }));
    expect(
      screen.getByRole("dialog", { name: "最近会话" }),
    ).toBeTruthy();
    expect(screen.getByText("Q3 营收复盘")).toBeTruthy();
    expect(screen.queryByRole("searchbox")).toBeNull();
  });

  it("opens and focuses compact work search from the platform shortcut", async () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.keyDown(window, {
      key: "\u000b",
      code: "KeyK",
      ctrlKey: true,
    });

    const search = await screen.findByRole("searchbox", {
      name: "搜索会话、报告、文件和知识…",
    });
    await waitFor(() => expect(search).toBe(document.activeElement));
  });

  it("opens a report returned by V3 search through guarded app navigation", async () => {
    setViewport(1600);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/api/v1/search")) {
          return new Response(
            JSON.stringify({
              query: "收入",
              partial: false,
              groups: [
                { kind: "session", unavailable: false, items: [] },
                {
                  kind: "report",
                  unavailable: false,
                  items: [
                    {
                      kind: "report",
                      id: "cv_1",
                      title: "收入复盘报告",
                      session_id: "s_report",
                      snippet: null,
                      updated_at: "2026-07-22T00:00:00Z",
                    },
                  ],
                },
                { kind: "file", unavailable: false, items: [] },
                { kind: "knowledge", unavailable: false, items: [] },
              ],
            }),
            { status: 200 },
          );
        }
        return new Response('{"ok":true}', { status: 200 });
      }),
    );
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.change(
      screen.getByRole("searchbox", {
        name: "搜索会话、报告、文件和知识…",
      }),
      { target: { value: "收入" } },
    );
    fireEvent.click(
      // The second line (its date) is part of the link name.
      await screen.findByRole("link", { name: /^收入复盘报告/ }),
    );

    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/?session=s_report&report=1"),
    );
  });

  it("says a timed-out source was not searched instead of hiding it", async () => {
    setViewport(1600);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/api/v1/search")) {
          return new Response(
            JSON.stringify({
              query: "收入",
              partial: true,
              groups: [
                { kind: "session", unavailable: true, timed_out: true, items: [] },
                { kind: "report", unavailable: false, items: [] },
                { kind: "file", unavailable: false, items: [] },
                { kind: "knowledge", unavailable: false, items: [] },
              ],
            }),
            { status: 200 },
          );
        }
        return new Response('{"ok":true}', { status: 200 });
      }),
    );
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.change(
      screen.getByRole("searchbox", {
        name: "搜索会话、报告、文件和知识…",
      }),
      { target: { value: "收入" } },
    );

    expect(
      await screen.findByText("这一类搜索超时，请重试或换个更具体的关键词。"),
    ).toBeTruthy();
  });

  it("returns focus to a compact group button when its menu closes on Escape", async () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);
    const library = screen.getByRole("button", { name: "资料库" });

    fireEvent.click(library, { detail: 0 });
    const [overview] = screen.getAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(overview));
    fireEvent.keyDown(overview!, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(library));
  });

  it("closes a compact group menu on a press outside", async () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "资料库" }));
    expect(screen.getByRole("menu", { name: "资料库" })).toBeTruthy();
    fireEvent.pointerDown(document.body);

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("closes the compact recent panel on Escape and returns focus to its button", async () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);
    const recent = screen.getByRole("button", { name: "最近会话" });

    fireEvent.click(recent, { detail: 1 });
    const dialog = screen.getByRole("dialog", { name: "最近会话" });
    await waitFor(() => expect(dialog).toBe(document.activeElement));
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "最近会话" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(recent));
  });

  it("closes the compact recent panel when focus moves out of it", async () => {
    compactRail();
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);

    fireEvent.click(screen.getByRole("button", { name: "最近会话" }), { detail: 1 });
    const dialog = screen.getByRole("dialog", { name: "最近会话" });
    await waitFor(() => expect(dialog).toBe(document.activeElement));
    const outside = screen.getByRole("button", { name: "account-menu" });
    fireEvent.focusOut(dialog, { relatedTarget: outside });
    outside.focus();

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "最近会话" })).toBeNull(),
    );
  });

  it("keeps the compact recent panel open while a row's action menu is used", async () => {
    compactRail();
    const onTogglePin = vi.fn();
    renderIntl(
      <UnifiedSidebar
        onNewSession={() => "created"}
        sessions={{ ...sessions, onTogglePin }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "最近会话" }));
    fireEvent.click(screen.getByRole("button", { name: /^更多会话操作/ }));
    const unpin = await screen.findByRole("menuitem", { name: "取消置顶" });
    // The inline menu mounts on body; pressing its item must not count as an outside click on the Recent Work overlay.
    fireEvent.pointerDown(unpin);
    expect(screen.getByRole("dialog", { name: "最近会话" })).toBeTruthy();
    fireEvent.click(unpin);

    await waitFor(() => expect(onTogglePin).toHaveBeenCalledWith("s_1", true));
    expect(screen.getByRole("dialog", { name: "最近会话" })).toBeTruthy();
  });

describe("sidebar shortcut vs. workbench maximize shortcut (batch 4a)", () => {
  const presentation = () => screen.getByTestId("unified-sidebar").dataset.presentation;

  it("Ctrl+Shift+\\ is left to the workbench; without Shift the shortcut toggles the sidebar", () => {
    setViewport(1280);
    renderIntl(<UnifiedSidebar onNewSession={() => "created"} sessions={sessions} />);
    // P2 batch 4: the expanded sidebar is persistent from 1024, so the shortcut toggles the docked mode
    // instead of opening the overlay.
    expect(presentation()).toBe("expanded");
    fireEvent.keyDown(window, { key: "\\", code: "Backslash", ctrlKey: true, shiftKey: true });
    expect(presentation()).toBe("expanded");
    fireEvent.keyDown(window, { key: "\\", code: "Backslash", ctrlKey: true });
    expect(presentation()).toBe("compact");
  });

});
});
