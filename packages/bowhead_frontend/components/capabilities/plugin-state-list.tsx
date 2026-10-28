"use client";

/**
 * Plugins catalog with team and personal switches (Capability Center design brief §4.3, §4.9).
 *
 * Built-in and team Plugins share one row style. Administrators get a "Team" switch; everyone gets a
 * "Me" switch that is locked while the team has the Plugin disabled. A team-level disable previews
 * the affected Agents and scheduled tasks first; a personal disable applies at once with Undo.
 */
import { useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import { agentKeyFor, useBuiltinAgentLabel } from "@/components/chat/step-list/agent-meta";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  fetchDisableImpact,
  setBuiltinPluginMyState,
  setBuiltinPluginTeamState,
  setTeamPluginMyState,
  type BuiltinPluginState,
  type DisableImpact,
  type ImpactKind,
} from "@/lib/builtin-resources-api";
import type { DiscoveryPlugin } from "@/lib/capability-catalog-api";
import type { Locale } from "@/lib/locale";
import { toast } from "@/lib/use-toast";
import styles from "./capability-center.module.css";

interface Row {
  key: string;
  id: string;
  kind: "builtin" | "team";
  name: string;
  version: string | null;
  description: string;
  count: string;
  teamDisabled: boolean;
  myDisabled: boolean;
  /** None of the Plugin's Tools is configured for the team. */
  notReady: boolean;
  /** Team Plugins keep their team switch on the management page. */
  teamSwitch: boolean;
}

