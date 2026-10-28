"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { ChevronRight } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { PageFrame, PageHeader } from "@/components/shell/page-frame";
import { Switch } from "@/components/ui/switch";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { fetchMe, type MeUser } from "@/lib/auth-api";
import { CapabilityLink } from "./capability-link";
import { CapabilityManagementLink } from "./capability-management-link";
import {
  fetchCapabilityDiscoveryCatalog,
  type CapabilityDiscoveryCatalog,
} from "@/lib/capability-catalog-api";
import {
  capabilityOverviewGroups,
} from "@/lib/navigation-centers";
import { builtinTools, localizedToolText } from "@/lib/tool-display";
import {
  fetchBuiltinPlugins,
  fetchBuiltinTools,
  type BuiltinPluginsView,
  type BuiltinToolState,
} from "@/lib/builtin-resources-api";
import { PluginStateList } from "./plugin-state-list";
import { ToolStateList } from "./tool-state-list";
import { PersonalOverview, PluginOverview, SkillsOverview, ToolOverview, useResourceState } from "./resource-overview";
import { teamRoleForUser } from "@/lib/settings-navigation";
import styles from "./capability-center.module.css";

type CatalogType = "tools" | "capabilities";

/** The member catalog a Discover URL shows (`?type=tools|capabilities`), or null for the overview views. */
function catalogTypeFromQuery(query: Pick<URLSearchParams, "get">): CatalogType | null {
  const view = query.get("view");
  const type = query.get("type");
  if (view === "team" || view === "my") return null;
  return type === "tools" || type === "capabilities" ? type : null;
}

function pageTitleKey(catalogType: CatalogType | null): "tools_catalog" | "capabilities_catalog" | "title" {
  return catalogType === "tools" ? "tools_catalog" : catalogType === "capabilities" ? "capabilities_catalog" : "title";
}

export function CapabilityCenter() {
  // The page title follows the catalog view, so the route reads the query; Suspense covers static rendering.
  return (
    <Suspense fallback={null}>
      <CapabilityCenterPage />
    </Suspense>
  );
}

