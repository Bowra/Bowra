import type { AgentManagementView } from "./agents-api";

/** All Agent pages, including built-in configuration, share the primary entry. */
export function isAgentNavigationRoute(pathname: string, _search = "", prefix = ""): boolean {
  const base = prefix.replace(/\/+$/, "");
  const path = base && pathname.startsWith(`${base}/`) ? pathname.slice(base.length) : pathname;
  const [first, second, third] = path.split(/[?#]/, 1)[0].split("/").filter(Boolean);
  if (first === "agents") return true;
  if (first === "settings") return second === "subagents";
  return (first === "team" || first === "space") && second === "settings"
    && third === "subagents";
}

export const AGENT_MANAGEMENT_VIEWS: AgentManagementView[] = ["hired", "created", "shared", "archived", "team"];

export function agentManagementView(value: string | null): AgentManagementView | null {
  return AGENT_MANAGEMENT_VIEWS.includes(value as AgentManagementView) ? value as AgentManagementView : null;
}

export function agentManagementHref(view: AgentManagementView = "hired"): string {
  return `/agents/manage/${view === "team" ? "governance" : view}/`;
}

export function agentManagementViewForPath(pathname: string): AgentManagementView {
  const segment = pathname.split("/").filter(Boolean).at(-1);
  return segment === "governance" ? "team" : agentManagementView(segment ?? null) ?? "hired";
}

export function withAgentReturnView(href: string, view: AgentManagementView): string {
  const url = new URL(href, "https://navigation.invalid");
  url.searchParams.set("return_view", view);
  return `${url.pathname}${url.search}${url.hash}`;
}

export function agentManagementReturnHref(search: string, fallback: AgentManagementView = "created"): string {
  const view = agentManagementView(new URLSearchParams(search).get("return_view")) ?? fallback;
  return agentManagementHref(view);
}
