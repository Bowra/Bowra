"use client";

import { useEffect, useId, useState, type MouseEvent } from "react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { ChartNoAxesColumnIncreasing, ChevronRight, TrendingUp } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { Button } from "@/components/ui/button";
import { FilterMenuButton } from "@/components/ui/filter-menu-button";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { useAuthSession } from "@/lib/auth/auth-session-context";
import { appPath } from "@/lib/base-path";
import { AGENT_CHART_DOMAINS, agentChartDetailHref, agentChartFilters, agentChartsHref, type AgentChartView } from "@/lib/agent-charts-navigation";
import { listAgentCatalog, type AgentCatalogCollection, type AgentCatalogItem } from "@/lib/agents-api";
import { listAgentRankings, type AgentRankingCollection, type AgentRankingRow } from "@/lib/agent-rankings-api";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";
import { useAgentListingText } from "./agent-listing-text";
import { AgentPageHeader } from "./agent-page-frame";
import { AgentMarksContext, distinctAgentMarks, useAgentMark } from "./agent-mark";

type CatalogState = { key: string; result: AgentCatalogCollection | null; error: boolean };
type RankingState = { key: string; result: AgentRankingCollection | null; error: boolean; loaded: boolean };

function ChartCard({ item, row, view, domain, onNavigate }: {
  item: AgentCatalogItem;
  row?: AgentRankingRow;
  view: AgentChartView;
  domain: string;
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, href: string) => void;
}) {
  const t = useTranslations("agents_marketplace");
  const format = useFormatter();
  const { displayName, summary } = useAgentListingText();
  const name = displayName(item);
  const mark = useAgentMark(name);
  const href = agentChartDetailHref(item.agent_id, view, domain);
  const updatedAt = item.updated_label?.at ? new Date(item.updated_label.at) : null;
  return <li className="ac-card">
    {row ? <span className="ac-rank" aria-label={t("charts_rank", { rank: row.display_rank })}>{row.display_rank}</span> : null}
    <a className="ac-card-main" href={appPath(href)} onClick={event => onNavigate(event, href)} aria-label={t("view_agent", { name })}>
      <div className="ac-artwork" aria-hidden="true">{mark}</div>
      <div className="ac-card-copy"><h2>{name}</h2><p className="ac-provider">{item.source.kind === "system" ? t("by_bowhead") : t("by_creator", { name: item.source.owner_label ?? t("team_member") })}</p><p className="ac-description">{summary(item)}</p></div>
    </a>
    <div className="ac-signal">
      {row ? view === "trending" ? <><p className="ac-growth"><LucideGlyph icon={TrendingUp} size={16} aria-hidden="true" />{t("charts_growth", { count: format.number(row.active_members_7d! - row.active_members_previous_7d!) })}</p><span>{t("charts_trending_period")}</span></>
        : <><p>{t("charts_members", { count: format.number(row.active_members_28d!) })}</p><span>{t("charts_popular_period")}</span></>
        : updatedAt && Number.isFinite(updatedAt.getTime()) ? <><p>{t("charts_saved", { date: format.dateTime(updatedAt, { year: "numeric", month: "short", day: "numeric" }) })}</p><span>{t("charts_saved_context")}</span></> : <p>{t("charts_saved_context")}</p>}
    </div>
    <a className="settings-button settings-button--secondary settings-button--sm ac-details" href={appPath(href)} onClick={event => onNavigate(event, href)} aria-label={t("view_agent", { name })}>{t("details")}</a>
  </li>;
}

