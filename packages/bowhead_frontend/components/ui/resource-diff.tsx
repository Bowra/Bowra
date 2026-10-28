/**
 * Resource change view for release review and version comparison (Capability Center brief §4.11,
 * mockup "5 智能体选资源" E3/E4): additions and removals grouped by resource type. Plugin rows can
 * expand to the Tools they bring or take away. Renders nothing when no resource changed.
 */
"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import type { AgentPluginItem } from "@/lib/agents-api";
import { toolDisplay } from "@/lib/tool-display";
import type { Locale } from "@/lib/locale";

import styles from "./resource-diff.module.css";

export type ResourceChange = "added" | "removed";

export interface ResourceDiffInput {
  plugins?: Array<AgentPluginItem & { change: ResourceChange }>;
  tools?: { added: string[]; removed: string[] };
  skills?: { added: string[]; removed: string[] };
}

export function pluginLabel(plugin: Pick<AgentPluginItem, "listing" | "name">, locale: string): string {
  const listing = plugin.listing[locale as Locale] ?? plugin.listing["en-US"];
  return listing?.name ?? plugin.name;
}

function Row({ change, name, kind, children }: { change: ResourceChange; name: string; kind: string; children?: React.ReactNode }) {
  return (
    <li className={styles.row} data-change={change}>
      <span className={styles.sign} aria-hidden="true">
        {change === "added" ? "+" : "−"}
      </span>
      <span className={styles.name}>{name}</span>
      <span className={styles.kind}>{kind}</span>
      {children}
    </li>
  );
}

function PluginRow({ plugin }: { plugin: AgentPluginItem & { change: ResourceChange } }) {
  const t = useTranslations("resource_diff");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const kind = [plugin.kind === "builtin" ? t("builtin_plugin") : t("team_plugin"), plugin.version ? t("version", { version: plugin.version }) : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <>
      <Row change={plugin.change} name={pluginLabel(plugin, locale)} kind={kind}>
        {plugin.tools.length ? (
          <button type="button" className={styles.toggle} aria-expanded={open} onClick={() => setOpen((value) => !value)}>
            {t(plugin.change === "added" ? "brings" : "takes_away", { count: plugin.tools.length })}
          </button>
        ) : null}
      </Row>
      {open ? (
        <li className={styles.sub}>{plugin.tools.map((tool) => toolDisplay(tool, locale as Locale).label).join(t("list_separator"))}</li>
      ) : null}
    </>
  );
}

export function hasResourceChanges(input: ResourceDiffInput): boolean {
  return Boolean(
    input.plugins?.length || input.tools?.added.length || input.tools?.removed.length || input.skills?.added.length || input.skills?.removed.length,
  );
}

export function ResourceDiff({ input }: { input: ResourceDiffInput }) {
  const t = useTranslations("resource_diff");
  const locale = useLocale();
  if (!hasResourceChanges(input)) return null;
  const tools = [
    ...(input.tools?.added ?? []).map((ref) => ["added", ref] as const),
    ...(input.tools?.removed ?? []).map((ref) => ["removed", ref] as const),
  ];
  const skills = [
    ...(input.skills?.added ?? []).map((name) => ["added", name] as const),
    ...(input.skills?.removed ?? []).map((name) => ["removed", name] as const),
  ];
  return (
    <div className={styles.diff} aria-label={t("title")}>
      {input.plugins?.length ? (
        <section>
          <h3>{t("plugins")}</h3>
          <ul>
            {input.plugins.map((plugin) => (
              <PluginRow key={`${plugin.change}:${plugin.id}`} plugin={plugin} />
            ))}
          </ul>
        </section>
      ) : null}
      {tools.length ? (
        <section>
          <h3>{t("tools")}</h3>
          <ul>
            {tools.map(([change, ref]) => (
              <Row key={`${change}:${ref}`} change={change} name={toolDisplay(ref, locale as Locale).label} kind={t("tool")} />
            ))}
          </ul>
        </section>
      ) : null}
      {skills.length ? (
        <section>
          <h3>{t("skills")}</h3>
          <ul>
            {skills.map(([change, name]) => (
              <Row key={`${change}:${name}`} change={change} name={name} kind={t("skill")} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
