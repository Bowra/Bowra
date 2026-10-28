"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { DialogShell } from "@/components/ui/dialog";
import { FilterMenuButton } from "@/components/ui/filter-menu-button";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import {
  hireAgent,
  listAgentCatalog,
  type AgentAssignmentMutation,
  type AgentCapabilityFacet,
  type AgentCatalogItem,
  type AgentTaxonomyFacet,
  type AgentTeamTagFacet,
} from "@/lib/agents-api";
import { appPath } from "@/lib/base-path";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";
import { useAgentListingText } from "./agent-listing-text";
import { agentArtworkStyle } from "./agent-artwork";
import { AgentMarksContext, distinctAgentMarks, useAgentMark } from "./agent-mark";
import { AgentPageHeader } from "./agent-page-frame";
import { CapabilityChips } from "./capability-chips";
import { LimitedNote, ReasonChip, ReasonLine, useAvailabilityReason, useLimitedAvailability } from "./availability-reason";
import { useCapabilityLabel } from "./capability-label";
import {
  ListFilter as LucideListFilter,
  Search as LucideSearch,
  X as LucideX,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { CharIcon } from "@/components/char-icon";

type SourceFilter = "all" | "team" | "system";
type CatalogFilters = {
  query: string;
  source: SourceFilter;
  capability: string;
  domain: string;
  job: string;
  tag: string;
};

function catalogFilterKey(filters: CatalogFilters): string {
  return JSON.stringify([
    filters.query.trim(),
    filters.source,
    filters.capability,
    filters.domain,
    filters.job,
    filters.tag,
  ]);
}

function legacyCapabilityFacets(items: AgentCatalogItem[]): AgentCapabilityFacet[] {
  const values = new Map<string, AgentCapabilityFacet>();
  items.forEach((item) => item.capabilities.forEach((entry) => {
    const current = values.get(entry.id);
    values.set(entry.id, { ...entry, count: (current?.count ?? 0) + 1 });
  }));
  return [...values.values()].sort((left, right) => left.id.localeCompare(right.id));
}

function mergeCapabilityFacets(
  current: AgentCapabilityFacet[],
  incoming: AgentCapabilityFacet[],
): AgentCapabilityFacet[] {
  const values = new Map(current.map((entry) => [entry.id, entry]));
  incoming.forEach((entry) => {
    const previous = values.get(entry.id);
    values.set(entry.id, { ...entry, count: entry.count + (previous?.count ?? 0) });
  });
  return [...values.values()].sort((left, right) => left.id.localeCompare(right.id));
}

function SearchIcon() {
  return (
    <LucideGlyph icon={LucideSearch} size={21} aria-hidden="true" />
  );
}

function FilterIcon() {
  return (
    <LucideGlyph icon={LucideListFilter} size={16} aria-hidden="true" />
  );
}

function CloseIcon() {
  return (
    <LucideGlyph icon={LucideX} size={18} aria-hidden="true" />
  );
}

type CapabilityFilterOption = {
  id: string;
  label: string;
  count: number;
};

function AgentFilterSheet({
  capabilities,
  domains,
  draftCapability,
  draftDomain,
  draftJob,
  draftSource,
  draftTag,
  jobs,
  onApply,
  onClear,
  onClose,
  onDraftCapabilityChange,
  onDraftDomainChange,
  onDraftJobChange,
  onDraftSourceChange,
  onDraftTagChange,
  teamTags,
}: {
  capabilities: CapabilityFilterOption[];
  domains: AgentTaxonomyFacet[];
  draftCapability: string;
  draftDomain: string;
  draftJob: string;
  draftSource: SourceFilter;
  draftTag: string;
  jobs: AgentTaxonomyFacet[];
  onApply: () => void;
  onClear: () => void;
  onClose: () => void;
  onDraftCapabilityChange: (value: string) => void;
  onDraftDomainChange: (value: string) => void;
  onDraftJobChange: (value: string) => void;
  onDraftSourceChange: (value: SourceFilter) => void;
  onDraftTagChange: (value: string) => void;
  teamTags: AgentTeamTagFacet[];
}) {
  const t = useTranslations("agents_marketplace");

  return (
    <DialogShell
      presentation="center"
      onClose={onClose}
      closeOnBackdrop={false}
      labelledBy="agent-filter-title"
      backdropClassName="am-filter-sheet-backdrop"
      panelClassName="am-filter-sheet"
    >
      <header>
        <div>
          <span>{t("browse")}</span>
          <h2 id="agent-filter-title">{t("filter_title")}</h2>
        </div>
        <button data-dialog-initial-focus="" type="button" className="am-filter-close" onClick={onClose} aria-label={t("close_filters")}>
          <CloseIcon />
        </button>
      </header>
      <div className="am-filter-sheet-body">
        <fieldset>
          <legend>{t("filter_source_title")}</legend>
          <SegmentedControl appearance="none" orientation="both" className="am-filter-options" aria-label={t("filter_source_title")} value={draftSource} onValueChange={onDraftSourceChange}>
            {(["all", "team", "system"] as SourceFilter[]).map((value) => (
              <Segment key={value} value={value}>
                {t(`source_${value}`)}
              </Segment>
            ))}
          </SegmentedControl>
        </fieldset>
        <fieldset>
          <legend>{t("filter_capability_title")}</legend>
          <SegmentedControl appearance="none" orientation="both" className="am-filter-options am-filter-options-capabilities" aria-label={t("filter_capability_title")} value={draftCapability} onValueChange={onDraftCapabilityChange}>
            <Segment value="all">
              {t("all_capabilities")}
            </Segment>
            {capabilities.map((entry) => (
              <Segment key={entry.id} value={entry.id}>
                <span>{entry.label}</span><small>{entry.count}</small>
              </Segment>
            ))}
          </SegmentedControl>
        </fieldset>
        {domains.length ? <fieldset>
          <legend>{t("filter_domain_title")}</legend>
          <SegmentedControl appearance="none" orientation="both" className="am-filter-options" aria-label={t("filter_domain_title")} value={draftDomain} onValueChange={onDraftDomainChange}>
            <Segment value="all">{t("filter_all_domains")}</Segment>
            {domains.map((entry) => <Segment key={entry.id} value={entry.id}><span>{t(`taxonomy_domain_${entry.id}` as never)}</span><small>{entry.count}</small></Segment>)}
          </SegmentedControl>
        </fieldset> : null}
        {jobs.length ? <fieldset>
          <legend>{t("filter_job_title")}</legend>
          <SegmentedControl appearance="none" orientation="both" className="am-filter-options" aria-label={t("filter_job_title")} value={draftJob} onValueChange={onDraftJobChange}>
            <Segment value="all">{t("filter_all_jobs")}</Segment>
            {jobs.map((entry) => <Segment key={entry.id} value={entry.id}><span>{t(`taxonomy_job_${entry.id}` as never)}</span><small>{entry.count}</small></Segment>)}
          </SegmentedControl>
        </fieldset> : null}
        {teamTags.length ? <fieldset>
          <legend>{t("filter_team_tag_title")}</legend>
          <SegmentedControl appearance="none" orientation="both" className="am-filter-options" aria-label={t("filter_team_tag_title")} value={draftTag} onValueChange={onDraftTagChange}>
            <Segment value="all">{t("filter_all_team_tags")}</Segment>
            {teamTags.map((entry) => <Segment key={entry.id} value={entry.id}><span>{entry.label}</span><small>{entry.count}</small></Segment>)}
          </SegmentedControl>
        </fieldset> : null}
      </div>
      <footer>
        <button type="button" className="am-filter-clear" onClick={onClear}>{t("clear_filters")}</button>
        <button type="button" className="am-filter-apply" onClick={onApply}>{t("apply_filters")}</button>
      </footer>
    </DialogShell>
  );
}

function AgentArtwork({ item, large = false }: { item: AgentCatalogItem; large?: boolean }) {
  const { displayName } = useAgentListingText();
  const name = displayName(item);
  const initials = useAgentMark(name);
  return (
    <div className={`am-artwork ${large ? "am-artwork-large" : ""}`} style={agentArtworkStyle(`${item.agent_id}:${name}`)} aria-hidden="true">
      <span>{initials}</span>
      <i />
      <b />
    </div>
  );
}

function StatusPill({ item }: { item: AgentCatalogItem }) {
  const t = useTranslations("agents_marketplace");
  const reason = useAvailabilityReason(item);
  const status = item.assignment.status;
  // A hire that cannot run says so; not hired / turned off keep their own labels and buttons.
  // Not on it yet: the chip only where there is no Hire button next to it to contradict.
  const onIt = status === "active" || status === "access_revoked";
  if (reason && reason.code !== "not_hired" && (onIt || ((status === "not_hired" || status === "removed") && !item.actions.hire))) return <ReasonChip view={reason} />;
  if (status === "system_required") return <span className="am-status am-status-included">{t("included")}</span>;
  if (status === "active") return <span className="am-status am-status-active">{t("hired")}</span>;
  if (status === "personal_disabled") return <span className="am-status">{t("paused")}</span>;
  if (status === "access_revoked") return <span className="am-status am-status-warning">{t("access_changed")}</span>;
  return null;
}

function AgentCard({
  item,
  onHire,
  onNavigate,
  pending,
}: {
  item: AgentCatalogItem;
  onHire: (item: AgentCatalogItem) => void;
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
  pending: boolean;
}) {
  const t = useTranslations("agents_marketplace");
  const capabilityLabel = useCapabilityLabel();
  const { displayName, summary } = useAgentListingText();
  const name = displayName(item);
  const reason = useAvailabilityReason(item);
  // "Hire to use" would sit on every card in the store; the hire button already says it.
  const showReason = reason !== null && reason.code !== "not_hired";
  const limited = useLimitedAvailability(item);
  return (
    <article className="am-card">
      <a
        className="am-card-main"
        href={appPath(`/agents/detail/?id=${encodeURIComponent(item.agent_id)}`)}
        onClick={(event) => onNavigate(event, `/agents/detail/?id=${encodeURIComponent(item.agent_id)}`)}
        aria-label={t("view_agent", { name })}
      >
        <AgentArtwork item={item} />
        <div className="am-card-copy">
          <div className="am-card-title-row">
            <h3>{name}</h3>
            <StatusPill item={item} />
          </div>
          <p className="am-developer">
            {item.source.kind === "system" ? t("by_bowhead") : t("by_creator", { name: item.source.owner_label ?? t("team_member") })}
            {item.source.kind === "team" ? <span> · {t("creator_listing_original")}</span> : null}
          </p>
          <p className="am-summary">{summary(item)}</p>
        </div>
      </a>
      <CapabilityChips
        label={t("capabilities")}
        chips={item.capabilities.map((capability) => ({
          id: capability.id,
          label: capabilityLabel(capability),
          note: capability.verification_status === "derived_unverified" ? <small>{t("capability_pending_review")}</small> : undefined,
        }))}
      />
      {showReason ? <ReasonLine view={reason} /> : limited ? <LimitedNote view={limited} /> : null}
      {item.facets.team_tags?.length ? <div className="am-team-tag-row" aria-label={t("taxonomy_team_tags")}>{item.facets.team_tags.slice(0, 3).map((tagValue) => <span key={tagValue.id}>{tagValue.label}</span>)}</div> : null}
      <footer className="am-card-footer">
        <span className={`am-risk am-risk-${item.risk.level}`}>{t("risk_label", { level: t(`risk_${item.risk.level}` as never) })}</span>
        {item.actions.hire ? (
          <Button variant="primary" size="sm" className="am-hire-button" disabled={pending} onClick={() => onHire(item)}>
            {pending ? t("hiring") : t("hire")}
          </Button>
        ) : (
          <a className="settings-button settings-button--secondary settings-button--sm am-detail-link" href={appPath(`/agents/detail/?id=${encodeURIComponent(item.agent_id)}`)} onClick={(event) => onNavigate(event, `/agents/detail/?id=${encodeURIComponent(item.agent_id)}`)}>
            {t("view_short")}
          </a>
        )}
      </footer>
    </article>
  );
}

function MarketplaceSkeleton() {
  return (
    <div className="am-grid" aria-hidden="true">
      {[0, 1, 2, 3, 4, 5].map((value) => <div className="am-card am-skeleton" key={value} />)}
    </div>
  );
}

export function AgentMarketplace() {
  const router = useAppRouter();
  const t = useTranslations("agents_marketplace");
  const pageT = useTranslations("agents_page");
  const capabilityLabel = useCapabilityLabel();
  const { displayName, summary } = useAgentListingText();
  const [items, setItems] = useState<AgentCatalogItem[]>([]);
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<SourceFilter>("all");
  const [capability, setCapability] = useState("all");
  const [domain, setDomain] = useState("all");
  const [job, setJob] = useState("all");
  const [tag, setTag] = useState("all");
  const [loading, setLoading] = useState(true);
  const [validating, setValidating] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [filtersReady, setFiltersReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [canCreate, setCanCreate] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [totalCount, setTotalCount] = useState(0);
  const [capabilityFacets, setCapabilityFacets] = useState<AgentCapabilityFacet[]>([]);
  const [domainFacets, setDomainFacets] = useState<AgentTaxonomyFacet[]>([]);
  const [jobFacets, setJobFacets] = useState<AgentTaxonomyFacet[]>([]);
  const [teamTagFacets, setTeamTagFacets] = useState<AgentTeamTagFacet[]>([]);
  const [featured, setFeatured] = useState<AgentCatalogItem[]>([]);
  const [hireSuccess, setHireSuccess] = useState<{ name: string } | null>(null);
  const [filterSheetOpen, setFilterSheetOpen] = useState(false);
  const [draftSource, setDraftSource] = useState<SourceFilter>("all");
  const [draftCapability, setDraftCapability] = useState("all");
  const [draftDomain, setDraftDomain] = useState("all");
  const [draftJob, setDraftJob] = useState("all");
  const [draftTag, setDraftTag] = useState("all");
  const searchRef = useRef<HTMLInputElement>(null);
  const filterTriggerRef = useRef<HTMLButtonElement>(null);
  const loadSequence = useRef(0);
  const catalogController = useRef<AbortController | null>(null);
  const loadedRef = useRef(false);
  // The router object can change identity while a navigation starts; read it through a ref so that does not
  // re-run the filter sync (which would refetch and could replace the URL over the new navigation).
  const routerRef = useRef(router);
  routerRef.current = router;
  const filtersRef = useRef<CatalogFilters>({ query, source, capability, domain, job, tag });
  filtersRef.current = { query, source, capability, domain, job, tag };

  const refresh = useCallback(async (options: {
    append?: boolean;
    cursor?: string | null;
    mutationSucceeded?: boolean;
    preserve?: boolean;
  } = {}) => {
    catalogController.current?.abort();
    const controller = new AbortController();
    catalogController.current = controller;
    const requestedFilters = { ...filtersRef.current };
    const requestedFilterKey = catalogFilterKey(requestedFilters);
    const sequence = loadSequence.current + 1;
    loadSequence.current = sequence;
    if (options.append) setLoadingMore(true);
    else if (options.preserve && loadedRef.current) setValidating(true);
    else {
      setLoading(true);
      setLoaded(false);
      setItems([]);
      setNextCursor(null);
      setTotalCount(0);
      setCapabilityFacets([]);
      setDomainFacets([]);
      setJobFacets([]);
      setTeamTagFacets([]);
      setFeatured([]);
    }
    try {
      const result = await listAgentCatalog({
        q: requestedFilters.query.trim() || undefined,
        source: requestedFilters.source,
        capability: requestedFilters.capability,
        domain: requestedFilters.domain,
        job: requestedFilters.job,
        tag: requestedFilters.tag,
        cursor: options.cursor ?? undefined,
        limit: 48,
        signal: controller.signal,
      });
      if (
        controller.signal.aborted
        || loadSequence.current !== sequence
        || catalogFilterKey(filtersRef.current) !== requestedFilterKey
      ) return false;
      const isLegacyResponse = result.total_count === undefined || result.facets === undefined;
      if (options.append && isLegacyResponse) {
        setError(t("load_failed"));
        return false;
      }
      const responseItems = isLegacyResponse
        ? result.items.filter((item) => (
            (requestedFilters.source === "all" || item.source.kind === requestedFilters.source)
            && (
              requestedFilters.capability === "all"
              || item.capabilities.some((entry) => entry.id === requestedFilters.capability)
            )
            && (
              requestedFilters.domain === "all"
              || item.facets.domains.includes(requestedFilters.domain)
            )
            && (
              requestedFilters.job === "all"
              || item.facets.jobs.includes(requestedFilters.job)
            )
            && (
              requestedFilters.tag === "all"
              || item.facets.team_tags?.some((entry) => entry.id === requestedFilters.tag)
            )
          ))
        : result.items;
      setItems((current) => options.append
        ? [...new Map([...current, ...responseItems].map((item) => [item.agent_id, item])).values()]
        : responseItems);
      setCanCreate(result.actions.create);
      setNextCursor(result.next_cursor);
      setTotalCount((current) => result.total_count ?? (
        options.append ? current + responseItems.length : responseItems.length
      ));
      setCapabilityFacets((current) => {
        if (result.facets?.capabilities) return result.facets.capabilities;
        const fallback = legacyCapabilityFacets(responseItems);
        return options.append ? mergeCapabilityFacets(current, fallback) : fallback;
      });
      setDomainFacets(result.facets?.domains ?? []);
      setJobFacets(result.facets?.jobs ?? []);
      setTeamTagFacets(result.facets?.team_tags ?? []);
      setFeatured(
        result.collections.find((collection) => collection.id === "recently_updated")?.items?.slice(0, 4) ?? [],
      );
      setLoaded(true);
      loadedRef.current = true;
      setError(null);
      return true;
    } catch (cause) {
      if (controller.signal.aborted || loadSequence.current !== sequence) return false;
      if (catalogFilterKey(filtersRef.current) !== requestedFilterKey) return false;
      if (options.preserve && !options.mutationSucceeded) {
        setItems([]);
        setNextCursor(null);
        setTotalCount(0);
        setCapabilityFacets([]);
        setDomainFacets([]);
        setJobFacets([]);
        setTeamTagFacets([]);
        setFeatured([]);
        setLoaded(false);
        loadedRef.current = false;
      }
      setError(t(options.mutationSucceeded ? "saved_refresh_failed" : "load_failed"));
      return false;
    } finally {
      if (!controller.signal.aborted && loadSequence.current === sequence) {
        setLoading(false);
        setValidating(false);
        setLoadingMore(false);
      }
    }
  }, [t]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setQuery(params.get("q") ?? "");
    const requestedSource = params.get("source");
    setSource(requestedSource === "team" || requestedSource === "system" ? requestedSource : "all");
    setCapability(params.get("capability") || "all");
    setDomain(params.get("domain") || "all");
    setJob(params.get("job") || "all");
    setTag(params.get("tag") || "all");
    setFiltersReady(true);
  }, []);
  useEffect(() => {
    if (!filtersReady) return;
    setNextCursor(null);
    const timeout = window.setTimeout(() => {
      const params = new URLSearchParams();
      if (query.trim()) params.set("q", query.trim());
      if (source !== "all") params.set("source", source);
      if (capability !== "all") params.set("capability", capability);
      if (domain !== "all") params.set("domain", domain);
      if (job !== "all") params.set("job", job);
      if (tag !== "all") params.set("tag", tag);
      const suffix = params.size ? `?${params.toString()}` : "";
      // Sync filters into the URL only when they changed it and the reader is still on the marketplace: a no-op
      // replace re-runs navigation (moving focus), and a late one would cancel a navigation the reader started
      // meanwhile. Never scroll the reader away from where they are.
      const target = `/agents/marketplace/${suffix}`;
      const targetUrl = new URL(appPath(target), window.location.origin);
      const samePage = window.location.pathname.replace(/\/+$/, "") === targetUrl.pathname.replace(/\/+$/, "");
      if (samePage && window.location.search !== targetUrl.search) routerRef.current.replace(target, { scroll: false });
      void refresh({ preserve: loadedRef.current });
    }, 250);
    return () => {
      window.clearTimeout(timeout);
      catalogController.current?.abort();
    };
  }, [capability, domain, filtersReady, job, query, refresh, source, tag]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const capabilities = useMemo(() => capabilityFacets.map((entry) => ({
    id: entry.id,
    label: capabilityLabel(entry),
    count: entry.count,
  })), [capabilityFacets, capabilityLabel]);
  const activeFilterCount = Number(source !== "all") + Number(capability !== "all") + Number(domain !== "all") + Number(job !== "all") + Number(tag !== "all");

  const closeFilterSheet = useCallback(() => {
    setFilterSheetOpen(false);
    window.requestAnimationFrame(() => filterTriggerRef.current?.focus());
  }, []);

  const openFilterSheet = () => {
    setDraftSource(source);
    setDraftCapability(capability);
    setDraftDomain(domain);
    setDraftJob(job);
    setDraftTag(tag);
    setFilterSheetOpen(true);
  };

  const applyFilterSheet = () => {
    setSource(draftSource);
    setCapability(draftCapability);
    setDomain(draftDomain);
    setJob(draftJob);
    setTag(draftTag);
    closeFilterSheet();
  };

  const navigate = useCallback((event: MouseEvent<HTMLAnchorElement>, path: string) => {
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    router.push(path);
  }, [router]);

  const applyMutation = (item: AgentCatalogItem, mutation: AgentAssignmentMutation) => {
    setItems((current) => current.map((entry) => entry.agent_id === item.agent_id
      ? {
          ...entry,
          assignment: { ...entry.assignment, ...mutation.assignment },
          availability: { ...entry.availability, runnable: mutation.runnable, public_reason: mutation.public_reason },
          actions: { ...entry.actions, hire: false, disable: true, unhire: true },
        }
      : entry));
  };

  const onHire = async (item: AgentCatalogItem) => {
    setPendingId(item.agent_id);
    try {
      const mutation = await hireAgent(item.agent_id);
      applyMutation(item, mutation);
      setHireSuccess({ name: displayName(item) });
      await refresh({ mutationSucceeded: true, preserve: true });
    } catch (cause) {
      setError(t("hire_failed"));
    } finally {
      setPendingId(null);
    }
  };

  // One set of distinct avatar marks for everything on the page (featured strip and catalog).
  const marks = useMemo(
    () => distinctAgentMarks([...featured, ...items].map((item) => displayName(item))),
    [displayName, featured, items],
  );

  return (
    <AgentMarksContext.Provider value={marks}>
    <div className="am-page">
      <AgentPageHeader page="marketplace" canCreate={canCreate} description={pageT("desc_market")} />

      <div className="am-toolbar" role="group" aria-label={t("filters")}>
        <label className="am-search">
          <SearchIcon />
          <span className="sr-only">{t("search_label")}</span>
          <input ref={searchRef} name="agent-search" autoComplete="off" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("search_placeholder")} />
          <kbd>⌘K</kbd>
        </label>
        {domainFacets.length || domain !== "all" ? (
          <FilterMenuButton
            value={domain}
            options={[
              { value: "all", label: pageT("filter_all") },
              ...domainFacets.map((entry) => ({ value: entry.id, label: t(`menu_taxonomy_domain_${entry.id}` as never) })),
            ]}
            onChange={setDomain}
            format={(value) => pageT("filter_category", { value })}
            menuLabel={pageT("filter_category_label")}
            active={domain !== "all"}
            testId="am-category-filter"
          />
        ) : null}
        <button
          ref={filterTriggerRef}
          type="button"
          className="am-filter-trigger"
          data-active={activeFilterCount ? "true" : undefined}
          aria-haspopup="dialog"
          aria-expanded={filterSheetOpen}
          onClick={openFilterSheet}
        >
          <FilterIcon />
          <span>{pageT("filter_button")}</span>
          {activeFilterCount ? <b aria-label={t("active_filter_count", { count: activeFilterCount })}>{activeFilterCount}</b> : null}
        </button>
        <p className="am-toolbar-count" aria-live="polite">
          {loading
            ? t("catalog_loading")
            : validating
              ? t("result_count_updating", { count: totalCount })
              : t("result_count", { count: totalCount })}
        </p>
      </div>

      {error ? <div className="am-notice" role="alert"><strong>{t("temporarily_unavailable")}</strong><span>{error}</span><button onClick={() => void refresh()}>{t("retry")}</button></div> : null}
      {hireSuccess ? (
        <div className="am-hire-success" role="status" aria-live="polite">
          <span aria-hidden="true"><CharIcon ch="✓" aria-hidden /></span>
          <div>
            <strong>{t("hire_success_title", { name: hireSuccess.name })}</strong>
            <p>{t("hire_success_body")}</p>
          </div>
          <a href={appPath("/agents/manage/")} onClick={(event) => navigate(event, "/agents/manage/")}>{t("manage_hired_agent")}</a>
          <button type="button" onClick={() => setHireSuccess(null)} aria-label={t("dismiss_hire_success")}><CloseIcon /></button>
        </div>
      ) : null}

      {!loading && featured.length ? (
        <section className="am-featured">
          <div className="am-section-heading">
            <div><span>{t("editors_choice")}</span><h2>{t("featured_title")}</h2></div>
            <p>{t("featured_subtitle")}</p>
          </div>
          <div className="am-featured-strip">
            {featured.map((item) => (
              <a key={item.agent_id} className="am-featured-card" href={appPath(`/agents/detail/?id=${encodeURIComponent(item.agent_id)}`)} onClick={(event) => navigate(event, `/agents/detail/?id=${encodeURIComponent(item.agent_id)}`)}>
                <AgentArtwork item={item} large />
                <div>
                  <span>
                    {item.capabilities[0] ? capabilityLabel(item.capabilities[0]) : t("general_agent")}
                    {item.capabilities[0]?.verification_status === "derived_unverified" ? ` · ${t("capability_pending_review")}` : ""}
                  </span>
                  <h3>{displayName(item)}</h3>
                  <small className="am-featured-developer">
                    {item.source.kind === "system" ? t("by_bowhead") : t("by_creator", { name: item.source.owner_label ?? t("team_member") })}
                    {item.source.kind === "team" ? <span> · {t("creator_listing_original")}</span> : null}
                  </small>
                  <p>{summary(item)}</p>
                </div>
              </a>
            ))}
          </div>
        </section>
      ) : null}

      <section className="am-catalog" aria-label={t("all_agents")}>
        {!loading && featured.length ? (
          <div className="am-section-heading am-catalog-heading">
            <div><h2>{t("all_agents")}</h2></div>
          </div>
        ) : null}
        {loading ? <MarketplaceSkeleton /> : items.length ? (
          <>
            <div className="am-grid" aria-busy={validating || loadingMore}>
              {items.map((item) => <AgentCard key={item.agent_id} item={item} onHire={onHire} onNavigate={navigate} pending={pendingId === item.agent_id} />)}
            </div>
            {nextCursor ? <div className="am-load-more"><button type="button" disabled={loadingMore} onClick={() => void refresh({ append: true, cursor: nextCursor })}>{loadingMore ? t("loading_more") : t("load_more")}</button></div> : null}
          </>
        ) : loaded && !error ? (
          <div className="am-empty"><h3>{t("empty_title")}</h3><p>{t("empty_body")}</p><Button variant="secondary" onClick={() => { setQuery(""); setSource("all"); setCapability("all"); setDomain("all"); setJob("all"); setTag("all"); }}>{t("clear_filters")}</Button></div>
        ) : null}
      </section>
      {filterSheetOpen ? (
        <AgentFilterSheet
          capabilities={capabilities}
          domains={domainFacets}
          draftCapability={draftCapability}
          draftDomain={draftDomain}
          draftJob={draftJob}
          draftSource={draftSource}
          draftTag={draftTag}
          jobs={jobFacets}
          onApply={applyFilterSheet}
          onClear={() => { setDraftSource("all"); setDraftCapability("all"); setDraftDomain("all"); setDraftJob("all"); setDraftTag("all"); }}
          onClose={closeFilterSheet}
          onDraftCapabilityChange={setDraftCapability}
          onDraftDomainChange={setDraftDomain}
          onDraftJobChange={setDraftJob}
          onDraftSourceChange={setDraftSource}
          onDraftTagChange={setDraftTag}
          teamTags={teamTagFacets}
        />
      ) : null}
    </div>
    </AgentMarksContext.Provider>
  );
}
