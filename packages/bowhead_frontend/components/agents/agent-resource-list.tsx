/**
 * Market details: the Plugins and Tools an Agent uses (Capability Center brief §4.12, mockup "5" E5).
 * Items the viewer cannot use show why. Display only; running the Agent still checks access itself.
 */
"use client";

import { useLocale, useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { pluginLabel } from "@/components/ui/resource-diff";
import type { AgentConsumerResources } from "@/lib/agents-api";
import type { Locale } from "@/lib/locale";
import { toolDisplay } from "@/lib/tool-display";

export function AgentResourceList({ resources }: { resources: AgentConsumerResources }) {
  const t = useTranslations("resource_diff");
  const locale = useLocale();
  const rows = [
    ...resources.plugins.map((plugin) => ({
      key: `plugin:${plugin.id}`,
      name: pluginLabel(plugin, locale),
      kind: plugin.version ? `${t("resources_plugin")} · ${t("version", { version: plugin.version })}` : t("resources_plugin"),
      reason: plugin.unavailable_reason,
    })),
    ...resources.tools.map((tool) => ({
      key: `tool:${tool.ref}`,
      name: toolDisplay(tool.ref, locale as Locale).label,
      kind: t("resources_tool"),
      reason: tool.unavailable_reason,
    })),
  ];
  return (
    <section className="am-quality-evidence" aria-labelledby="agent-resources-title">
      <div className="am-detail-section-heading">
        <h2 id="agent-resources-title">{t("resources_title")}</h2>
      </div>
      {rows.length ? (
        <ul className="am-resource-list">
          {rows.map((row) => (
            <li key={row.key}>
              <span>{row.name}</span>
              <small>{row.kind}</small>
              {row.reason ? (
                <Badge tone="neutral" size="sm">
                  {t(`reason_${row.reason}`)}
                </Badge>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p>{t("resources_empty")}</p>
      )}
    </section>
  );
}
