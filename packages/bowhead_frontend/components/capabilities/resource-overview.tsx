"use client";

/**
 * Overview sections for Plugins and Tools (Capability Center design brief §4.2–§4.3, mockup "1 总览"):
 * real items and state, with "View all" opening the full lists. The parts render nothing while
 * the resource endpoints are unavailable.
 */
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { CapabilityLink } from "./capability-link";
import { fetchScheduledJobs, type ScheduledJobDTO } from "@/lib/api";
import { fetchBuiltinPlugins, fetchBuiltinTools, type BuiltinPluginsView, type BuiltinToolState } from "@/lib/builtin-resources-api";
import { fetchCapabilityDiscoveryCatalog, type CapabilityDiscoveryCatalog } from "@/lib/capability-catalog-api";
import type { Locale } from "@/lib/locale";
import { visibleBuiltinToolStates } from "@/lib/tool-display";
import { personalMcpApi, type PersonalMcpConnection } from "@/lib/personal-mcp-api";
import { skillDescription, skillTitle } from "@/lib/skills-meta";
import { useSkills } from "@/lib/use-skills";
import styles from "./capability-center.module.css";

const OVERVIEW_PLUGIN_LIMIT = 6;

export interface ResourceState {
  plugins: BuiltinPluginsView;
  tools: BuiltinToolState[];
  /** Team Plugins and external Tools; null when the discovery catalog could not be read. */
  catalog: CapabilityDiscoveryCatalog | null;
}

/**
 * Built-in Plugin and Tool state for the overview; null while loading or while the switch is off.
 * The discovery catalog (team Plugins, external Tools) is read only for the overview itself.
 */
export function useResourceState({ withCatalog }: { withCatalog: boolean }): ResourceState | null {
  const [state, setState] = useState<ResourceState | null>(null);
  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      fetchBuiltinPlugins().catch(() => null),
      fetchBuiltinTools().catch(() => null),
      withCatalog ? Promise.resolve().then(fetchCapabilityDiscoveryCatalog).catch(() => null) : null,
    ]).then(([plugins, tools, catalog]) => {
      if (!cancelled) setState(plugins && tools ? { plugins, tools, catalog } : null);
    });
    return () => {
      cancelled = true;
    };
  }, [withCatalog]);
  return state;
}

/** Plugin cards: attention first, then registry order. With "Only mine", only Plugins the viewer turned off. */
export function PluginOverview({ state, onlyMine }: { state: ResourceState; onlyMine: boolean }) {
  const t = useTranslations("sidebar.centers");
  const r = useTranslations("sidebar.centers.resources");
  const locale = useLocale() as Locale;
  const { plugins } = state;
  const disabled = plugins.plugins.filter((plugin) => plugin.team_disabled || plugin.my_disabled).length;
  const candidates = onlyMine
    ? plugins.plugins.filter((plugin) => plugin.my_disabled)
    : plugins.plugins.filter((plugin) => plugin.family === "general");
  const shown = [...candidates]
    .sort((a, b) => Number(b.team_disabled || b.my_disabled || b.ready === false) - Number(a.team_disabled || a.my_disabled || a.ready === false))
    .slice(0, OVERVIEW_PLUGIN_LIMIT);
  return (
    <>
      <span className={styles.count}>
        {onlyMine
          ? r("overview_my_disabled", { count: shown.length })
          : state.catalog
            ? r("overview_plugins_count_team", { builtin: plugins.plugins.length, team: state.catalog.capabilities.length, disabled })
            : r("overview_plugins_count", { builtin: plugins.plugins.length, disabled })}
      </span>
      {shown.length ? (
        <div className={styles.grid}>
          {shown.map((plugin) => {
            const badge = plugin.team_disabled
              ? r("team_disabled")
              : plugin.my_disabled
                ? r("my_disabled")
                : plugin.ready === false
                  ? r("not_ready")
                  : null;
            return (
              <CapabilityLink key={plugin.name} href="/capabilities/?type=capabilities" className={styles.card}>
                <h2>
                  {plugin.listing[locale]?.name ??
                    (t.has(`system_plugins.${plugin.name}.name`) ? t(`system_plugins.${plugin.name}.name`) : plugin.name)}
                  <span className={styles.version}>v{plugin.version}</span>
                </h2>
                <p>{plugin.listing[locale]?.description ?? ""}</p>
                <span className={styles.cardFoot}>
                  {t("bundle_count", { tools: plugin.tool_count, skills: plugin.skill_count })}
                  {badge ? (
                    <Badge tone={plugin.ready === false && !plugin.team_disabled && !plugin.my_disabled ? "warning" : "neutral"} size="sm">
                      {badge}
                    </Badge>
                  ) : null}
                </span>
              </CapabilityLink>
            );
          })}
        </div>
      ) : null}
    </>
  );
}

const OVERVIEW_ROW_LIMIT = 4;

