"use client";

/**
 * Built-in Tools on the Tools page, in the groups the Agent editor uses (`toolGroups`). Administrators get one
 * Team switch per Tool; members see a state only when the Team turned a Tool off. Turning on a Tool that writes
 * externally asks the administrator to acknowledge the risk; turning a Tool off previews who is affected.
 * Without the Team state (request failed) the list is read-only and states nothing about on or off.
 */
import { useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Dialog } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  fetchDisableImpact,
  setBuiltinToolTeamState,
  type BuiltinPluginState,
  type BuiltinToolState,
  type DisableImpact,
} from "@/lib/builtin-resources-api";
import type { Locale } from "@/lib/locale";
import { TOOL_DISPLAY_TABLE, toolDisplay, visibleBuiltinToolStates } from "@/lib/tool-display";
import { toast } from "@/lib/use-toast";
import { TOOL_GROUP_ORDER, toolGroups } from "@/components/ui/resource-picker";
import { ImpactBody } from "./plugin-state-list";
import styles from "./capability-center.module.css";

type ToolRow = BuiltinToolState & { label: string; blurb: string };

/** Every user-visible built-in Tool with its declared risk, for the read-only list shown without the Team state. */
function declaredTools(): BuiltinToolState[] {
  return Object.entries(TOOL_DISPLAY_TABLE)
    .filter(([, entry]) => entry.kind === "tool")
    .map(([name, entry]) => ({ name, risk: entry.risk ?? "read", enabled: true }));
}

/** The built-in Tool rows the Tools page lists (Team state, or declared Tools without it) and which match a search. */
export function builtinToolRows(
  tools: BuiltinToolState[] | null,
  googleWorkspace: boolean,
  locale: Locale,
  needle: string,
): { all: ToolRow[]; rows: ToolRow[] } {
  const all = visibleBuiltinToolStates(tools ?? declaredTools(), googleWorkspace).map((tool) => {
    const display = toolDisplay(tool.name, locale);
    return { ...tool, label: display.label, blurb: display.blurb };
  });
  return { all, rows: all.filter((tool) => `${tool.label} ${tool.blurb}`.toLocaleLowerCase().includes(needle)) };
}

/** "{shown} / {total}" while a search narrows a list, else the total, so a filtered count never reads as the whole. */
export function useHeadingCount() {
  const r = useTranslations("sidebar.centers.resources");
  return (shown: number, total: number, filtering: boolean) => (filtering ? r("filtered_count", { shown, total }) : String(total));
}