function CapabilityCenterPage() {
  const query = useSearchParams();
  const session = useOptionalAuthSession();
  const [fallback, setFallback] = useState<MeUser | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const t = useTranslations("sidebar.centers");
  // The top-bar title repeats the page H1 (Tools / Team Plugins / Capability Center) for the large-title pattern.
  const pageTitle = t(pageTitleKey(catalogTypeFromQuery(query)));
  const me = session ? session.me : fallback;
  const loadFailed = session ? session.status === "error" : failed;
  useEffect(() => {
    if (session) return;
    let cancelled = false;
    setFailed(false);
    void fetchMe()
      .then((user) => {
        if (!cancelled) setFallback(user);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [session, retry]);
  return (
    // PageFrame (Header, Language & Paging Spec §2, D4–D6): shared top bar, the 1120 column, the standard header.
    <PageFrame title={pageTitle}>
      {!me ? (
        <div className={styles.content}>
          <p role={loadFailed ? "alert" : "status"}>
            {t(loadFailed ? "load_failed" : "loading")}
          </p>
          {loadFailed ? (
            <button
              type="button"
              className="ui-btn"
              onClick={() => (session ? session.retry() : setRetry((value) => value + 1))}
            >
              {t("retry")}
            </button>
          ) : null}
        </div>
      ) : (
        <Suspense fallback={<p role="status">{t("loading")}</p>}>
          <CenterContent me={me} />
        </Suspense>
      )}
    </PageFrame>
  );
}

type OverviewSection = "plugins" | "skills" | "tools" | "agents" | "personal";

// Which overview section shows each existing entry point (destination id from capabilityOverviewGroups).
const SECTION_OF: Record<string, OverviewSection> = {
  capabilities_catalog: "plugins",
  capabilities: "plugins",
  skills: "skills",
  my_skills: "skills",
  workflows: "skills",
  tools_catalog: "tools",
  tools: "tools",
  agents: "agents",
  my_agents: "agents",
  team_agents: "agents",
  subagents: "agents",
  connections: "personal",
  scheduled: "personal",
};
const SECTIONS: OverviewSection[] = ["plugins", "skills", "tools", "personal", "agents"];
const VIEW_ALL = new Set(["capabilities_catalog", "tools_catalog"]);
const PERSONAL_LIVE = new Set(["connections", "scheduled"]);
// Administrator management entries sit inside the section of the same name, so they say what they manage.
// Card titles and links use sentence case (menus contract: only "⋯" menu items use Title Case).
const MANAGE_LABEL: Record<string, "manage_capabilities_button" | "manage_tools_button"> = {
  capabilities: "manage_capabilities_button",
  tools: "manage_tools_button",
};

function CenterContent({ me }: { me: MeUser }) {
  const query = useSearchParams();
  const t = useTranslations("sidebar.centers");
  const r = useTranslations("sidebar.centers.resources");
  const tabs = useTranslations("settings.tabs");
  const role = teamRoleForUser(me);
  const admin = role === "owner" || role === "team_admin";
  const view = query.get("view");
  // "Only mine" replaces the former My Capabilities tab; ?view=my keeps opening it.
  const [onlyMine, setOnlyMine] = useState(view === "my");
  const catalogType = catalogTypeFromQuery(query);
  const resourceState = useResourceState({ withCatalog: !catalogType });
  const groups = capabilityOverviewGroups(me);
  const visibleGroups = onlyMine ? ["capability_personal"] : ["capability_discovery", "capability_personal", "capability_team"];
  const items = groups
    .filter((group) => visibleGroups.includes(group.key))
    .flatMap((group) => group.items)
    .filter((item) => item.destination.kind !== "center" || !["discover", "mine", "team"].includes(item.destination.id));
  const entryCard = (item: (typeof items)[number]) => {
    const id = item.destination.id;
    const manage = MANAGE_LABEL[id];
    const label = manage ? t(manage) : item.destination.kind === "tab" ? tabs(id) : t(id);
    // A card that would repeat its section heading ("Plugins" under "Plugins", the full-list fallback while the
    // Plugin/Tool state is unavailable) keeps only its description and "Open …" link.
    const section = SECTION_OF[id];
    const repeatsSection = section !== undefined && label === sectionTitle(section);
    return (
      <CapabilityLink key={item.key} href={item.route} className={styles.card}>
        {repeatsSection ? null : <h3>{label}</h3>}
        <p>{t(`descriptions.${id}`)}</p>
        <span className={styles.cardLink}>
          {manage ? label : t("open", { name: label })}
          <LucideGlyph icon={ChevronRight} size={14} aria-hidden="true" />
        </span>
      </CapabilityLink>
    );
  };
  const sectionTitle = (section: OverviewSection) =>
    section === "plugins" ? r("plugins") : section === "skills" ? t("skills") : section === "tools" ? t("tools_catalog") : section === "agents" ? t("agents") : r("overview_personal");
  return (
    <section className={styles.content} aria-label={t("title")} data-testid="capability-center">
      <PageHeader
        title={t(pageTitleKey(catalogType))}
        description={catalogType ? t("catalog_description") : t("description")}
      />
      {view === "team" && !admin ? (
        <p role="alert">{t("team_denied")}</p>
      ) : catalogType ? (
        <DiscoveryCatalog
          key={`${me.id}|${me.team_id}|${me.auth_session_generation ?? ""}|${catalogType}`}
          me={me}
          type={catalogType}
        />
      ) : (
        <>
          {/* One resource-sectioned page (brief §3); "Only mine" is a header toggle, not a tab. */}
          <label className={styles.onlyMine}>
            <Switch checked={onlyMine} onCheckedChange={setOnlyMine} aria-label={r("only_mine")} />
            <span>{r("only_mine")}</span>
          </label>
          {SECTIONS.map((section) => {
            // The full Plugin / Tool lists open from the section's "View all" link instead of a card.
            const viewAll = items.find((item) => SECTION_OF[item.destination.id] === section && VIEW_ALL.has(item.destination.id));
            const sectionItems = items.filter((item) => SECTION_OF[item.destination.id] === section && !VIEW_ALL.has(item.destination.id));
            const showPlugins = section === "plugins" && resourceState;
            const showTools = section === "tools" && resourceState && !onlyMine;
            // Without the Plugin/Tool state (switch off, request failed or still loading) the full list
            // keeps its card, so members never lose the entry point.
            const fallback = viewAll && !showPlugins && !showTools && !(section === "tools" && onlyMine) ? [viewAll] : [];
            // Connections and scheduled tasks show one live card each; the plain card is their fallback.
            const personal = sectionItems.filter((item) => PERSONAL_LIVE.has(item.destination.id));
            const cards = [...fallback, ...sectionItems.filter((item) => !PERSONAL_LIVE.has(item.destination.id))];
            if (!cards.length && !showPlugins && !showTools && !personal.length) return null;
            return (
              <section key={section} className={styles.overviewSection} aria-label={sectionTitle(section)}>
                <div className={styles.overviewHead}>
                  <h2 className="ui-section-title">{sectionTitle(section)}</h2>
                  {viewAll && !fallback.length ? (
                    <CapabilityLink className={styles.viewAll} href={viewAll.route}>
                      {r("view_all")}
                    </CapabilityLink>
                  ) : null}
                </div>
                {showPlugins ? <PluginOverview state={resourceState} onlyMine={onlyMine} /> : null}
                {showTools ? <ToolOverview state={resourceState} admin={admin} googleWorkspace={Boolean(me.capabilities?.google_workspace)} /> : null}
                {section === "skills" ? <SkillsOverview onlyMine={onlyMine} /> : null}
                {personal.length ? (
                  <PersonalOverview
                    entries={personal.map((item) => ({
                      id: item.destination.id as "connections" | "scheduled",
                      href: item.route,
                      fallback: entryCard(item),
                    }))}
                  />
                ) : null}
                {cards.length ? <div className={styles.grid}>{cards.map(entryCard)}</div> : null}
              </section>
            );
          })}
        </>
      )}
    </section>
  );
}

function DiscoveryCatalog({ me, type }: { me: MeUser; type: "tools" | "capabilities" }) {
  const t = useTranslations("sidebar.centers");
  const [catalog, setCatalog] = useState<CapabilityDiscoveryCatalog | null>(null);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const [query, setQuery] = useState("");
  // Team/personal state of built-in Plugins and Tools; null when unavailable,
  // in which case the read-only catalog below stays as it was.
  const [plugins, setPlugins] = useState<BuiltinPluginsView | null>(null);
  const [stateTools, setStateTools] = useState<BuiltinToolState[] | null>(null);
  const [stateVersion, setStateVersion] = useState(0);
  const role = teamRoleForUser(me);
  const admin = role === "owner" || role === "team_admin";
  useEffect(() => {
    let cancelled = false;
    setCatalog(null);
    setError(false);
    void fetchCapabilityDiscoveryCatalog()
      .then((result) => {
        if (!cancelled) setCatalog(result);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [retry]);
  useEffect(() => {
    let cancelled = false;
    void Promise.all([fetchBuiltinPlugins().catch(() => null), fetchBuiltinTools().catch(() => null)]).then(([pluginView, toolStates]) => {
      if (cancelled) return;
      setPlugins(pluginView);
      setStateTools(toolStates);
    });
    return () => {
      cancelled = true;
    };
  }, [retry, stateVersion]);
  const reloadState = () => setStateVersion((value) => value + 1);
  const needle = query.trim().toLocaleLowerCase();
  const builtin = builtinTools({
    googleWorkspace: Boolean(me.capabilities?.google_workspace),
  });
  const rows =
    type === "capabilities"
      ? [
          ...(catalog?.builtin_plugins ?? []).map((item) => ({
            id: item.id,
            name: t.has(`system_plugins.${item.name}.name`) ? t(`system_plugins.${item.name}.name`) : item.name,
            description: t.has(`system_plugins.${item.name}.description`) ? t(`system_plugins.${item.name}.description`) : t("settings_plugin_usage"),
            source: t("builtin_source"),
            detail: t("bundle_count", { tools: item.tool_count, skills: item.skill_count }),
            builtinPlugin: true,
          })),
          ...(catalog?.capabilities.map((item) => ({
            id: item.id,
            name: item.name,
            description: item.description,
            source: t("team_source"),
            detail: t("bundle_count", { tools: item.tool_count, skills: item.skill_count }),
            builtinPlugin: false,
          })) ?? []),
        ]
      : [
          ...builtin.map((item) => ({
            id: item.ref,
            name: item.name,
            description: item.blurb,
            source: t("builtin_source"),
            detail: t("runtime_access"),
            builtinPlugin: false,
          })),
          ...(catalog?.tools
            .filter((item) => item.source_kind !== "native")
            .map((item) => {
              const text = localizedToolText(item);
              return {
                id: item.stable_ref,
                ...text,
                source: item.source_display_name || t("team_source"),
                detail: t(item.selectable ? "selectable" : "unavailable"),
                builtinPlugin: false,
              };
            }) ?? []),
        ];
  const visible = rows.filter((item) =>
    `${item.name} ${item.description} ${item.source}`.toLocaleLowerCase().includes(needle),
  );
  const renderRows = (items: typeof rows) => (
    <ul className={styles.list}>
      {items.map((item) => (
        <li key={item.id}>
          <div>
            <h3>{item.name}</h3>
            <p>{item.description}</p>
          </div>
          <div className={styles.meta}>
            <span>{item.source}</span>
            <small>{item.detail}</small>
          </div>
        </li>
      ))}
    </ul>
  );
  const builtinPlugins = visible.filter((item) => item.builtinPlugin);
  const teamPlugins = visible.filter((item) => !item.builtinPlugin);
  return (
    <section aria-label={t(type === "tools" ? "tools_catalog" : "capabilities_catalog")}>
      <div className={styles.header}>
        <CapabilityManagementLink resource={type} me={me} />
        <input
          type="search"
          className="ui-input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label={t("catalog_search")}
          placeholder={t("catalog_search")}
        />
      </div>
      {error ? (
        <div role="alert">
          <p>{t("load_failed")}</p>
          <button className="ui-btn" onClick={() => setRetry((value) => value + 1)}>
            {t("retry")}
          </button>
        </div>
      ) : !catalog ? (
        <p role="status">{t("loading")}</p>
      ) : (
        <>
          {type === "capabilities" && plugins ? (
            <PluginStateList
              builtin={plugins.plugins}
              team={catalog.capabilities}
              teamMyDisabled={plugins.team_plugins_my_disabled}
              admin={admin}
              needle={needle}
              onChanged={reloadState}
            />
          ) : type === "tools" && plugins && stateTools ? (
            <>
              <ToolStateList
                tools={stateTools}
                plugins={plugins.plugins}
                admin={admin}
                googleWorkspace={Boolean(me.capabilities?.google_workspace)}
                needle={needle}
                onChanged={reloadState}
              />
              {visible.some((item) => item.source !== t("builtin_source")) ? (
                <section aria-label={t("external_tools")}>
                  <h2 className="ui-section-title">{t("external_tools")}</h2>
                  {renderRows(visible.filter((item) => item.source !== t("builtin_source")))}
                </section>
              ) : null}
            </>
          ) : visible.length === 0 ? (
            <p role="status">{t(needle ? "no_results" : "catalog_empty")}</p>
          ) : type === "capabilities" ? (
            <>
              {builtinPlugins.length > 0 ? (
                <section aria-label={t("builtin_plugins")}>
                  <h2 className="ui-section-title">{t("builtin_plugins")}</h2>
                  <p className="ui-page-desc">{t("system_plugin_usage")}</p>
                  {renderRows(builtinPlugins)}
                </section>
              ) : null}
              {/* Hidden when the team has no team Plugins, as in the Agent picker (design §7.3 item 8). */}
              {teamPlugins.length > 0 ? (
                <section aria-label={t("team_plugins")}>
                  <h2 className="ui-section-title">{t("team_plugins")}</h2>
                  {renderRows(teamPlugins)}
                </section>
              ) : null}
            </>
          ) : (
            renderRows(visible)
          )}
        </>
      )}
    </section>
  );
}