export function PluginStateList({
  builtin,
  team,
  teamMyDisabled,
  admin,
  needle,
  onChanged,
}: {
  builtin: BuiltinPluginState[];
  team: DiscoveryPlugin[];
  teamMyDisabled: string[];
  admin: boolean;
  needle: string;
  onChanged: () => void;
}) {
  const t = useTranslations("sidebar.centers");
  const r = useTranslations("sidebar.centers.resources");
  const locale = useLocale() as Locale;
  const { confirm, dialog } = useConfirmDialog();
  const [busy, setBusy] = useState<string | null>(null);

  // General Plugins first, settings Plugins after, each in registry order (rows never jump on state changes).
  const ordered = [...builtin].sort((a, b) => Number(a.family === "settings") - Number(b.family === "settings"));
  const builtinRows: Row[] = ordered.map((plugin) => ({
    key: `builtin:${plugin.name}`,
    id: plugin.name,
    kind: "builtin" as const,
    name: plugin.listing[locale]?.name ?? (t.has(`system_plugins.${plugin.name}.name`) ? t(`system_plugins.${plugin.name}.name`) : plugin.name),
    version: `v${plugin.version}`,
    description:
      plugin.listing[locale]?.description ??
      (t.has(`system_plugins.${plugin.name}.description`) ? t(`system_plugins.${plugin.name}.description`) : "") +
        (plugin.family === "settings" ? ` ${r("settings_only")}` : ""),
    count: t("bundle_count", { tools: plugin.tool_count, skills: plugin.skill_count }),
    teamDisabled: plugin.team_disabled,
    myDisabled: plugin.my_disabled,
    notReady: plugin.ready === false,
    teamSwitch: true,
  }));
  const teamRows: Row[] = team.map((plugin) => ({
    key: `team:${plugin.id}`,
    id: plugin.id,
    kind: "team",
    name: plugin.name,
    version: null,
    description: plugin.description,
    count: t("bundle_count", { tools: plugin.tool_count, skills: plugin.skill_count }),
    teamDisabled: false,
    myDisabled: teamMyDisabled.includes(plugin.id),
    notReady: false,
    teamSwitch: false,
  }));
  const matches = (row: Row) => `${row.name} ${row.description}`.toLocaleLowerCase().includes(needle);

  /** Resolves true when the save succeeded; failures show the error toast and resolve false. */
  const run = async (key: string, action: () => Promise<void>): Promise<boolean> => {
    setBusy(key);
    try {
      await action();
      onChanged();
      return true;
    } catch {
      toast({ kind: "error", text: r("save_failed") });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const setTeam = async (row: Row, enabled: boolean) => {
    if (!enabled) {
      const impact = await fetchDisableImpact("builtin_plugin" satisfies ImpactKind, row.id).catch(() => null);
      const ok = await confirm({
        title:
          impact && countAgents(impact) > 0
            ? r("disable_impact_title", { name: row.name, count: countAgents(impact) })
            : r("disable_title", { name: row.name }),
        description: <ImpactBody impact={impact} reportsWarning={row.id === "reports-canvas"} />,
        confirmLabel: r("disable"),
        tone: "danger",
      });
      if (!ok) return;
    }
    await run(row.key, () => setBuiltinPluginTeamState(row.id, !enabled));
  };

  const setMine = async (row: Row, enabled: boolean) => {
    const save = (disabled: boolean) => (row.kind === "builtin" ? setBuiltinPluginMyState(row.id, disabled) : setTeamPluginMyState(row.id, disabled));
    const saved = await run(row.key, () => save(!enabled));
    if (saved && !enabled) {
      toast({
        kind: "info",
        undo: true,
        text: r("disabled_for_you", { name: row.name }),
        action: { label: r("undo"), onAction: async () => void (await run(row.key, () => save(false))) },
      });
    }
  };

  const renderRow = (row: Row) => {
    const off = row.teamDisabled || row.myDisabled;
    const badge = row.teamDisabled ? r("team_disabled") : row.myDisabled ? r("my_disabled") : null;
    const notReady = !off && row.notReady;
    return (
      <li key={row.key} className={off ? styles.rowOff : undefined}>
        <div>
          <h3>
            {row.name}
            {row.version ? <span className={styles.version}>{row.version}</span> : null}
            {badge ? (
              <Badge tone="neutral" size="sm" className={styles.badge}>
                {badge}
              </Badge>
            ) : null}
            {notReady ? (
              <Badge tone="warning" size="sm" className={styles.badge}>
                {r("not_ready")}
              </Badge>
            ) : null}
          </h3>
          <p>{row.description}</p>
        </div>
        <div className={`${styles.meta} ${styles.pluginMeta}`}>
          <small>{row.count}</small>
          <div className={styles.switches}>
            {admin && row.teamSwitch ? (
              <label className={styles.switchColumn}>
                {r("team")}
                <Switch
                  checked={!row.teamDisabled}
                  busy={busy === row.key}
                  aria-label={r("team_switch", { name: row.name })}
                  onCheckedChange={(enabled) => void setTeam(row, enabled)}
                />
              </label>
            ) : null}
            <label className={styles.switchColumn} title={row.teamDisabled ? r("team_locked") : undefined}>
              {r("me")}
              <Switch
                checked={!row.myDisabled && !row.teamDisabled}
                disabled={row.teamDisabled}
                busy={busy === row.key}
                aria-label={row.teamDisabled ? `${r("me_switch", { name: row.name })} ${r("team_locked")}` : r("me_switch", { name: row.name })}
                onCheckedChange={(enabled) => void setMine(row, enabled)}
              />
            </label>
          </div>
        </div>
      </li>
    );
  };

  const visibleBuiltin = builtinRows.filter(matches);
  const visibleTeam = teamRows.filter(matches);
  return (
    <>
      {visibleBuiltin.length > 0 ? (
        <section aria-label={t("builtin_plugins")}>
          <h2 className="ui-section-title">
            {t("builtin_plugins")} <span className={styles.headingCount}>{builtinRows.length}</span>
          </h2>
          <ul className={styles.list}>{visibleBuiltin.map(renderRow)}</ul>
        </section>
      ) : null}
      {visibleTeam.length > 0 || !needle ? (
        <section aria-label={t("team_plugins")}>
          <h2 className="ui-section-title">
            {t("team_plugins")} <span className={styles.headingCount}>{teamRows.length}</span>
          </h2>
          {visibleTeam.length > 0 ? <ul className={styles.list}>{visibleTeam.map(renderRow)}</ul> : <p role="status">{t("team_plugins_empty")}</p>}
        </section>
      ) : null}
      {dialog}
    </>
  );
}

function countAgents(impact: DisableImpact): number {
  return impact.system_agents.length + impact.team_agents.length;
}

/** Impact preview body (B5/B6): affected system Agents, team Agents and scheduled tasks. */
export function ImpactBody({
  impact,
  reportsWarning = false,
  lead = true,
}: {
  impact: DisableImpact | null;
  reportsWarning?: boolean;
  /** False in details panels, which list usage without the "turning this off affects" sentence. */
  lead?: boolean;
}): ReactNode {
  const r = useTranslations("sidebar.centers.resources");
  const builtinLabel = useBuiltinAgentLabel();
  if (!impact) return <p>{r("impact_unavailable")}</p>;
  const systemName = (name: string) => (agentKeyFor(name) === "generic" ? name : builtinLabel(agentKeyFor(name)));
  return (
    <div className={styles.impact}>
      {reportsWarning ? <p role="note">{r("reports_warning")}</p> : null}
      {lead || countAgents(impact) === 0 ? <p>{countAgents(impact) > 0 ? r("impact_lead") : r("impact_none")}</p> : null}
      {impact.system_agents.length > 0 ? (
        <>
          <h3>{r("impact_system", { count: impact.system_agents.length })}</h3>
          <ul>
            {impact.system_agents.map((agent) => (
              <li key={agent.name}>{systemName(agent.name)}</li>
            ))}
          </ul>
        </>
      ) : null}
      {impact.team_agents.length > 0 ? (
        <>
          <h3>{r("impact_team", { count: impact.team_agents.length })}</h3>
          <ul>
            {impact.team_agents.map((agent) => (
              <li key={agent.id}>{agent.display_name || agent.name}</li>
            ))}
          </ul>
        </>
      ) : null}
      <h3>{r("impact_schedules", { count: impact.scheduled_tasks.length })}</h3>
      {impact.scheduled_tasks.length > 0 ? (
        <ul>
          {impact.scheduled_tasks.map((job) => (
            <li key={job.id}>{job.name}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
