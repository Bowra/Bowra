"use client";

/**
 * Team settings page for system-built-in Agent configuration.
 *
 * Read only the system manifest and this team's overrides; do not load or display Agents
 * from other sources such as custom creation, sharing or marketplace hiring. The Agent management center handles custom Agent creation and Owner configuration.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { AgentRunLimitsSettings } from "@/components/settings/agent-run-limits";
import { ExecutionLimitsSettings } from "@/components/settings/execution-limits";
import { BuiltinSubagentCards } from "@/components/settings/builtin-subagents-section";
import { Button } from "@/components/ui/button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { SaveBarSlotContext } from "@/components/ui/save-bar";
import { SettingsShell } from "@/components/settings/settings-shell";
import { Tab, TabList, TabPanel, Tabs } from "@/components/ui/tabs";
import { getAgentRunLimits, type AgentRunLimits } from "@/lib/agent-run-limits-api";
import { useAppRouter } from "@/lib/use-app-router";
import {
  listBuiltinSubagents,
  type BuiltinSubagent,
} from "@/lib/team-subagents-api";

function EmptyState() {
  const t = useTranslations("settings.subagents");
  return (
    <div className="builtin-agents-empty">
      <h2>{t("builtin_empty_title")}</h2>
      <p>{t("builtin_empty_body")}</p>
    </div>
  );
}

function BuiltinSubagentsManagement({ embedded = false }: { embedded?: boolean }) {
  const t = useTranslations("settings.subagents");
  const common = useTranslations("common");
  const [items, setItems] = useState<BuiltinSubagent[]>([]);
  const [manageable, setManageable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await listBuiltinSubagents();
      setItems(result.items);
      setManageable(result.manageable);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className={embedded ? undefined : "builtin-agents-settings"}>
      {embedded ? (
        <p className="builtin-agents-subtitle">{t("subtitle")}</p>
      ) : (
        <header className="builtin-agents-head">
          <h1 className="ui-page-title">{t("title")}</h1>
          <p className="builtin-agents-subtitle">{t("subtitle")}</p>
        </header>
      )}

      {error ? (
        <Notice tone="error">
          <span>{error}</span>
          <Button variant="secondary" onClick={() => void refresh()} disabled={loading}>
            {common("retry")}
          </Button>
        </Notice>
      ) : null}

      {loading ? (
        <LoadingSkeleton label={t("loading")} />
      ) : error ? null : (
        <>
          <div className="builtin-agents-section-head">
            <h2>{t("catalog_title")}</h2>
            <p>
              <span>{t("count", { count: items.length })}</span>
              <span>{t("enabled_count", { count: items.filter((item) => item.enabled).length })}</span>
              <span>{t("customized_count", { count: items.filter((item) => item.overridden_fields.length > 0).length })}</span>
            </p>
          </div>
          {items.length === 0 ? <EmptyState /> : (
            <BuiltinSubagentCards
              items={items}
              manageable={manageable}
              onItemChanged={(next) =>
                setItems((current) => current.map((item) => (item.name === next.name ? next : item)))}
              onItemRemoved={(name) =>
                setItems((current) => current.filter((item) => item.name !== name))}
            />
          )}
          <p className="builtin-agents-save-note">{t("live_save_notice")}</p>
        </>
      )}
    </div>
  );
}

type AgentsTab = "builtin" | "run_limits";

/** Team runtime policy stays available independently of the legacy loop-limits switch. */
function AgentsSettings() {
  const t = useTranslations("settings.subagents");
  const [tab, setTab] = useState<AgentsTab>("builtin");
  const [runLimits, setRunLimits] = useState<AgentRunLimits | null | undefined>(undefined);
  const [runLimitsFailed, setRunLimitsFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getAgentRunLimits(controller.signal)
      .then((value) => { if (!controller.signal.aborted) setRunLimits(value); })
      .catch(() => { if (!controller.signal.aborted) { setRunLimits(null); setRunLimitsFailed(true); } });
    return () => controller.abort();
  }, []);

  // Decide the layout once, so the built-in list mounts (and loads) only once.
  if (runLimits === undefined) return <LoadingSkeleton label={t("loading")} />;
  const tabs: Array<[AgentsTab, string]> = [
    ["builtin", t("tab_builtin")],
    ["run_limits", t("tab_run_limits")],
  ];
  return (
    <div className="builtin-agents-settings">
      <header className="builtin-agents-head">
        <h1 className="ui-page-title">{t("title")}</h1>
      </header>
      <Tabs value={tab} onValueChange={setTab}>
        <TabList appearance="underline" className="builtin-agents-tabs">
          {tabs.map(([id, label]) => (
            <Tab className={tab === id ? "is-active" : undefined} key={id} value={id}>
              {label}
            </Tab>
          ))}
        </TabList>
        <TabPanel value="builtin" as="section" keepMounted>
          <BuiltinSubagentsManagement embedded />
        </TabPanel>
        <TabPanel value="run_limits" as="section" keepMounted>
          <SaveBarSlotContext.Provider value={null}><div className="runtime-limits-settings">
            {runLimits ? <AgentRunLimitsSettings initial={runLimits} /> : runLimitsFailed ? <Notice tone="error">{t("run_limits_load_failed")}</Notice> : null}
            <ExecutionLimitsSettings />
          </div></SaveBarSlotContext.Provider>
        </TabPanel>
      </Tabs>
    </div>
  );
}

export default function SubagentsSettingsPage() {
  const t = useTranslations("settings.subagents");
  const router = useAppRouter();
  const [redirecting, setRedirecting] = useState(false);

  useEffect(() => {
    const agentId = new URLSearchParams(window.location.search).get("agent_id");
    if (!agentId) return;
    setRedirecting(true);
    router.replace(`/agents/manage/${window.location.search}${window.location.hash}`);
  }, [router]);

  if (redirecting) return <div aria-busy="true" />;
  return (
    <SettingsShell active="subagents" title={t("title")}>
      <AgentsSettings />
    </SettingsShell>
  );
}
