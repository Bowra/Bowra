import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";

import { NextIntlClientProvider } from "next-intl";
import zhMessages from "@/messages/zh-CN.json";

import { renderIntl } from "@/__tests__/intl-render";
import type { NavigationContext } from "@/lib/navigation-centers";
import type { MeUser } from "@/lib/auth-api";
import { resetSettingsNavCountsForTests, SETTINGS_NAV_COUNTS_TTL_MS } from "@/lib/settings-nav-counts";
import { notifySettingsNavCountsChanged } from "@/lib/settings-nav-signal";
import {
  SettingsSidebarNav,
  useSettingsNavModel,
  type SettingsNavModel,
} from "./settings-sidebar-nav";

const { space, api } = vi.hoisted(() => ({
  space: {
    items: [{ id: "ws_1", name: "零售经营分析", desc: "", role: "admin" as "admin" | "member" | "viewer" }],
    activeId: "ws_1",
  },
  api: {
    fetchApprovals: vi.fn(),
    fetchScheduledJobs: vi.fn(),
    listAccessRequests: vi.fn(),
    getAuthCapabilities: vi.fn(),
    fetchFeedbackPendingCount: vi.fn(),
  },
}));

vi.mock("@/lib/use-space", () => ({ useSpaceSelection: () => space }));
vi.mock("@/lib/api", () => ({
  fetchApprovals: api.fetchApprovals,
  fetchScheduledJobs: api.fetchScheduledJobs,
}));
vi.mock("@/lib/access-requests-api", () => ({ listAccessRequests: api.listAccessRequests }));
vi.mock("@/lib/invite-api", () => ({ getAuthCapabilities: api.getAuthCapabilities }));
vi.mock("@/lib/feedback-candidates-api", () => ({ fetchFeedbackPendingCount: api.fetchFeedbackPendingCount }));

function user(role: MeUser["team_role"], capabilities: MeUser["capabilities"] = {}): MeUser {
  return {
    id: "u_1",
    email: "a@example.com",
    display_name: "A",
    avatar_url: null,
    team_id: "team_1",
    tier: "pro",
    role: "user",
    team_role: role,
    status: "active",
    email_verified: true,
    credit_balance: 1,
    credit_quota: 1,
    locale: "zh-CN",
    timezone: "Asia/Shanghai",
    is_team_admin: role === "owner" || role === "team_admin",
    workspace_count: 1,
    needs_onboarding: false,
    capabilities,
  };
}

function Harness({
  me,
  active = { tab: "profile", admin: null },
  onNavigate = () => {},
  context = "settings",
}: {
  me: MeUser;
  context?: NavigationContext;
  active?: SettingsNavModel["active"];
  onNavigate?: (route: string) => void;
}) {
  const model = useSettingsNavModel({ me, active, enabled: true, refreshKey: "k", context });
  return (
    <SettingsSidebarNav
      model={model}
      surface="sidebar"
      onNavigate={(event, route) => {
        event.preventDefault();
        onNavigate(route);
      }}
    />
  );
}

function groupTitles(): string[] {
  return screen.getAllByRole("group").map((group) => group.getAttribute("aria-labelledby") ?? "")
    .map((id) => document.getElementById(id)?.textContent ?? "");
}

