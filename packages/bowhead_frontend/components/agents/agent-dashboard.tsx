"use client";

import { useEffect, useRef, useState } from "react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { ArrowUpRight } from "lucide-react";

import { CapabilityLink } from "@/components/capabilities/capability-link";
import { LucideGlyph } from "@/components/icon-refresh";
import { Button } from "@/components/ui/button";
import { StatTiles } from "@/components/ui/stat-tiles";
import { useAuthSession } from "@/lib/auth/auth-session-context";
import { AGENT_CHART_DOMAINS } from "@/lib/agent-charts-navigation";
import { AgentDashboardReadError, agentDashboardAttention, agentDashboardDetailHref, agentListingState, readAgentDashboard } from "@/lib/agent-dashboard";
import { getAgentReleaseInboxCount, type AgentCatalogItem } from "@/lib/agents-api";
import { teamRoleForUser } from "@/lib/settings-navigation";
import { AgentBreadcrumb } from "./agent-breadcrumb";
import { useAgentListingText } from "./agent-listing-text";
import { AgentDashboardChart } from "./agent-dashboard-chart";

type DirectoryState = { key: string; items: AgentCatalogItem[]; omitted: boolean; at: Date; error?: "failed" | "forbidden" | "incomplete" };
type ReviewState = { key: string; count: number | null };
const TEAM_MANAGEMENT = "/agents/manage/governance/";