export function ToolStateList({
  tools,
  plugins,
  admin,
  googleWorkspace,
  needle,
  onChanged,
}: {
  /** Team state of the built-in Tools; null when it could not be loaded. */
  tools: BuiltinToolState[] | null;
  plugins: BuiltinPluginState[] | null;
  admin: boolean;
  googleWorkspace: boolean;
  needle: string;
  onChanged: () => void;
}) {
  const r = useTranslations("sidebar.centers.resources");
  const locale = useLocale() as Locale;
  const headingCount = useHeadingCount();
  const { confirm, dialog } = useConfirmDialog();
  const [busy, setBusy] = useState<string | null>(null);
  const [detailsFor, setDetailsFor] = useState<string | null>(null);
  const stateKnown = tools !== null;
  const canSwitch = admin && stateKnown;

  const groupOf = toolGroups((plugins ?? []).map((plugin) => ({ kind: "builtin" as const, id: plugin.name, tools: plugin.tools })));
  const { all, rows } = builtinToolRows(tools, googleWorkspace, locale, needle);

  const save = async (tool: BuiltinToolState, enabled: boolean, acknowledgeRisk: boolean) => {
    setBusy(tool.name);
    try {
      await setBuiltinToolTeamState(tool.name, { enabled, acknowledge_risk: acknowledgeRisk });
      onChanged();
    } catch {
      toast({ kind: "error", text: r("save_failed") });
    } finally {
      setBusy(null);
    }
  };

  const toggle = async (tool: ToolRow, enabled: boolean) => {
    if (enabled && tool.risk === "external_write") {
      const ok = await confirm({
        title: r("enable_risk_title", { name: tool.label }),
        description: (
          <ul className={styles.riskList}>
            <li>{r("enable_risk_irreversible")}</li>
            <li>{r("enable_risk_audit")}</li>
          </ul>
        ),
        confirmLabel: r("enable"),
        tone: "primary",
      });
      if (!ok) return;
      await save(tool, true, true);
      return;
    }
    if (!enabled) {
      const impact = await fetchDisableImpact("builtin_tool", tool.name).catch(() => null);
      const ok = await confirm({
        title: r("disable_title", { name: tool.label }),
        description: <ImpactBody impact={impact} />,
        confirmLabel: r("disable"),
        tone: "danger",
      });
      if (!ok) return;
    }
    await save(tool, enabled, false);
  };

  const detailsTool = detailsFor ? (rows.find((row) => row.name === detailsFor) ?? null) : null;
  const groups = TOOL_GROUP_ORDER.map((key) => ({ key, rows: rows.filter((row) => groupOf(row.name) === key) })).filter(
    (group) => group.rows.length > 0,
  );
  if (needle && rows.length === 0) return null;
  return (
    <section aria-label={r("builtin_tools")} className={styles.catalogSection}>
      <h2 className="ui-section-title">
        {r("builtin_tools")} <span className={styles.headingCount}>{headingCount(rows.length, all.length, Boolean(needle))}</span>
      </h2>
      {groups.map((group) => (
        <section key={group.key} aria-label={r(`groups.${group.key}`)} className={styles.toolGroup}>
          <h3 className={styles.groupTitle}>
            {r(`groups.${group.key}`)} <span className={styles.headingCount}>{group.rows.length}</span>
          </h3>
          <ul className={styles.list}>
            {group.rows.map((tool) => (
              <li key={tool.name} className={tool.enabled ? undefined : styles.rowOff}>
                <div>
                  <h4>
                    {tool.label}
                    <RiskTag risk={tool.risk} />
                  </h4>
                  <p>{tool.blurb}</p>
                </div>
                <div className={styles.rowState}>
                  {stateKnown ? (
                    <button
                      type="button"
                      className={styles.detailsButton}
                      aria-label={r("details_label", { name: tool.label })}
                      onClick={() => setDetailsFor(tool.name)}
                    >
                      {r("details")}
                    </button>
                  ) : null}
                  {canSwitch ? (
                    <Switch
                      checked={tool.enabled}
                      busy={busy === tool.name}
                      aria-label={r("tool_switch", { name: tool.label })}
                      onCheckedChange={(enabled) => void toggle(tool, enabled)}
                    />
                  ) : stateKnown && !tool.enabled ? (
                    <small>{r("team_off")}</small>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {detailsTool ? (
        <ToolDetails
          tool={detailsTool}
          admin={admin}
          busy={busy === detailsTool.name}
          onToggle={(enabled) => void toggle(detailsTool, enabled)}
          onClose={() => setDetailsFor(null)}
        />
      ) : null}
      {dialog}
    </section>
  );
}

/** The warning tag for Tools that write externally, destroy content or have an unknown risk; nothing for the rest. */
export function RiskTag({ risk }: { risk: string | null | undefined }) {
  const r = useTranslations("sidebar.centers.resources");
  if (risk !== "external_write" && risk !== "destructive" && risk !== "unknown") return null;
  return (
    <Badge tone="warning" size="sm" className={styles.badge}>
      {r(`risk.${risk}`)}
    </Badge>
  );
}

/** Tool details: what it does, its risk, and for administrators the Team switch and who uses it. */
function ToolDetails({
  tool,
  admin,
  busy,
  onToggle,
  onClose,
}: {
  tool: ToolRow;
  admin: boolean;
  busy: boolean;
  onToggle: (enabled: boolean) => void;
  onClose: () => void;
}) {
  const r = useTranslations("sidebar.centers.resources");
  const [impact, setImpact] = useState<DisableImpact | null | undefined>(undefined);
  useEffect(() => {
    if (!admin) return;
    let cancelled = false;
    void fetchDisableImpact("builtin_tool", tool.name)
      .catch(() => null)
      .then((value) => {
        if (!cancelled) setImpact(value);
      });
    return () => {
      cancelled = true;
    };
  }, [admin, tool.name]);
  return (
    <Dialog title={tool.label} onClose={onClose} variant="drawer">
      <div className={styles.details}>
        <p>{tool.blurb}</p>
        {tool.risk === "external_write" || tool.risk === "destructive" ? (
          <p>
            <RiskTag risk={tool.risk} />
          </p>
        ) : null}
        {admin ? (
          <>
            <label className={styles.switchLabel}>
              <Switch checked={tool.enabled} busy={busy} aria-label={r("tool_switch", { name: tool.label })} onCheckedChange={onToggle} />
              <span>{tool.enabled ? r("enabled") : r("not_enabled")}</span>
            </label>
            <p className={styles.hint}>{r("tool_off_effect")}</p>
            <h3>{r("tool_usage")}</h3>
            {impact === undefined ? null : <ImpactBody impact={impact} lead={false} />}
          </>
        ) : (
          <small>{tool.enabled ? r("enabled") : r("team_off")}</small>
        )}
      </div>
    </Dialog>
  );
}