describe("SettingsSidebarNav", () => {
  afterEach(() => {
    cleanup();
    resetSettingsNavCountsForTests();
    vi.useRealTimers();
    vi.resetAllMocks();
    window.sessionStorage.clear();
    delete window.__BOWHEAD_BASE_PATH__;
    space.items = [{ id: "ws_1", name: "零售经营分析", desc: "", role: "admin" }];
  });

  function mockData() {
    api.getAuthCapabilities.mockResolvedValue({ deployment_mode: "saas", team_openapi: false });
    api.fetchApprovals.mockResolvedValue([{ id: "a1" }, { id: "a2" }, { id: "a3" }]);
    api.fetchScheduledJobs.mockResolvedValue({ items: [{ status: "active" }, { status: "paused" }] });
    api.listAccessRequests.mockResolvedValue({ items: [{ id: "r1" }] });
    api.fetchFeedbackPendingCount.mockResolvedValue({ count: 4 });
  }

  it("lists every scope an owner can enter, in order, with the current page selected", async () => {
    mockData();
    renderIntl(<Harness me={user("owner")} />);
    expect(groupTitles()).toEqual([
      "个人",
      "空间 · 零售经营分析",
      "团队 · 成员与空间",
      "团队 · AI 与数据",
      "团队 · 接入与安全",
      "团队 · 外观与策略",
      // Operations is generally available since 7a6f870ae removed its rollout switch.
      "团队 · 日常管理",
    ]);
    const current = screen.getByRole("link", { current: "page" });
    expect(current.textContent).toBe("账号");
    expect(screen.getByRole("navigation", { name: "设置导航" })).toBeTruthy();
  });

  it("shows execution approvals only in the administration directory", async () => {
    mockData();
    renderIntl(<Harness me={user("owner")} context="administration" />);
    const approvals = await screen.findByRole("link", { name: /审批.*3 个待处理/ });
    expect(approvals.querySelector(".unified-settings-count.is-pending")?.firstElementChild?.textContent).toBe("3");
    expect(api.fetchScheduledJobs).not.toHaveBeenCalled();
    expect(api.listAccessRequests).not.toHaveBeenCalled();
    expect(api.getAuthCapabilities).not.toHaveBeenCalled();
  });

  it("shows personal active schedules in the capability directory", async () => {
    mockData();
    renderIntl(<Harness me={user("member")} context="capabilities" />);
    const scheduled = await screen.findByRole("link", { name: /定时任务.*1 个启用中/ });
    expect(scheduled.querySelector(".unified-settings-count")?.classList.contains("is-pending")).toBe(false);
    expect(api.fetchApprovals).not.toHaveBeenCalled();
    expect(api.listAccessRequests).not.toHaveBeenCalled();
  });

  it("does not request team counters for members, hides the team groups and offers only the Space instructions", async () => {
    mockData();
    space.items = [{ id: "ws_1", name: "零售经营分析", desc: "", role: "member" }];
    renderIntl(<Harness me={user("member", { access_requests: true })} />);
    expect(groupTitles()).toEqual(["个人", "空间 · 零售经营分析"]);
    expect(screen.getByRole("link", { name: "空间说明" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "成员" })).toBeNull();
    await waitFor(() => expect(screen.getByRole("link", { name: /访问申请/ }).textContent).toContain("1 个待处理"));
    expect(api.fetchApprovals).not.toHaveBeenCalled();
    expect(api.fetchScheduledJobs).not.toHaveBeenCalled();
  });

  it.each(["member", "audit_admin"] as const)("hides Team settings on a denied deep link for %s", (role) => {
    mockData();
    space.items = [{ id: "ws_1", name: "零售经营分析", desc: "", role: "viewer" }];
    renderIntl(<Harness me={user(role, { feedback_loop: true })} active={{ tab: "people", admin: null }} />);
    expect(groupTitles()).toEqual(["个人", "空间 · 零售经营分析"]);
    expect(screen.queryByTestId("settings-nav-tab:people")).toBeNull();
    expect(api.fetchFeedbackPendingCount).not.toHaveBeenCalled();
  });

  it("removes gated entries and current selection when capabilities are revoked", async () => {
    mockData();
    const view = renderIntl(<Harness me={user("owner", { access_requests: true, feedback_loop: true })} active={{ tab: "feedback-inbox", admin: null }} />);
    await screen.findByRole("link", { name: /建议收件箱/ });
    expect(screen.getByTestId("settings-nav-tab:access-requests")).toBeTruthy();
    view.rerender(<NextIntlClientProvider locale="zh-CN" messages={zhMessages}><Harness me={user("owner")} active={{ tab: "feedback-inbox", admin: null }} /></NextIntlClientProvider>);
    expect(screen.queryByTestId("settings-nav-tab:feedback-inbox")).toBeNull();
    expect(screen.queryByTestId("settings-nav-tab:access-requests")).toBeNull();
    expect(screen.queryByTestId("settings-nav-tab:openapi")).toBeNull();
  });

  it("hides an unavailable OpenAPI deep link when deployment facts fail to load", async () => {
    mockData();
    api.getAuthCapabilities.mockRejectedValue(new Error("unavailable"));
    renderIntl(<Harness me={user("owner")} active={{ tab: "openapi", admin: null }} />);
    await waitFor(() => expect(api.getAuthCapabilities).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("settings-nav-tab:openapi")).toBeNull();
  });

  it("filters rows by name and says when nothing matches", () => {
    mockData();
    renderIntl(<Harness me={user("owner")} />);
    const search = screen.getByRole("searchbox", { name: "搜索设置" });
    fireEvent.change(search, { target: { value: "模型" } });
    const nav = screen.getByRole("navigation", { name: "设置导航" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(["模型"]);
    fireEvent.change(search, { target: { value: "zzz" } });
    expect(screen.getByRole("status").textContent).toBe("没有匹配的设置");
  });

  it("goes back to the last work page under the public-entry prefix", () => {
    mockData();
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    window.sessionStorage.setItem("bowhead.nav.last_work_route", "/files/");
    const onNavigate = vi.fn();
    renderIntl(<Harness me={user("owner")} onNavigate={onNavigate} />);
    const back = screen.getByRole("link", { name: "返回工作" });
    expect(back.getAttribute("href")).toBe("/bowhead/files/");
    fireEvent.click(back);
    expect(onNavigate).toHaveBeenCalledWith("/files/");
    expect(screen.getByRole("link", { name: "成员" }).getAttribute("href")).toBe("/bowhead/team/settings/people/");
  });

  it("highlights an administration page by its route", () => {
    mockData();
    space.items = [{ id: "ws_1", name: "零售经营分析", desc: "", role: "viewer" }];
    renderIntl(<Harness me={user("audit_admin")} active={{ tab: null, admin: "audit" }} context="administration" />);
    expect(groupTitles()).toEqual(["审计"]);
    expect(screen.getByRole("link", { current: "page" }).textContent).toBe("审计日志");
  });

  it("shows the suggestion inbox pending pill for admins with the feedback loop", async () => {
    mockData();
    renderIntl(<Harness me={user("owner", { feedback_loop: true })} />);
    await waitFor(() => expect(screen.getByRole("link", { name: /建议收件箱/ }).textContent).toContain("4 个待处理"));
    expect(api.fetchFeedbackPendingCount).toHaveBeenCalledTimes(1);
  });

  it("shares one set of count requests between directories and reuses it within the TTL", async () => {
    mockData();
    const owner = user("owner", { feedback_loop: true, access_requests: true });
    const view = renderIntl(
      <>
        <Harness me={owner} context="administration" />
        <Harness me={owner} context="administration" />
      </>,
    );
    await waitFor(() => expect(screen.getAllByText("3 个待处理").length).toBe(2));
    expect(api.fetchApprovals).toHaveBeenCalledTimes(1);
    expect(api.fetchScheduledJobs).not.toHaveBeenCalled();
    expect(api.listAccessRequests).not.toHaveBeenCalled();
    expect(api.fetchFeedbackPendingCount).not.toHaveBeenCalled();
    expect(api.getAuthCapabilities).not.toHaveBeenCalled();
    view.unmount();
    // Remounting (another settings page) within the TTL issues no new requests.
    renderIntl(<Harness me={owner} context="administration" />);
    await waitFor(() => expect(screen.getByText("3 个待处理")).toBeTruthy());
    expect(api.fetchApprovals).toHaveBeenCalledTimes(1);
  });

  it("refetches after the TTL and right away after a decision", async () => {
    mockData();
    const now = Date.now();
    const spy = vi.spyOn(Date, "now").mockReturnValue(now);
    const owner = user("owner");
    const view = renderIntl(<Harness me={owner} context="administration" />);
    await waitFor(() => expect(screen.getByText("3 个待处理")).toBeTruthy());
    expect(api.fetchApprovals).toHaveBeenCalledTimes(1);
    api.fetchApprovals.mockResolvedValue([{ id: "a1" }]);
    await act(async () => notifySettingsNavCountsChanged());
    await waitFor(() => expect(screen.getByText("1 个待处理")).toBeTruthy());
    expect(api.fetchApprovals).toHaveBeenCalledTimes(2);
    view.unmount();
    spy.mockReturnValue(now + SETTINGS_NAV_COUNTS_TTL_MS + 1);
    renderIntl(<Harness me={owner} context="administration" />);
    await waitFor(() => expect(api.fetchApprovals).toHaveBeenCalledTimes(3));
    spy.mockRestore();
  });

  it("does not reuse another viewer's counts", async () => {
    mockData();
    const view = renderIntl(<Harness me={user("owner")} context="administration" />);
    await waitFor(() => expect(screen.getByText("3 个待处理")).toBeTruthy());
    view.unmount();
    space.items = [{ id: "ws_1", name: "零售经营分析", desc: "", role: "member" }];
    renderIntl(<Harness me={{ ...user("member"), id: "u_2" }} />);
    expect(screen.queryByText("3 个待处理")).toBeNull();
  });
});
