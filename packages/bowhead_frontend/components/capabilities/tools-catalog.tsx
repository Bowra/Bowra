"use client";

/**
 * The Tools page (`/capabilities/?type=tools`): everything an Agent can call, by origin — built-in Tools with their
 * Team state, the always-on system capabilities, then external Tools grouped by their source with risk tags and the
 * catalog's reason when one cannot be added to an Agent. Product logic: docs/sprints/tools-page-2026-11-27/product-logic.md.
 */
import { useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import type { BuiltinPluginState, BuiltinToolState } from "@/lib/builtin-resources-api";
import type { CapabilityDiscoveryCatalog, DiscoveryTool } from "@/lib/capability-catalog-api";
import type { Locale } from "@/lib/locale";
import { listSources } from "@/lib/team-tool-sources-api";
import { localizedToolText, systemCapabilities } from "@/lib/tool-display";
import { CapabilityManagementLink } from "./capability-management-link";
import { RiskTag, ToolStateList, builtinToolRows, useHeadingCount } from "./tool-state-list";
import type { MeUser } from "@/lib/auth-api";
import styles from "./capability-center.module.css";

const LOCKED_REASONS = new Set(["approval_executor_unavailable", "risk_metadata_unknown", "mcp_feature_unlicensed"]);

export function ToolsCatalog({
  me,
  catalog,
  states,
  plugins,
  admin,
  needle,
  onChanged,
}: {
  me: MeUser;
  catalog: CapabilityDiscoveryCatalog;
  states: BuiltinToolState[] | null;
  plugins: BuiltinPluginState[] | null;
  admin: boolean;
  needle: string;
  onChanged: () => void;
}) {
  const t = useTranslations("sidebar.centers");
  const r = useTranslations("sidebar.centers.resources");
  const locale = useLocale() as Locale;
  const headingCount = useHeadingCount();
  const googleWorkspace = Boolean(me.capabilities?.google_workspace);
  const matches = (text: string) => text.toLocaleLowerCase().includes(needle);

  const system = systemCapabilities(locale);
  const shownSystem = system.filter((tool) => matches(`${tool.name} ${tool.blurb}`));

  const external = catalog.tools
    .filter((tool) => tool.source_kind !== "native")
    .map((tool) => ({ tool, source: tool.source_display_name || t("team_source"), ...localizedToolText(tool, locale) }));
  const shownExternal = external.filter((item) => matches(`${item.name} ${item.description} ${item.source}`));
  const sources = [...new Set(shownExternal.map((item) => item.source))].map((source) => ({
    source,
    items: shownExternal.filter((item) => item.source === source),
  }));
  const offSources = useSourcesOutsideCatalog(admin);

  const builtinMatches = builtinToolRows(states, googleWorkspace, locale, needle).rows.length;
  const nothing = Boolean(needle) && builtinMatches === 0 && shownSystem.length === 0 && shownExternal.length === 0;

  return (
    <>
      <ToolStateList tools={states} plugins={plugins} admin={admin} googleWorkspace={googleWorkspace} needle={needle} onChanged={onChanged} />
      {shownSystem.length > 0 ? (
        <section aria-label={r("system_capabilities")} className={styles.catalogSection} data-testid="system-capabilities">
          <h2 className="ui-section-title">
            {r("system_capabilities")} <span className={styles.headingCount}>{headingCount(shownSystem.length, system.length, Boolean(needle))}</span>
          </h2>
          <ul className={styles.list}>
            {shownSystem.map((tool) => (
              <li key={tool.ref}>
                <div>
                  <h3>{tool.name}</h3>
                  <p>{tool.blurb}</p>
                </div>
                <div className={styles.rowState}>
                  <small>{r("always_on")}</small>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {sources.length > 0 || (!needle && offSources > 0) ? (
        <section aria-label={t("external_tools")} className={styles.catalogSection}>
          <h2 className="ui-section-title">
            {t("external_tools")} <span className={styles.headingCount}>{headingCount(shownExternal.length, external.length, Boolean(needle))}</span>
          </h2>
          {sources.map(({ source, items }) => (
            <section key={source} aria-label={source} className={styles.toolGroup}>
              <h3 className={styles.groupTitle}>
                {source} <span className={styles.headingCount}>{items.length}</span>
              </h3>
              <ul className={styles.list}>
                {items.map(({ tool, name, description }) => (
                  <li key={tool.stable_ref}>
                    <div>
                      <h4>
                        {name}
                        <RiskTag risk={tool.risk} />
                      </h4>
                      {description ? (
                        <p className={styles.clamp} title={description}>
                          {description}
                        </p>
                      ) : null}
                      {tool.selectable ? null : <p className={styles.lockedReason}>{lockedReason(r, tool)}</p>}
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ))}
          {!needle && offSources > 0 ? (
            <div className={styles.sourcesNote} role="note">
              <span>{r("external_sources_off", { count: offSources })}</span>
              <CapabilityManagementLink resource="tools" me={me} />
            </div>
          ) : null}
        </section>
      ) : null}
      {nothing ? <p role="status">{t("tools_no_results")}</p> : null}
    </>
  );
}

function lockedReason(r: ReturnType<typeof useTranslations>, tool: DiscoveryTool): string {
  const code = tool.locked_reason ?? "";
  return LOCKED_REASONS.has(code) ? r(`locked.${code}`) : r("locked.other", { code: code || "unavailable" });
}

/**
 * Team Tool Sources that are not on (turned off, or waiting for sign-in): the catalog leaves their Tools out, so
 * administrators are told how many there are. Members get no source list (the API is for administrators).
 */
function useSourcesOutsideCatalog(admin: boolean): number {
  const [count, setCount] = useState(0);
  useEffect(() => {
    if (!admin) return;
    let cancelled = false;
    void listSources()
      .then((items) => {
        if (!cancelled) setCount(items.filter((source) => source.status !== "enabled").length);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [admin]);
  return admin ? count : 0;
}
