import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import zhMessages from "@/messages/zh-CN.json";
import { renderIntl } from "@/__tests__/intl-render";
import { dashboardAgent, dashboardCollection } from "@/__tests__/fixtures/agent-dashboard";
import { getAgentReleaseInboxCount, listManagedAgents, type AgentManagementCollection } from "@/lib/agents-api";
import { AgentDashboard } from "./agent-dashboard";

const { auth, push } = vi.hoisted(() => ({ auth: { status: "authenticated", me: { id: "usr_one", team_id: "team_one", auth_session_generation: "one", team_role: "team_admin", capabilities: { subagent_management_v2: true } } }, push: vi.fn() }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useAuthSession: () => auth, useOptionalAuthSession: () => auth }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push }) }));
vi.mock("@/lib/agents-api", () => ({ listManagedAgents: vi.fn(), getAgentReleaseInboxCount: vi.fn() }));
vi.mock("./agent-dashboard-chart", () => ({ AgentDashboardChart: ({ rows }: { rows: Array<{ id: string; label: string; count: number }> }) => <ul>{rows.map(row => <li key={row.id}>{row.label}</li>)}</ul> }));

describe("Agent overview", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    auth.status = "authenticated";
    auth.me = { id: "usr_one", team_id: "team_one", auth_session_generation: "one", team_role: "team_admin", capabilities: { subagent_management_v2: true } };
    vi.mocked(listManagedAgents).mockResolvedValue(dashboardCollection([dashboardAgent("one", { display_name: "Blocked Agent", listing_safety: { effective_status: "blocked", deterministic_status: "blocked", semantic_status: "not_run", listing_hash: null, reasons: [] } })]));
    vi.mocked(getAgentReleaseInboxCount).mockResolvedValue({ kind: "agent_release_inbox_count", schema_version: 1, count: 7 });
  });

  it("renders source-backed checks and a contextual detail link", async () => {
    renderIntl(<AgentDashboard />);
    expect(await screen.findByText("Blocked Agent")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1, name: "智能体概览" })).toBeTruthy();
    // D7: the listing breakdown is the blocked tile's secondary line, not a separate three-bar chart.
    const blocked = document.querySelector<HTMLElement>('[data-stat-tile="blocked"]')!;
    expect(within(blocked).getByText("上架检查受阻")).toBeTruthy();
    expect(within(blocked).getByText("1")).toBeTruthy();
    expect(within(blocked).getByText("通过 0 · 暂无证据 0")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "上架内容检查" })).toBeNull();
    expect(screen.getByText("上架检查只看展示内容，不代表智能体质量或运行就绪状态。")).toBeTruthy();
    expect(screen.getByRole("region", { name: "领域覆盖" })).toBeTruthy();
    const link = screen.getByRole("link", { name: "查看详情" });
    fireEvent.click(link);
    expect(push).toHaveBeenCalledWith("/agents/detail/?id=one&return_page=dashboard");
  });

  it.each(["member", "audit_admin"])("does not mount admin data or navigation for %s", role => {
    auth.me.team_role = role;
    renderIntl(<AgentDashboard />);
    expect(listManagedAgents).not.toHaveBeenCalled();
    expect(getAgentReleaseInboxCount).not.toHaveBeenCalled();
  });

  it("clears old team counts immediately and excludes late responses", async () => {
    let finish!: (result: AgentManagementCollection) => void;
    vi.mocked(listManagedAgents).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const rendered = renderIntl(<AgentDashboard />);
    await waitFor(() => expect(listManagedAgents).toHaveBeenCalledTimes(1));
    auth.me = { ...auth.me, team_id: "team_two", auth_session_generation: "two" };
    rendered.rerender(<NextIntlClientProvider locale="zh-CN" messages={zhMessages}><AgentDashboard /></NextIntlClientProvider>);
    expect(screen.queryByText("7")).toBeNull();
    expect(await screen.findByText("Blocked Agent")).toBeTruthy();
    await act(async () => finish(dashboardCollection([dashboardAgent("private", { display_name: "Old Team Secret" })])));
    expect(screen.queryByText("Old Team Secret")).toBeNull();
  });

  it("keeps failed review counts unknown and the valid directory readable", async () => {
    vi.mocked(getAgentReleaseInboxCount).mockRejectedValue(new Error("Private diagnostics"));
    renderIntl(<AgentDashboard />);
    expect(await screen.findByText("Blocked Agent")).toBeTruthy();
    expect(screen.getByText("暂时无法读取待审批数量。可以打开审核队列或刷新。")).toBeTruthy();
    expect(document.body.textContent).not.toContain("Private diagnostics");
  });

  it("lets administrators retry a failed directory while approval counts remain usable", async () => {
    vi.mocked(listManagedAgents).mockRejectedValueOnce(new Error("Private diagnostics"));
    renderIntl(<AgentDashboard />);
    expect(await screen.findByText("暂时无法读取目录概况")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("Blocked Agent")).toBeTruthy();
  });

  it("uses an empty state for a genuinely empty directory", async () => {
    vi.mocked(listManagedAgents).mockResolvedValue(dashboardCollection([]));
    renderIntl(<AgentDashboard />);
    expect(await screen.findByText("暂无管理条目")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "领域覆盖" })).toBeNull();
  });

  it("shows an unread count as a dash rather than zero", async () => {
    vi.mocked(getAgentReleaseInboxCount).mockRejectedValue(new Error("unavailable"));
    renderIntl(<AgentDashboard />);
    expect(await screen.findByText("Blocked Agent")).toBeTruthy();
    const reviews = document.querySelector<HTMLElement>('[data-stat-tile="reviews"]')!;
    expect(within(reviews).getByText("—")).toBeTruthy();
    expect(within(reviews).queryByText("0")).toBeNull();
  });
});
