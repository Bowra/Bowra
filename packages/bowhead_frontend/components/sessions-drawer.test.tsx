import { PRODUCT_NAME } from "@/lib/product-identity";
/**
 * V5 mobile fix · SessionsDrawer + Provider tests.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { archiveSession, fetchSessions, renameSession } from "@/lib/api";
import { fetchMe } from "@/lib/auth-api";
import { resetSessionTitleOverridesForTest } from "@/lib/session-rename";
import { sessionPages, sessionsResource } from "@/lib/use-sessions";
import { spacesResource } from "@/lib/use-space";

import {
  SessionsDrawer,
  SessionsDrawerProvider,
  useSessionsDrawer,
} from "./sessions-drawer";
import type { UnifiedSidebarSessions } from "./unified-sidebar";

const { drawerNavigationState, drawerRouter } = vi.hoisted(() => ({
  drawerNavigationState: { pathname: "/", search: "" },
  drawerRouter: {
    push: vi.fn(),
    replace: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    refresh: vi.fn(),
    prefetch: vi.fn(),
  },
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => {
    const ZH: Record<string, string> = {
      new_work: "新工作",
      reports: "我的报告",
      memory: "记忆",
      skills: "技能",
      settings: "设置",
      library: "资料库",
      capabilities: "能力中心",
      "centers.agents": "智能体",
      files: "文件",
      knowledge: "知识库",
      expand_group: "展开分组",
      collapse_group: "收起分组",
      navigation_drawer: "导航抽屉",
      close_drawer: "关闭抽屉",
      primary_navigation: "主导航",
      rail_loading: "正在加载会话",
      rail_load_failed: "会话列表没有加载出来",
      rail_search_retry: "重试",
      rail_load_older: "更早的会话",
      rail_no_older: "没有更早的会话了",
      rail_load_older_failed: "没有加载出更早的会话",
      rail_older_loaded_announce: "已加载更早的会话",
      rail_empty: "还没有会话",
      rail_empty_hint: "从「新工作」开始第一个分析。",
      retry: "重试",
    };
    return ZH[key] ?? key;
  },
  useLocale: () => "zh-CN",
  useTimeZone: () => "UTC",
}));

// P4 drawer profile footer — stub the footer's child controls; they have their
// own tests and pull in deps (auth/me fetch, useLocale) irrelevant to the drawer.
vi.mock("@/components/user-menu", () => ({
  UserMenu: ({
    onBeforePersonalSettingsNavigate,
  }: {
    onBeforePersonalSettingsNavigate?: () => unknown;
  }) => (
    <button type="button" onClick={() => onBeforePersonalSettingsNavigate?.()}>
      open-personal-settings
    </button>
  ),
}));
vi.mock("@/lib/auth-api", () => ({
  fetchMe: vi.fn().mockResolvedValue({
    id: "usr_1",
    email: "member@example.com",
    display_name: "Member",
    team_role: "member",
    is_team_admin: false,
    capabilities: {},
  }),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => drawerNavigationState.pathname,
  useSearchParams: () => new URLSearchParams(drawerNavigationState.search),
  useRouter: () => drawerRouter,
}));

vi.mock("@/lib/api", () => ({
  fetchSessions: vi.fn().mockResolvedValue({
    groups: [
      {
        id: "today",
        label: "TODAY",
        items: [
          { id: "s_1", title: "Q3 营收分析", intents: ["lookup"], time: "10m", state: "done" },
          { id: "s_2", title: "客户分群", intents: ["report"], time: "1h", state: "done" },
        ],
      },
    ],
  }),
  archiveSession: vi.fn().mockResolvedValue(undefined),
  restoreSession: vi.fn().mockResolvedValue(undefined),
  pinSession: vi.fn(),
  unpinSession: vi.fn(),
  renameSession: vi.fn(),
  // The drawer reads the shared session list, which scopes itself to the
  // active space (lib/use-sessions → lib/use-space).
  fetchWorkspaces: vi.fn().mockResolvedValue({
    items: [{ id: "ws_1", name: "Space", desc: "" }],
    active_id: "ws_1",
  }),
  setLastWorkspace: vi.fn().mockResolvedValue(undefined),
}));

// The drawer's session list only talks to the API outside mock mode; the
// fixtures branch is exercised by the sidebar tests.
beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_BOWHEAD_SSE_MODE", "sse");
  // Shared resources are module-level by design — reset so each test starts
  // from a cold cache instead of the previous test's response.
  sessionsResource.reset();
  sessionPages.reset();
  spacesResource.reset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  delete window.__BOWHEAD_BASE_PATH__;
});


function Harness({
  onNewSession = () => "created",
  archiveEnabled = false,
  navigationPending,
  sessions,
}: {
  navigationPending?: boolean;
  onNewSession?: () =>
    | "created"
    | "cancelled"
    | "navigating"
    | Promise<"created" | "cancelled" | "navigating">;
  archiveEnabled?: boolean;
  sessions?: UnifiedSidebarSessions;
}) {
  return (
    <SessionsDrawerProvider>
      <OpenButton />
      <SessionsDrawer
        onNewSession={onNewSession}
        navigationPending={navigationPending}
        archiveEnabled={archiveEnabled}
        sessions={sessions}
      />
    </SessionsDrawerProvider>
  );
}

function OpenButton() {
  const { open } = useSessionsDrawer();
  return <button data-testid="open-btn" onClick={(event) => open(event.currentTarget)}>open</button>;
}


describe("SessionsDrawer", () => {
  beforeEach(() => {
    drawerNavigationState.pathname = "/";
    drawerNavigationState.search = "";
    drawerRouter.push.mockReset();
    drawerRouter.replace.mockReset();
    vi.mocked(archiveSession).mockReset().mockResolvedValue(undefined);
    vi.mocked(fetchSessions).mockReset().mockResolvedValue({
      groups: [
        {
          id: "today",
          label: "TODAY",
          items: [
            {
              id: "s_1",
              title: "Q3 营收分析",
              intents: ["lookup"],
              time: "10m",
              state: "done",
            },
            {
              id: "s_2",
              title: "客户分群",
              intents: ["report"],
              time: "1h",
              state: "done",
            },
          ],
        },
      ],
    });
    // jsdom does not implement window.location.assign; stub it
    Object.defineProperty(window, "location", {
      value: { ...window.location, search: "", href: "" },
      writable: true,
    });
  });

  it("not rendered when closed", () => {
    render(<Harness />);
    expect(screen.queryByTestId("sessions-drawer")).toBeNull();
    expect(screen.queryByTestId("sessions-drawer-mask")).toBeNull();
  });

  it("opens via context.open()", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    expect(screen.getByTestId("sessions-drawer")).toBeTruthy();
    expect(screen.getByTestId("sessions-drawer-mask")).toBeTruthy();
    expect(screen.getByRole("dialog", { name: "导航抽屉" })).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: PRODUCT_NAME })
        .querySelector("svg")
        ?.getAttribute("viewBox"),
    ).toBe("200 380 850 480");
    expect(screen.getByRole("button", { name: "关闭抽屉" })).toBeTruthy();
  });

  it("renders the available work destinations", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    const newWork = screen.getByTestId("unified-drawer-nav-new_work");
    expect(newWork).toBeTruthy();
    expect(newWork.getAttribute("aria-current")).toBe("page");
    expect(newWork.classList.contains("is-active")).toBe(true);
    expect(screen.getByTestId("unified-drawer-nav-reports")).toBeTruthy();
    expect(screen.getByTestId("unified-drawer-nav-library")).toBeTruthy();
    expect(screen.getByTestId("unified-drawer-nav-capabilities")).toBeTruthy();
  });

  it("keeps Agent details in the primary phone navigation", async () => {
    const identity = await vi.mocked(fetchMe)();
    vi.mocked(fetchMe).mockResolvedValueOnce({ ...identity, capabilities: { subagent_management_v2: true } });
    drawerNavigationState.pathname = "/agents/detail/";
    drawerNavigationState.search = "agent_id=sa_1&return_view=shared";
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    const entry = await screen.findByTestId("unified-drawer-nav-agents");
    expect(entry.getAttribute("href")).toBe("/agents/manage/hired/");
    expect(screen.getByTestId("unified-drawer-nav-my_agents").getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeTruthy();
    fireEvent.click(entry);
    await waitFor(() => expect(window.location.href).toBe("/agents/manage/hired/"));
    expect(screen.queryByTestId("sessions-drawer")).toBeNull();
  });

  it("prefixes drawer links at runtime", () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));

    expect(
      screen.getByTestId("unified-drawer-nav-reports").getAttribute("href"),
    ).toBe("/bowhead/reports/");
  });

  it("moves the drawer active state from New work to reports", () => {
    drawerNavigationState.pathname = "/reports/";
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));

    expect(
      screen
        .getByTestId("unified-drawer-nav-new_work")
        .getAttribute("aria-current"),
    ).toBeNull();
    expect(
      screen
        .getByTestId("unified-drawer-nav-reports")
        .getAttribute("aria-current"),
    ).toBe("page");
  });

  it("nav items show zh-CN labels", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    const drawer = screen.getByTestId("sessions-drawer");
    expect(drawer.textContent).toContain("新工作");
    expect(drawer.textContent).not.toContain("新建分析");
    expect(drawer.textContent).toContain("资料库");
    expect(drawer.textContent).toContain("能力中心");
  });

  it("runs the shared New work action and closes only after success", async () => {
    const onNewSession = vi.fn(() => Promise.resolve("created" as const));
    render(<Harness onNewSession={onNewSession} />);
    fireEvent.click(screen.getByTestId("open-btn"));

    const action = screen.getByTestId("unified-drawer-nav-new_work");
    expect(action.tagName).toBe("BUTTON");
    expect(action.getAttribute("href")).toBeNull();
    fireEvent.click(action);

    await waitFor(() => expect(onNewSession).toHaveBeenCalledOnce());
    await waitFor(() =>
      expect(screen.queryByTestId("sessions-drawer")).toBeNull(),
    );
  });

  it("shows pending feedback and prevents duplicate New work requests", async () => {
    let finish: ((result: "created") => void) | undefined;
    const onNewSession = vi.fn(
      () =>
        new Promise<"created">((resolve) => {
          finish = resolve;
        }),
    );
    render(<Harness onNewSession={onNewSession} />);
    fireEvent.click(screen.getByTestId("open-btn"));

    const action = screen.getByTestId("unified-drawer-nav-new_work");
    fireEvent.click(action);
    await waitFor(() =>
      expect(action.getAttribute("aria-busy")).toBe("true"),
    );
    expect((action as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(action);
    expect(onNewSession).toHaveBeenCalledOnce();

    finish?.("created");
    await waitFor(() =>
      expect(screen.queryByTestId("sessions-drawer")).toBeNull(),
    );
  });

  it("keeps New work pending while a document navigation starts", async () => {
    render(<Harness onNewSession={() => "navigating"} />);
    fireEvent.click(screen.getByTestId("open-btn"));

    const action = screen.getByTestId("unified-drawer-nav-new_work");
    fireEvent.click(action);

    await waitFor(() =>
      expect(action.getAttribute("aria-busy")).toBe("true"),
    );
    expect((action as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("sessions-drawer")).toBeTruthy();
  });

  it("shows New work as pending while the shell navigates after a leave confirmation", () => {
    render(<Harness navigationPending />);
    fireEvent.click(screen.getByTestId("open-btn"));

    const action = screen.getByTestId("unified-drawer-nav-new_work");
    expect(action.getAttribute("aria-busy")).toBe("true");
    expect((action as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps the drawer open when New work is cancelled", async () => {
    render(<Harness onNewSession={() => Promise.resolve("cancelled")} />);
    fireEvent.click(screen.getByTestId("open-btn"));
    fireEvent.click(screen.getByTestId("unified-drawer-nav-new_work"));

    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId("sessions-drawer")).toBeTruthy();
  });

  it("ends the page-supplied list with a 44px older-sessions row", async () => {
    const onLoadMore = vi.fn();
    render(<Harness sessions={{
      groups: [{
        id: "older",
        label: "OLDER",
        items: [{ id: "s_old", title: "门店客流归因", intents: [], time: "40d", state: "done" }],
      }],
      currentSessionId: null,
      onPick: vi.fn(),
      olderSessions: {
        hasMore: true,
        loadingMore: false,
        loadMoreError: false,
        olderPagesLoaded: 0,
        onLoadMore,
      },
    }} />);
    fireEvent.click(screen.getByTestId("open-btn"));

    const row = await screen.findByRole("button", { name: "更早的会话" });
    expect(row.className).toContain("is-touch");
    fireEvent.click(row);
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("loads older sessions into its own list and ends with the closing line", async () => {
    vi.mocked(fetchSessions).mockReset().mockImplementation(async (options) => {
      if (options?.cursor === "c1") {
        return {
          groups: [{
            id: "older",
            label: "OLDER",
            items: [{ id: "s_old", title: "2025 双十一复盘", intents: [], time: "300d", state: "done" }],
          }],
          next_cursor: null,
          has_more: false,
        };
      }
      return {
        groups: [{
          id: "today",
          label: "TODAY",
          items: [{ id: "s_1", title: "Q3 营收分析", intents: [], time: "10m", state: "done" }],
        }],
        archived_count: 0,
        next_cursor: "c1",
        has_more: true,
      };
    });
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));

    fireEvent.click(await screen.findByRole("button", { name: "更早的会话" }));
    expect(await screen.findByText("2025 双十一复盘")).toBeTruthy();
    expect(screen.getByText("没有更早的会话了")).toBeTruthy();
    // Focus follows the ids the new page added.
    await waitFor(() =>
      expect(document.activeElement?.textContent).toContain("2025 双十一复盘"),
    );
    expect(screen.getAllByText("Q3 营收分析")).toHaveLength(1);
    expect(vi.mocked(fetchSessions).mock.calls.some(([options]) => options?.cursor === "c1")).toBe(true);
  });

  it("closes after the page accepts a session pick", async () => {
    const onPick = vi.fn().mockResolvedValue("picked");
    render(<Harness sessions={{
      groups: [{
        id: "today",
        label: "TODAY",
        items: [{
          id: "s_pick",
          title: "可渐进加载的会话",
          intents: [],
          time: "now",
          state: "done",
        }],
      }],
      currentSessionId: null,
      onPick,
    }} />);
    fireEvent.click(screen.getByTestId("open-btn"));
    fireEvent.click(await screen.findByText("可渐进加载的会话"));

    await waitFor(() => expect(onPick).toHaveBeenCalledWith("s_pick"));
    await waitFor(() =>
      expect(screen.queryByTestId("sessions-drawer")).toBeNull(),
    );
  });

  it("keeps the drawer open when a session pick is cancelled", async () => {
    const onPick = vi.fn().mockResolvedValue("cancelled");
    render(<Harness sessions={{
      groups: [{
        id: "today",
        label: "TODAY",
        items: [{
          id: "s_cancel",
          title: "保留当前报告",
          intents: [],
          time: "now",
          state: "done",
        }],
      }],
      currentSessionId: "s_current",
      onPick,
    }} />);
    fireEvent.click(screen.getByTestId("open-btn"));
    fireEvent.click(await screen.findByText("保留当前报告"));

    await waitFor(() => expect(onPick).toHaveBeenCalledWith("s_cancel"));
    expect(screen.getByTestId("sessions-drawer")).toBeTruthy();
  });

  it("keeps a modified unified destination link click native and leaves the drawer open", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));

    const library = await screen.findByTestId("unified-drawer-nav-reports");
    const event = new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
      button: 0,
    });
    library.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(false);
    expect(screen.getByTestId("sessions-drawer")).toBeTruthy();
  });

  it("expands Library without leaving work", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    await waitFor(() => expect(screen.getByTestId("unified-drawer-nav-library").tagName).toBe("BUTTON"));
    fireEvent.click(screen.getByTestId("unified-drawer-nav-library"));
    expect(screen.getByText("文件")).toBeTruthy();
    expect(screen.getByText("知识库")).toBeTruthy();
    expect(screen.getByTestId("sessions-drawer")).toBeTruthy();
    expect(drawerRouter.push).not.toHaveBeenCalled();
  });

  it("focuses the close button and dismisses on ESC", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    expect(screen.queryByTestId("sessions-drawer")).toBeTruthy();
    // Send Esc keydown to current focus; opening the drawer focuses its close button.
    expect(document.activeElement).toBe(screen.getByTestId("sessions-drawer-close"));
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    expect(screen.queryByTestId("sessions-drawer")).toBeNull();
  });

  it("mask click dismisses", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    fireEvent.click(screen.getByTestId("sessions-drawer-mask"));
    expect(screen.queryByTestId("sessions-drawer")).toBeNull();
  });

  it("close button dismisses", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    fireEvent.click(screen.getByTestId("sessions-drawer-close"));
    expect(screen.queryByTestId("sessions-drawer")).toBeNull();
  });

  it("returns focus to the opener after closing", async () => {
    render(<Harness />);
    const opener = screen.getByTestId("open-btn");
    opener.focus();
    fireEvent.click(opener);
    fireEvent.click(screen.getByTestId("sessions-drawer-close"));
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it("returns focus to the opener even when tapping it did not focus it (Safari)", async () => {
    render(<Harness />);
    const opener = screen.getByTestId("open-btn");
    // Safari does not focus clicked buttons: on opening, activeElement still points at the previous input.
    const elsewhere = document.createElement("input");
    document.body.append(elsewhere);
    elsewhere.focus();
    fireEvent.click(opener);
    fireEvent.click(screen.getByTestId("sessions-drawer-close"));
    await waitFor(() => expect(document.activeElement).toBe(opener));
    elsewhere.remove();
  });

  it("leaves focus alone when closing for personal settings navigation", async () => {
    render(<Harness />);
    const opener = screen.getByTestId("open-btn");
    opener.focus();
    fireEvent.click(opener);
    fireEvent.click(screen.getByRole("button", { name: "open-personal-settings" }));
    expect(screen.queryByTestId("sessions-drawer")).toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(document.activeElement).not.toBe(opener);
  });

  it("body click does NOT dismiss", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    fireEvent.click(screen.getByTestId("sessions-drawer"));
    // Drawer should still be open
    expect(screen.queryByTestId("sessions-drawer")).toBeTruthy();
  });

  it("nav link click closes drawer", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    fireEvent.click(screen.getByTestId("unified-drawer-nav-reports"));
    await waitFor(() =>
      expect(screen.queryByTestId("sessions-drawer")).toBeNull(),
    );
  });

  it("loads sessions via fetchSessions on open", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    await waitFor(() => {
      expect(screen.getByText("Q3 营收分析")).toBeTruthy();
      expect(screen.getByText("客户分群")).toBeTruthy();
    });
  });

  it("replaces the current deep link after a fallback archive", async () => {
    drawerNavigationState.search = "session=s_1";
    render(<Harness archiveEnabled />);
    fireEvent.click(screen.getByTestId("open-btn"));
    const row = await screen.findByText("Q3 营收分析");
    const item = row.closest(".unified-work-item");
    expect(item).not.toBeNull();

    fireEvent.keyDown(
      item!.querySelector<HTMLButtonElement>('button[aria-haspopup="menu"]')!,
      { key: "ArrowDown" },
    );
    fireEvent.click(
      await screen.findByRole("menuitem", { name: "rail_archive_title" }),
    );

    await waitFor(() =>
      expect(archiveSession).toHaveBeenCalledWith("s_1"),
    );
    expect(drawerRouter.replace).toHaveBeenCalledWith("/");
    expect(drawerRouter.push).not.toHaveBeenCalled();
  });

  it("offers Rename first in the phone row menu and saves through the sessions API", async () => {
    vi.mocked(renameSession).mockResolvedValue({ id: "s_1", title: "Q3 复盘" });
    render(<Harness archiveEnabled />);
    fireEvent.click(screen.getByTestId("open-btn"));
    const row = await screen.findByText("Q3 营收分析");
    const item = row.closest(".unified-work-item");
    fireEvent.keyDown(
      item!.querySelector<HTMLButtonElement>('button[aria-haspopup="menu"]')!,
      { key: "ArrowDown" },
    );
    const items = await screen.findAllByRole("menuitem");
    expect(items[0].textContent).toBe("rail_rename_title…");
    expect(items[items.length - 1].textContent).toBe("rail_delete_title");
    fireEvent.click(items[0]);

    const field = await screen.findByRole("textbox", { name: "rename_field_label" });
    fireEvent.change(field, { target: { value: "Q3 复盘" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(renameSession).toHaveBeenCalledWith("s_1", "Q3 复盘"));
    resetSessionTitleOverridesForTest();
  });

  it("distinguishes a load failure from an empty session list and retries", async () => {
    vi.mocked(fetchSessions).mockRejectedValueOnce(new Error("offline"));
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));

    expect(await screen.findByText("会话列表没有加载出来")).toBeTruthy();
    expect(screen.queryByText("还没有会话")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    await waitFor(() => expect(screen.getByText("Q3 营收分析")).toBeTruthy());
    expect(screen.queryByText("会话列表没有加载出来")).toBeNull();
  });

  it("shows skeleton rows while the first load is pending and the empty state only after it settles", async () => {
    let resolveFirst: ((value: { groups: never[] }) => void) | undefined;
    vi.mocked(fetchSessions).mockImplementationOnce(
      () => new Promise((resolve) => { resolveFirst = resolve as (value: { groups: never[] }) => void; }),
    );
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));

    const skeleton = await screen.findByRole("status", { name: "正在加载会话" });
    expect(skeleton.getAttribute("aria-busy")).toBe("true");
    expect(skeleton.querySelectorAll(".ui-skeleton-row")).toHaveLength(6);
    expect(skeleton.textContent).toBe("正在加载会话");
    expect(screen.queryByText("还没有会话")).toBeNull();
    expect(screen.queryByText("正在加载会话…")).toBeNull();

    await act(async () => { resolveFirst?.({ groups: [] }); });
    expect(await screen.findByText("还没有会话")).toBeTruthy();
    expect(screen.getByText("从「新工作」开始第一个分析。")).toBeTruthy();
    expect(screen.queryByRole("status", { name: "正在加载会话" })).toBeNull();
  });

  it("reuses the in-flight request instead of starting a second one", async () => {
    // Ownership contract: refresh triggers (window focus here) fold into the
    // request already in flight. The list can no longer be overwritten by an
    // older response, because two responses are never outstanding at once.
    let resolveFirst: ((value: { groups: never[] }) => void) | undefined;
    vi.mocked(fetchSessions)
      .mockReset()
      .mockImplementation(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve as (value: { groups: never[] }) => void;
          }),
      );
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    await waitFor(() => expect(fetchSessions).toHaveBeenCalledTimes(1));

    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });
    expect(fetchSessions).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirst?.({ groups: [] });
      await Promise.resolve();
    });
    expect(screen.queryByText("会话加载失败。")).toBeNull();
  });

  it("body scroll locked when drawer open, unlocked when closed", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-btn"));
    expect(document.body.style.overflow).toBe("hidden");
    expect(document.body.style.getPropertyPriority("overflow")).toBe(
      "important",
    );
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    // After cleanup, overflow restored to prev (empty in test)
    expect(document.body.style.overflow).toBe("");
  });
});
