import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import zhMessages from "@/messages/zh-CN.json";
import { renderIntl } from "@/__tests__/intl-render";
import { CHART_AGENT, CHART_CATALOG, chartResult } from "@/__tests__/fixtures/agent-charts";
import { listAgentCatalog } from "@/lib/agents-api";
import { listAgentRankings, type AgentRankingCollection } from "@/lib/agent-rankings-api";
import { AgentCharts } from "./agent-charts";

const { auth, push } = vi.hoisted(() => ({ auth: { status: "authenticated", me: { id: "usr_one", team_id: "team_one", auth_session_generation: "generation_one" } }, push: vi.fn() }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useAuthSession: () => auth, useOptionalAuthSession: () => auth }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push }) }));
vi.mock("@/lib/agents-api", () => ({ listAgentCatalog: vi.fn() }));
vi.mock("@/lib/agent-rankings-api", () => ({ listAgentRankings: vi.fn() }));

describe("AgentCharts", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    auth.status = "authenticated";
    auth.me = { id: "usr_one", team_id: "team_one", auth_session_generation: "generation_one" };
    window.history.replaceState({}, "", "/agents/charts/");
    vi.mocked(listAgentCatalog).mockResolvedValue(CHART_CATALOG);
    vi.mocked(listAgentRankings).mockResolvedValue(null);
  });

  it("keeps unsupported charts honest and uses the server recent-update collection", async () => {
    renderIntl(<AgentCharts />);
    expect(await screen.findByText("榜单暂不可用")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: CHART_AGENT.display_name })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Directory First" })).toBeNull();
    expect(screen.queryByLabelText("第 1 名")).toBeNull();
    expect(screen.queryByText(/18 位成员使用/)).toBeNull();
  });

  it("renders a supported chart with a contextual detail return path", async () => {
    vi.mocked(listAgentRankings).mockResolvedValue(chartResult());
    renderIntl(<AgentCharts />);
    expect(await screen.findByText("18 位成员使用")).toBeTruthy();
    const region = screen.getByRole("region", { name: "热门" });
    const detail = within(region).getAllByRole("link", { name: "查看 Finance Partner" }).at(-1)!;
    expect(detail.getAttribute("href")).toContain("return_page=charts&chart=popular");
    fireEvent.click(detail);
    expect(push).toHaveBeenCalledWith(expect.stringContaining("return_page=charts&chart=popular"));
  });

  it("restores a bookmarked trend category without fetching an unrelated chart", async () => {
    window.history.replaceState({}, "", "/agents/charts/?chart=trending&domain=finance");
    vi.mocked(listAgentRankings).mockResolvedValue(chartResult("trending", "finance"));
    renderIntl(<AgentCharts />);
    expect(await screen.findByText("+8 位活跃成员")).toBeTruthy();
    expect(screen.getByRole("radio", { name: "上升" }).getAttribute("aria-checked")).toBe("true");
    expect(listAgentRankings).toHaveBeenCalledTimes(1);
    expect(listAgentRankings).toHaveBeenCalledWith(expect.objectContaining({ rankingKind: "trending", domain: "finance" }));
  });

  it("does not replace a newly selected trend chart with a late popular response", async () => {
    let finishPopular!: (value: AgentRankingCollection) => void;
    vi.mocked(listAgentRankings).mockImplementation(params => params.rankingKind === "popular" ? new Promise(resolve => { finishPopular = resolve; }) : Promise.resolve(chartResult("trending")));
    renderIntl(<AgentCharts />);
    await waitFor(() => expect(listAgentRankings).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("radio", { name: "上升" }));
    expect(await screen.findByText("+8 位活跃成员")).toBeTruthy();
    await act(async () => finishPopular(chartResult()));
    expect(screen.queryByText("18 位成员使用")).toBeNull();
    expect(window.location.search).toBe("?chart=trending");
  });

  it("clears the previous team's listing and counts while the next team loads", async () => {
    vi.mocked(listAgentRankings).mockResolvedValue(chartResult());
    const rendered = renderIntl(<AgentCharts />);
    expect(await screen.findByText("18 位成员使用")).toBeTruthy();
    vi.mocked(listAgentRankings).mockReturnValue(new Promise(() => {}));
    vi.mocked(listAgentCatalog).mockReturnValue(new Promise(() => {}));
    auth.me = { ...auth.me, team_id: "team_two", auth_session_generation: "generation_two" };
    rendered.rerender(<NextIntlClientProvider locale="zh-CN" messages={zhMessages}><AgentCharts /></NextIntlClientProvider>);
    expect(screen.queryByText("18 位成员使用")).toBeNull();
    expect(screen.queryByRole("heading", { name: CHART_AGENT.display_name })).toBeNull();
    expect(screen.getByText("正在读取榜单…")).toBeTruthy();
  });

  it("keeps recent updates usable when the chart request fails and supports retry", async () => {
    vi.mocked(listAgentRankings).mockRejectedValueOnce(new Error("Private backend diagnostics"));
    renderIntl(<AgentCharts />);
    expect(await screen.findByText("暂时无法读取榜单")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: CHART_AGENT.display_name })).toBeTruthy();
    expect(document.body.textContent).not.toContain("Private backend diagnostics");
    vi.mocked(listAgentRankings).mockResolvedValue(chartResult());
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("18 位成员使用")).toBeTruthy();
  });

  it("uses aggregate-unavailable states without a fabricated number or rank", async () => {
    vi.mocked(listAgentRankings).mockResolvedValue({ ...chartResult(), state: "insufficient_sample", rows: [] });
    renderIntl(<AgentCharts />);
    expect(await screen.findByText("暂未形成榜单")).toBeTruthy();
    expect(screen.queryByLabelText("第 1 名")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "查看最近更新" }));
    expect(screen.getByRole("radio", { name: "最近更新" }).getAttribute("aria-checked")).toBe("true");
    expect(window.location.search).toBe("?chart=updated");
    expect(screen.queryByText("18 位成员使用")).toBeNull();
  });

  it("cancels the previous catalog read on a team change and the current read on unmount", async () => {
    vi.mocked(listAgentCatalog).mockReturnValue(new Promise(() => {}));
    const rendered = renderIntl(<AgentCharts />);
    await waitFor(() => expect(listAgentCatalog).toHaveBeenCalledTimes(1));
    const previousSignal = vi.mocked(listAgentCatalog).mock.calls[0][0]!.signal!;
    auth.me = { ...auth.me, team_id: "team_two" };
    rendered.rerender(<NextIntlClientProvider locale="zh-CN" messages={zhMessages}><AgentCharts /></NextIntlClientProvider>);
    await waitFor(() => expect(listAgentCatalog).toHaveBeenCalledTimes(2));
    expect(previousSignal.aborted).toBe(true);
    const currentSignal = vi.mocked(listAgentCatalog).mock.calls[1][0]!.signal!;
    expect(currentSignal.aborted).toBe(false);
    rendered.unmount();
    expect(currentSignal.aborted).toBe(true);
  });

  it("ignores malformed coverage dates", async () => {
    vi.mocked(listAgentRankings).mockResolvedValue({ ...chartResult(), state: "insufficient_sample", reason: "window_incomplete", available_at: "invalid", rows: [] });
    renderIntl(<AgentCharts />);
    expect(await screen.findByText("正在积累完整统计窗口")).toBeTruthy();
    expect(screen.queryByText(/统计窗口最早可于/)).toBeNull();
  });

  it("explains incomplete coverage separately from too few members", async () => {
    vi.mocked(listAgentRankings).mockResolvedValue({ ...chartResult(), state: "insufficient_sample", reason: "window_incomplete", available_at: "2026-11-02T12:00:00Z", rows: [] });
    renderIntl(<AgentCharts />);
    expect(await screen.findByText("正在积累完整统计窗口")).toBeTruthy();
    expect(screen.getByText(/Top 需要 28 天/)).toBeTruthy();
    // A viewer-zone date (noon UTC is Nov 2 from UTC-11 to UTC+11), with no fixed-zone label.
    expect(screen.getByText(/2026年11月2日/).textContent).not.toContain("UTC");
    expect(screen.getByText(/统计窗口最早可于/).textContent).toContain("榜单仍需有足够合格成员满足隐私要求。");
    expect(screen.queryByLabelText("第 1 名")).toBeNull();
    expect(screen.queryByText("暂未形成榜单")).toBeNull();
  });

  it("uses a Category menu button, single-language chart names and a chevron disclosure for the method note", async () => {
    vi.mocked(listAgentRankings).mockResolvedValue(chartResult());
    renderIntl(<AgentCharts />);
    expect(await screen.findByText("18 位成员使用")).toBeTruthy();

    expect(document.querySelector("select, details")).toBeNull();
    expect(screen.getAllByRole("radio").map((radio) => radio.textContent)).toEqual(["热门", "上升", "最近更新"]);
    const category = await screen.findByTestId("ac-category-filter");
    await waitFor(() => expect(category.textContent).toBe("分类：全部"));
    fireEvent.click(category, { detail: 0 });
    fireEvent.click(within(await screen.findByTestId("ac-category-filter-menu")).getByRole("menuitemradio", { name: /^财务/ }));
    await waitFor(() => expect(listAgentRankings).toHaveBeenLastCalledWith(expect.objectContaining({ domain: "finance" })));
    expect(window.location.search).toContain("domain=finance");

    const toggle = screen.getByRole("button", { name: "榜单说明" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const panel = document.getElementById(toggle.getAttribute("aria-controls")!)!;
    expect(panel.hidden).toBe(true);
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(panel.hidden).toBe(false);
  });
});