export function AgentCharts() {
  const t = useTranslations("agents_marketplace");
  const pageT = useTranslations("agents_page");
  const locale = useLocale();
  const methodId = useId();
  const { displayName: listingName } = useAgentListingText();
  const [methodOpen, setMethodOpen] = useState(false);
  const format = useFormatter();
  const router = useAppRouter();
  const session = useAuthSession();
  const [filters, setFilters] = useState<{ view: AgentChartView; domain: string }>({ view: "popular", domain: "all" });
  const [initialized, setInitialized] = useState(false);
  const [catalog, setCatalog] = useState<CatalogState | null>(null);
  const [ranking, setRanking] = useState<RankingState | null>(null);
  const [retry, setRetry] = useState(0);
  const identity = `${session.me?.id}:${session.me?.team_id}:${session.me?.auth_session_generation}:${locale}`;
  const catalogKey = `${identity}:${filters.domain}:${retry}`;
  const rankingKey = `${catalogKey}:${filters.view}`;

  useEffect(() => {
    const read = () => setFilters(agentChartFilters(window.location.search));
    read();
    setInitialized(true);
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  useEffect(() => {
    if (!initialized || session.status !== "authenticated") return;
    const controller = new AbortController();
    void listAgentCatalog({ domain: filters.domain, limit: 1, signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setCatalog({ key: catalogKey, result, error: false });
    }).catch(() => {
      if (!controller.signal.aborted) setCatalog({ key: catalogKey, result: null, error: true });
    });
    return () => controller.abort();
  }, [initialized, session.status, catalogKey, filters.domain]);

  useEffect(() => {
    if (!initialized || session.status !== "authenticated" || filters.view === "updated") return;
    const controller = new AbortController();
    void listAgentRankings({ rankingKind: filters.view, domain: filters.domain, signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setRanking({ key: rankingKey, result, error: false, loaded: true });
    }).catch(() => {
      if (!controller.signal.aborted) setRanking({ key: rankingKey, result: null, error: true, loaded: true });
    });
    return () => controller.abort();
  }, [initialized, session.status, rankingKey, filters.view, filters.domain]);

  const visibleCatalog = catalog?.key === catalogKey && session.status === "authenticated" ? catalog : null;
  const visibleRanking = ranking?.key === rankingKey && session.status === "authenticated" ? ranking : null;
  const recent = visibleCatalog?.result?.collections.find(collection => collection.id === "recently_updated");
  // The server collection is globally ordered before pagination; the first catalog page is not a chart.
  const updatedItems = (recent?.items ?? []).filter(item => item.availability.discoverable && item.actions.view_detail);
  const rows = visibleRanking?.result?.state === "ready" ? visibleRanking.result.rows : [];
  const loading = !initialized || (filters.view === "updated" ? !visibleCatalog : !visibleRanking?.loaded);
  const error = filters.view === "updated" ? visibleCatalog?.error : visibleRanking?.error;
  const domains = visibleCatalog?.result?.facets?.domains?.map(entry => entry.id) ?? [];
  const selectedDomains = AGENT_CHART_DOMAINS.filter(domain => domains.includes(domain) || domain === filters.domain);
  const state = visibleRanking?.result?.state;
  const lowSample = state === "insufficient_sample" || state === "privacy_suppressed" || (state === "ready" && rows.length === 0);
  const collecting = state === "insufficient_sample" && visibleRanking?.result?.reason === "window_incomplete";
  const generatedAt = visibleRanking?.result?.generated_at;
  const availableAt = collecting && visibleRanking?.result?.available_at ? new Date(visibleRanking.result.available_at) : null;
  const collectingDate = availableAt && Number.isFinite(availableAt.getTime())
    ? format.dateTime(availableAt, { year: "numeric", month: "short", day: "numeric" })
    : null;

  function select(next: { view: AgentChartView; domain: string }) {
    setFilters(next);
    window.history.replaceState(null, "", appPath(agentChartsHref(next.view, next.domain)));
  }
  function navigate(event: MouseEvent<HTMLAnchorElement>, href: string) {
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    router.push(href);
  }
  // Distinct avatar marks across the ranked rows and recent updates shown on this page.
  const marks = distinctAgentMarks([...rows.map(row => row.item), ...updatedItems].map(item => listingName(item)));
  const card = (item: AgentCatalogItem, row?: AgentRankingRow) => <ChartCard key={item.agent_id} item={item} row={row} view={filters.view} domain={filters.domain} onNavigate={navigate} />;

  return <AgentMarksContext.Provider value={marks}><div className="ac-page">
    <AgentPageHeader page="charts" canCreate={Boolean(visibleCatalog?.result?.actions.create)} description={t("charts_subtitle")} />
    <div className="ac-controls">
      <SegmentedControl value={filters.view} onValueChange={view => select({ ...filters, view })} aria-label={t("charts_type")}><Segment value="popular">{t("charts_popular")}</Segment><Segment value="trending">{t("charts_trending")}</Segment><Segment value="updated">{t("charts_updated")}</Segment></SegmentedControl>
      <FilterMenuButton
        value={filters.domain}
        options={[{ value: "all", label: pageT("filter_all") }, ...selectedDomains.map(domain => ({ value: domain, label: t(`menu_taxonomy_domain_${domain}`) }))]}
        onChange={domain => select({ ...filters, domain })}
        format={value => pageT("filter_category", { value })}
        menuLabel={t("charts_category")}
        active={filters.domain !== "all"}
        testId="ac-category-filter"
      />
    </div>
    <div className="ac-context"><span>{t("charts_team_scope")} · {t(filters.view === "updated" ? "charts_updated_period" : filters.view === "trending" ? "charts_trending_period" : "charts_popular_period")}</span>{generatedAt && rows.length ? <span>{t("charts_generated", { date: format.dateTime(new Date(generatedAt), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) })}</span> : null}</div>
    {filters.view !== "updated" ? <div className="ac-method">
      <button type="button" className="ac-method-toggle" aria-expanded={methodOpen} aria-controls={methodId} onClick={() => setMethodOpen(open => !open)}>
        <LucideGlyph icon={ChevronRight} size={14} aria-hidden="true" className="ac-method-chevron" />
        <span>{t("charts_method")}</span>
      </button>
      <div id={methodId} className="ac-method-panel" hidden={!methodOpen}><p>{t("charts_method_popular")}</p><p>{t("charts_method_trending")}</p><p>{t("charts_method_exclusions")}</p><p>{t("charts_method_privacy")}</p></div>
    </div> : null}
    <section aria-label={t(`charts_${filters.view}`)} aria-busy={loading} className="ac-results">
      {loading ? <><p className="ac-status" role="status">{t("charts_loading")}</p><div className="ac-grid" aria-hidden="true">{[0,1,2,3,4,5].map(value => <div className="ac-skeleton" key={value} />)}</div></>
        : error ? <div className="ac-empty" role="alert"><h2>{t("charts_error_title")}</h2><p>{t("charts_error_body")}</p><Button onClick={() => setRetry(value => value + 1)}>{t("retry")}</Button></div>
        : filters.view === "updated" ? updatedItems.length ? <ul className="ac-grid">{updatedItems.map(item => card(item))}</ul> : <div className="ac-empty"><h2>{t("charts_updated_empty")}</h2><p>{t("charts_updated_empty_body")}</p><a className="settings-button settings-button--secondary settings-button--sm" href={appPath("/agents/marketplace/")} onClick={event => navigate(event, "/agents/marketplace/")}>{t("charts_browse")}</a></div>
        : rows.length ? <ol className="ac-grid">{rows.map(row => card(row.item, row))}</ol>
        : <div className="ac-empty" role="status"><LucideGlyph icon={ChartNoAxesColumnIncreasing} size={32} aria-hidden="true" /><h2>{t(collecting ? "charts_collecting_title" : lowSample ? "charts_insufficient_title" : state === "stale" ? "charts_stale_title" : "charts_unavailable_title")}</h2><p>{t(collecting ? "charts_collecting_body" : lowSample ? "charts_insufficient_body" : state === "stale" ? "charts_stale_body" : "charts_unavailable_body")}</p>{collectingDate ? <p>{t("charts_collecting_date", { date: collectingDate })}</p> : null}<div className="ac-empty-actions"><Button onClick={() => select({ ...filters, view: "updated" })}>{t("charts_view_updated")}</Button><a href={appPath("/agents/marketplace/")} onClick={event => navigate(event, "/agents/marketplace/")}>{t("charts_browse")}</a></div></div>}
    </section>
    {filters.view !== "updated" && !loading && updatedItems.length ? <section className="ac-recent"><header><h2>{t("charts_updated")}</h2><Button variant="ghost" onClick={() => select({ ...filters, view: "updated" })}>{t("charts_view_all")}</Button></header><ul className="ac-grid">{updatedItems.slice(0,4).map(item => card(item))}</ul></section> : null}
    {filters.view !== "updated" && visibleCatalog?.error ? <div className="ac-catalog-error" role="status"><span>{t("charts_recent_error")}</span><Button variant="ghost" onClick={() => setRetry(value => value + 1)}>{t("retry")}</Button></div> : null}
  </div></AgentMarksContext.Provider>;
}