/** Skills section (mockup A1/A3): counts and the first few Skills, the viewer's own first. */
export function SkillsOverview({ onlyMine }: { onlyMine: boolean }) {
  const r = useTranslations("sidebar.centers.resources");
  const { data } = useSkills();
  if (!data) return null;
  const mine = [...data.user_upload, ...data.user_generated];
  const shown = (onlyMine ? mine : [...mine, ...data.builtin]).slice(0, OVERVIEW_ROW_LIMIT);
  return (
    <>
      <span className={styles.count}>{r("overview_skills_count", { mine: mine.length, builtin: data.builtin.length })}</span>
      {shown.length ? (
        <div className={styles.grid}>
          {shown.map((skill) => (
            <div key={`${skill.source_type}:${skill.id ?? skill.name}`} className={styles.card}>
              <h2>{skillTitle(skill)}</h2>
              <p>{skillDescription(skill)}</p>
              {skill.enabled ? null : (
                <span className={styles.cardFoot}>
                  <Badge tone="neutral" size="sm">
                    {r("overview_state_off")}
                  </Badge>
                </span>
              )}
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}

export interface PersonalOverviewEntry {
  id: "connections" | "scheduled";
  href: string;
  /** The plain entry card, shown while loading or when the live summary cannot be read. */
  fallback: ReactNode;
}

/**
 * Personal connections and scheduled tasks (mockup A1): one card per available entry, showing its
 * count and first rows and opening the page; an entry whose summary cannot be read keeps its plain card.
 */
export function PersonalOverview({ entries }: { entries: PersonalOverviewEntry[] }) {
  const r = useTranslations("sidebar.centers.resources");
  const [connections, setConnections] = useState<PersonalMcpConnection[] | null>(null);
  const [jobs, setJobs] = useState<ScheduledJobDTO[] | null>(null);
  const wantsConnections = entries.some((entry) => entry.id === "connections");
  const wantsJobs = entries.some((entry) => entry.id === "scheduled");
  useEffect(() => {
    let cancelled = false;
    if (wantsConnections)
      void personalMcpApi<{ connections: PersonalMcpConnection[] }>("")
        .then((data) => !cancelled && setConnections(data.connections))
        .catch(() => undefined);
    if (wantsJobs)
      void fetchScheduledJobs()
        .then((data) => !cancelled && setJobs(data.items))
        .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [wantsConnections, wantsJobs]);
  if (!entries.length) return null;
  const active = jobs?.filter((job) => job.status === "active").length ?? 0;
  const paused = jobs?.filter((job) => job.status === "paused").length ?? 0;
  const live = (entry: PersonalOverviewEntry): ReactNode => {
    if (entry.id === "connections" && connections) {
      return (
        <CapabilityLink key={entry.id} href={entry.href} className={styles.card}>
          <h3>{r("overview_connections")}</h3>
          <p>
            {connections.length
              ? connections
                  .slice(0, OVERVIEW_ROW_LIMIT)
                  .map((item) => item.name)
                  .join(" · ")
              : r("overview_connections_empty")}
          </p>
          <span className={styles.cardFoot}>{r("overview_connections_count", { count: connections.length })}</span>
        </CapabilityLink>
      );
    }
    if (entry.id === "scheduled" && jobs) {
      return (
        <CapabilityLink key={entry.id} href={entry.href} className={styles.card}>
          <h3>{r("overview_scheduled")}</h3>
          <p>
            {jobs.length
              ? jobs
                  .slice(0, OVERVIEW_ROW_LIMIT)
                  .map((job) => (job.status === "paused" ? `${job.name} (${r("overview_state_paused")})` : job.name))
                  .join(" · ")
              : r("overview_scheduled_empty")}
          </p>
          <span className={styles.cardFoot}>{r("overview_scheduled_count", { active, paused })}</span>
        </CapabilityLink>
      );
    }
    return <Fragment key={entry.id}>{entry.fallback}</Fragment>;
  };
  return <div className={styles.grid}>{entries.map(live)}</div>;
}

/** Tool counts and, for administrators, the attention strip. */
export function ToolOverview({ state, admin, googleWorkspace }: { state: ResourceState; admin: boolean; googleWorkspace: boolean }) {
  const r = useTranslations("sidebar.centers.resources");
  const tools = visibleBuiltinToolStates(state.tools, googleWorkspace);
  const highRiskOff = tools.filter((tool) => tool.risk === "external_write" && !tool.enabled).length;
  const external = state.catalog?.tools.filter((tool) => tool.source_kind !== "native") ?? null;
  const sources = [...new Set((external ?? []).map((tool) => tool.source_display_name).filter((name): name is string => Boolean(name)))];
  return (
    <>
      <span className={styles.count}>
        {external
          ? r("overview_tools_count_external", { count: tools.length, external: external.length })
          : r("overview_tools_count", { count: tools.length })}
      </span>
      {sources.length ? <span className={styles.count}>{r("overview_tool_sources", { names: sources.join(" · ") })}</span> : null}
      {admin && highRiskOff > 0 ? (
        <CapabilityLink className={styles.attention} href="/capabilities/?type=tools">
          {r("overview_high_risk_off", { count: highRiskOff })}
        </CapabilityLink>
      ) : null}
    </>
  );
}
