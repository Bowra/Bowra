export const AGENT_CHART_VIEWS = ["popular", "trending", "updated"] as const;
export type AgentChartView = (typeof AGENT_CHART_VIEWS)[number];

export const AGENT_CHART_DOMAINS = ["finance", "marketing", "sales", "operations", "people", "it_data", "general"] as const;

export function agentChartFilters(search: string): { view: AgentChartView; domain: string } {
  const query = new URLSearchParams(search);
  const value = query.get("chart");
  return {
    view: AGENT_CHART_VIEWS.includes(value as AgentChartView) ? value as AgentChartView : "popular",
    domain: AGENT_CHART_DOMAINS.includes(query.get("domain") as typeof AGENT_CHART_DOMAINS[number]) ? query.get("domain")! : "all",
  };
}

export function agentChartsHref(view: AgentChartView, domain: string): string {
  const query = new URLSearchParams({ chart: view });
  if (AGENT_CHART_DOMAINS.includes(domain as typeof AGENT_CHART_DOMAINS[number])) query.set("domain", domain);
  return `/agents/charts/?${query}`;
}

export function agentChartDetailHref(agentId: string, view: AgentChartView, domain: string): string {
  const query = new URLSearchParams({ id: agentId, return_page: "charts", chart: view });
  if (AGENT_CHART_DOMAINS.includes(domain as typeof AGENT_CHART_DOMAINS[number])) query.set("domain", domain);
  return `/agents/detail/?${query}`;
}

export function agentChartsReturnHref(search: string): string | null {
  if (new URLSearchParams(search).get("return_page") !== "charts") return null;
  const { view, domain } = agentChartFilters(search);
  return agentChartsHref(view, domain);
}