export function AgentDashboard() {
  const session = useAuthSession();
  const t = useTranslations("agents_marketplace");
  const locale = useLocale();
  const format = useFormatter();
  const { displayName } = useAgentListingText();
  const role = teamRoleForUser(session.me);
  const allowed = session.status === "authenticated" && Boolean(session.me?.capabilities?.subagent_management_v2)
    && (role === "owner" || role === "team_admin");
  const [directory, setDirectory] = useState<DirectoryState | null>(null);
  const [reviews, setReviews] = useState<ReviewState | null>(null);
  const [revision, setRevision] = useState(0);
  const lastRead = useRef(0);
  const key = `${session.me?.id}:${session.me?.team_id}:${session.me?.auth_session_generation}:${role}:${locale}:${revision}`;

  useEffect(() => {
    if (!allowed) return;
    const controller = new AbortController();
    lastRead.current = Date.now();
    void readAgentDashboard(controller.signal).then(result => {
      if (!controller.signal.aborted) setDirectory({ key, ...result, at: new Date() });
    }).catch(cause => {
      if (!controller.signal.aborted) setDirectory({ key, items: [], omitted: false, at: new Date(), error: cause instanceof AgentDashboardReadError ? cause.reason : "failed" });
    });
    // The approval queue remains independent of directory availability.
    void getAgentReleaseInboxCount(controller.signal).then(result => {
      if (!controller.signal.aborted) setReviews({ key, count: result.kind === "agent_release_inbox_count" && result.schema_version === 1 && Number.isSafeInteger(result.count) && result.count >= 0 ? result.count : null });
    }).catch(() => { if (!controller.signal.aborted) setReviews({ key, count: null }); });
    return () => controller.abort();
  }, [allowed, key]);

  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible" && Date.now() - lastRead.current >= 60_000) setRevision(value => value + 1);
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => { window.removeEventListener("focus", refresh); document.removeEventListener("visibilitychange", refresh); };
  }, []);

  const current = allowed && directory?.key === key ? directory : null;
  const queue = allowed && reviews?.key === key ? reviews : null;
  const items = current?.items ?? [];
  const ready = current && !current.error;
  const counts = { pass: 0, blocked: 0, unknown: 0 };
  items.forEach(item => { counts[agentListingState(item)] += 1; });
  const defaults = items.filter(item => item.team_default.enabled).length;
  const attention = items.filter(item => agentDashboardAttention(item));
  const domains: Array<{ id: string; label: string; chartLabel?: string; count: number }> = AGENT_CHART_DOMAINS.map(domain => ({ id: domain, label: t(`taxonomy_domain_${domain}`), chartLabel: domain === "people" || domain === "it_data" ? t(`dashboard_domain_${domain}`) : undefined, count: items.filter(item => item.facets.domains.includes(domain)).length })).filter(row => row.count > 0).sort((left, right) => right.count - left.count);
  const unclassified = items.filter(item => !AGENT_CHART_DOMAINS.some(domain => item.facets.domains.includes(domain))).length;
  if (unclassified) domains.push({ id: "unclassified", label: t("dashboard_unclassified"), count: unclassified });
  const formatCount = (value: number) => format.number(value);

  if (!allowed) return null;
  return <div className="ad-page">
    <nav className="ad-navigation" aria-label={t("section_navigation")}>
      <AgentBreadcrumb href="/agents/marketplace/" label={t("charts_parent")} />
    </nav>
    <header className="ad-header"><div><span className="ad-eyebrow">{t("dashboard_eyebrow")}</span><h1>{t("dashboard_title")}</h1><p>{t("dashboard_subtitle")}</p></div><Button variant="secondary" onClick={() => setRevision(value => value + 1)} disabled={!current || !queue}>{t("dashboard_refresh")}</Button></header>
    <div className="ad-context"><p>{t("dashboard_scope")}</p>{ready ? <span>{t("dashboard_read_at", { date: format.dateTime(current.at, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) })}</span> : null}</div>
    {current?.error === "forbidden" ? <section className="ad-empty" role="alert"><h2>{t("dashboard_forbidden")}</h2><p>{t("dashboard_forbidden_body")}</p><CapabilityLink href="/agents/marketplace/">{t("charts_browse")}</CapabilityLink></section> : <>
      {/* Chart Embed Spec D6/D7: one hairline stat band. The listing breakdown (pass / no evidence) is the blocked tile's
          secondary line instead of a three-bar chart whose middle bar repeated this tile. */}
      <StatTiles
        className="ad-metrics"
        busy={!current}
        tiles={[
          { id: "agents", label: t("dashboard_agents"), value: ready ? items.length : null, format: formatCount, secondary: t("dashboard_agents_note"), secondaryKind: "note" },
          { id: "defaults", label: t("dashboard_defaults"), value: ready ? defaults : null, format: formatCount, secondary: <CapabilityLink href={TEAM_MANAGEMENT}>{t("dashboard_manage_defaults")}<LucideGlyph icon={ArrowUpRight} size={14} aria-hidden="true" /></CapabilityLink>, secondaryKind: "link" },
          { id: "blocked", label: t("dashboard_blocked"), value: ready ? counts.blocked : null, format: formatCount, secondary: ready ? t("dashboard_listing_breakdown", { pass: format.number(counts.pass), unknown: format.number(counts.unknown) }) : null, secondaryKind: "note" },
          { id: "reviews", label: t("dashboard_reviews"), value: queue?.count ?? null, format: formatCount, secondary: <CapabilityLink href={TEAM_MANAGEMENT}>{t("dashboard_open_reviews")}<LucideGlyph icon={ArrowUpRight} size={14} aria-hidden="true" /></CapabilityLink>, secondaryKind: "link" },
        ]}
      />
      <p className="ad-band-note">{t("dashboard_listing_note")}</p>
      {!queue && !current ? <p className="ad-status" role="status">{t("dashboard_loading")}</p> : null}
      {queue && queue.count === null ? <p className="ad-status" role="status">{t("dashboard_reviews_error")}</p> : null}
      {current?.error ? <section className="ad-empty" role="alert"><h2>{t("dashboard_error")}</h2><p>{t(current.error === "incomplete" ? "dashboard_incomplete" : "dashboard_error_body")}</p><Button onClick={() => setRevision(value => value + 1)}>{t("retry")}</Button></section> : !current ? <div className="ad-loading" aria-busy="true" aria-label={t("dashboard_loading")} /> : <>
        {current.omitted ? <p className="ad-status" role="status">{t("dashboard_omitted")}</p> : null}
        {!items.length ? <section className="ad-empty"><h2>{t("dashboard_empty")}</h2><p>{t("dashboard_empty_body")}</p><CapabilityLink href={TEAM_MANAGEMENT}>{t("dashboard_manage")}</CapabilityLink></section> : <>
          {/* One chart left, so domain coverage and the attention list sit side by side (single column below 1100). */}
          <div className="ad-panels">
            <section className="ad-panel" aria-labelledby="ad-domains-title"><header className="ad-panel-header"><div><h2 id="ad-domains-title">{t("dashboard_domains")}</h2><p>{t("dashboard_domains_note")}</p></div></header><AgentDashboardChart title={t("dashboard_domains")} titleId="ad-domains-title" rows={domains} /></section>
            <section className="ad-panel ad-attention" aria-labelledby="ad-attention-title"><header className="ad-panel-header"><div><h2 id="ad-attention-title">{t("dashboard_attention")}</h2><p>{t("dashboard_attention_note")}</p></div><CapabilityLink href={TEAM_MANAGEMENT}>{t("dashboard_manage")}<LucideGlyph icon={ArrowUpRight} size={14} aria-hidden="true" /></CapabilityLink></header>
              {attention.length ? <ul className="ad-attention-list">{attention.slice(0, 8).map(item => <li key={item.agent_id}><div><strong>{displayName(item)}</strong><span>{t(agentDashboardAttention(item) === "blocked" ? "dashboard_attention_blocked" : "dashboard_attention_default_access")}</span></div><CapabilityLink href={item.actions.view_detail ? agentDashboardDetailHref(item.agent_id) : TEAM_MANAGEMENT}>{t(item.actions.view_detail ? "details" : "dashboard_manage")}</CapabilityLink></li>)}</ul> : <p className="ad-clear">{t("dashboard_clear")}</p>}
              {attention.length > 8 ? <p className="ad-note">{t("dashboard_more", { count: format.number(attention.length - 8) })}</p> : null}
            </section>
          </div>
        </>}
      </>}
      <p className="ad-footer">{t("dashboard_usage_note")}</p>
    </>}
  </div>;
}
