"use client";

/**
 * Built-in Tools grouped like the mockup (design brief §4.5–§4.7): one enable switch per Tool for
 * administrators, read-only state for members. Turning on a Tool that writes externally asks the
 * administrator to confirm the risk; turning a Tool off previews who is affected.
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
import { toolDisplay, visibleBuiltinToolStates } from "@/lib/tool-display";
import { toast } from "@/lib/use-toast";
import { TOOL_GROUP_OF_PLUGIN as GROUP_OF_PLUGIN, TOOL_GROUP_ORDER as GROUP_ORDER } from "@/components/ui/resource-picker";
import { ImpactBody } from "./plugin-state-list";
import styles from "./capability-center.module.css";

export function ToolStateList({
  tools,
  plugins,
  admin,
  googleWorkspace,
  needle,
  onChanged,
}: {
  tools: BuiltinToolState[];
  plugins: BuiltinPluginState[];
  admin: boolean;
  googleWorkspace: boolean;
  needle: string;
  onChanged: () => void;
}) {
  const r = useTranslations("sidebar.centers.resources");
  const locale = useLocale() as Locale;
  const { confirm, dialog } = useConfirmDialog();
  const [busy, setBusy] = useState<string | null>(null);
  const [detailsFor, setDetailsFor] = useState<string | null>(null);

  const groupOf = new Map<string, string>();
  for (const plugin of plugins) {
    const group = GROUP_OF_PLUGIN[plugin.name];
    if (group) for (const tool of plugin.tools) groupOf.set(tool, group);
  }
  const visible = visibleBuiltinToolStates(tools, googleWorkspace);
  const rows = visible
    .map((tool) => {
      const display = toolDisplay(tool.name, locale);
      return { ...tool, label: display.label, blurb: display.blurb, group: groupOf.get(tool.name) ?? "skills" };
    })
    .filter((tool) => `${tool.label} ${tool.blurb}`.toLocaleLowerCase().includes(needle));

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

  const toggle = async (tool: BuiltinToolState & { label: string }, enabled: boolean) => {
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
  const groups = GROUP_ORDER.map((key) => ({ key, rows: rows.filter((row) => row.group === key) })).filter((group) => group.rows.length > 0);
  return (
    <section aria-label={r("builtin_tools")}>
      <h2 className="ui-section-title">
        {r("builtin_tools")}{" "}
        <span className={styles.headingCount}>{visible.length}</span>
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
                  <h3>
                    {tool.label}
                    {tool.risk === "external_write" || tool.risk === "destructive" ? (
                      <Badge tone="warning" size="sm" className={styles.badge}>
                        {r(`risk.${tool.risk}`)}
                      </Badge>
                    ) : null}
                  </h3>
                  <p>{tool.blurb}</p>
                </div>
                <div className={styles.meta}>
                  <button
                    type="button"
                    className={styles.detailsButton}
                    aria-label={r("details_label", { name: tool.label })}
                    onClick={() => setDetailsFor(tool.name)}
                  >
                    {r("details")}
                  </button>
                  {admin ? (
                    <Switch
                      checked={tool.enabled}
                      busy={busy === tool.name}
                      aria-label={r("tool_switch", { name: tool.label })}
                      onCheckedChange={(enabled) => void toggle(tool, enabled)}
                    />
                  ) : (
                    <small>{tool.enabled ? r("enabled") : r("not_enabled")}</small>
                  )}
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

/**
 * Tool details (mockup file 3, C4): what it does, its risk, who uses it (administrators), the enable
 * switch and the stricter-only "Always confirm" override. The override is saved now but only takes
 * effect once per-call confirmation ships, and the panel says so.
 */
function ToolDetails({
  tool,
  admin,
  busy,
  onToggle,
  onClose,
}: {
  tool: BuiltinToolState & { label: string; blurb: string };
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
  const risky = tool.risk === "external_write" || tool.risk === "destructive";
  return (
    <Dialog title={tool.label} onClose={onClose} variant="drawer">
      <div className={styles.details}>
        <p>{tool.blurb}</p>
        {risky ? (
          <p>
            <Badge tone="warning" size="sm">
              {r(`risk.${tool.risk}`)}
            </Badge>
          </p>
        ) : null}
        {admin ? (
          <>
            <label className={styles.switchLabel}>
              <Switch checked={tool.enabled} busy={busy} aria-label={r("tool_switch", { name: tool.label })} onCheckedChange={onToggle} />
              <span>{tool.enabled ? r("enabled") : r("not_enabled")}</span>
            </label>
            <h3>{r("tool_usage")}</h3>
            {impact === undefined ? null : <ImpactBody impact={impact} lead={false} />}
          </>
        ) : (
          <small>{tool.enabled ? r("enabled") : r("not_enabled")}</small>
        )}
      </div>
    </Dialog>
  );
}
