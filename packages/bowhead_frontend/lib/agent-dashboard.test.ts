import { beforeEach, describe, expect, it, vi } from "vitest";
import { dashboardAgent, dashboardCollection } from "@/__tests__/fixtures/agent-dashboard";
import { listManagedAgents } from "./agents-api";
import { agentDashboardAttention, agentDashboardDetailHref, agentDashboardReturnHref, agentListingState, readAgentDashboard } from "./agent-dashboard";

vi.mock("./agents-api", () => ({ listManagedAgents: vi.fn() }));
const signal = () => new AbortController().signal;

describe("Agent dashboard projection", () => {
  beforeEach(() => vi.resetAllMocks());

  it("reads every team page before returning the visible directory", async () => {
    vi.mocked(listManagedAgents).mockResolvedValueOnce(dashboardCollection([dashboardAgent("one")], "next", 2)).mockResolvedValueOnce(dashboardCollection([dashboardAgent("two")], null, 2));
    const result = await readAgentDashboard(signal());
    expect(result.items.map(item => item.agent_id)).toEqual(["one", "two"]);
    expect(result.omitted).toBe(false);
    expect(listManagedAgents).toHaveBeenLastCalledWith("team", expect.objectContaining({ cursor: "next", limit: 100 }));
  });

  it("rejects a non-admin response canonicalized to the personal view", async () => {
    vi.mocked(listManagedAgents).mockResolvedValue({ ...dashboardCollection([dashboardAgent("private")]), view: "hired", views: { hired: true, created: true, shared: true, archived: true, team: false } });
    await expect(readAgentDashboard(signal())).rejects.toMatchObject({ reason: "forbidden" });
  });

  it.each(["repeated cursor", "duplicate entry", "changing total"])("rejects an incomplete directory caused by %s", async reason => {
    vi.mocked(listManagedAgents).mockResolvedValueOnce(dashboardCollection([dashboardAgent("one")], "next", 3)).mockResolvedValueOnce(dashboardCollection([dashboardAgent(reason === "duplicate entry" ? "one" : "two")], reason === "repeated cursor" ? "next" : null, reason === "changing total" ? 4 : 3));
    await expect(readAgentDashboard(signal())).rejects.toMatchObject({ reason: "incomplete" });
  });

  it("reports server-omitted entries without synthesizing their status", async () => {
    vi.mocked(listManagedAgents).mockResolvedValue(dashboardCollection([dashboardAgent("one")], null, 2));
    expect(await readAgentDashboard(signal())).toMatchObject({ omitted: true, items: [{ agent_id: "one" }] });
  });

  it("bounds large reads without returning partial counts as a complete snapshot", async () => {
    vi.mocked(listManagedAgents).mockImplementation(async (_view, params) => dashboardCollection([dashboardAgent(`agent_${params?.cursor ?? "first"}`)], `${params?.cursor ?? ""}x`, 2500));
    await expect(readAgentDashboard(signal())).rejects.toMatchObject({ reason: "incomplete" });
    expect(listManagedAgents).toHaveBeenCalledTimes(20);
  });

  it("stops reading when identity changes abort the request", async () => {
    const controller = new AbortController();
    vi.mocked(listManagedAgents).mockImplementation(async () => { controller.abort(); return dashboardCollection([], "next", 1); });
    await expect(readAgentDashboard(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(listManagedAgents).toHaveBeenCalledTimes(1);
  });

  it("does not turn personal assignment or runtime availability into team health", () => {
    const item = dashboardAgent("one", { assignment: { status: "personal_disabled", source: null, row_version: null }, availability: { discoverable: true, hireable: true, runnable: false, reasons: ["not_hired"] } });
    expect(agentListingState(item)).toBe("pass");
    expect(agentDashboardAttention(item)).toBeNull();
    expect(agentListingState({ ...item, listing_safety: undefined })).toBe("unknown");
    expect(agentDashboardAttention({ ...item, team_default: { enabled: true, policy_version: 1, row_version: 1 }, actions: { ...item.actions, view_detail: false } })).toBe("default_access");
  });

  it("preserves a fixed Dashboard return destination", () => {
    const href = agentDashboardDetailHref("agent/one");
    expect(href).toContain("id=agent%2Fone&return_page=dashboard");
    expect(agentDashboardReturnHref(href.split("?")[1])).toBe("/agents/dashboard/");
    expect(agentDashboardReturnHref("return_page=https://untrusted.invalid")).toBeNull();
  });
});
