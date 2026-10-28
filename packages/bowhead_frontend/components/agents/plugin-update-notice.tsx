/**
 * Plugin update notice and confirmation note (Capability Center brief §4.12, mockup "5" E4/E5).
 *
 * Published Agents follow their Plugins' latest version (PRD §7.4); when a Plugin changed after the
 * release, the owner is told which one and from which version, and offered a re-evaluation. The
 * confirmation note is neutral information and never blocks release.
 *
 * Skills follow the same rule (Agent Skill gates design §3.2): runs use the latest approved Skill,
 * the release keeps the exact revision, and the notice names the updated Skills.
 */
"use client";

import { useLocale, useTranslations } from "next-intl";

import type { AgentPluginUpdate, AgentSkillUpdate } from "@/lib/agents-api";

function formatDay(value: string | null, locale: string): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? null : new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(parsed);
}

export function PluginUpdateNotice({ updates, skillUpdates, onReevaluate }: {
  updates: AgentPluginUpdate[] | undefined;
  skillUpdates?: AgentSkillUpdate[] | undefined;
  onReevaluate?: () => void;
}) {
  const t = useTranslations("resource_diff");
  const locale = useLocale();
  const skillNames = [...new Set((skillUpdates ?? []).map((update) => update.name).filter(Boolean))];
  if (!updates?.length && !skillNames.length) return null;
  const lines = (updates ?? []).map((update) => {
    const date = formatDay(update.changed_at, locale);
    const values = { name: update.name, from: update.release_version, to: update.current_version };
    return date ? t("update_line_dated", { ...values, date }) : t("update_line", values);
  });
  if (skillNames.length) {
    const names = new Intl.ListFormat(locale, { style: "long", type: "conjunction" }).format(skillNames);
    lines.push(t("skill_update_line", { count: skillNames.length, names }));
  }
  return (
    <div className="am-notice" role="status" data-tone="info">
      <span>
        {lines.join(" ")} {t("update_suggest")}
      </span>
      {onReevaluate ? (
        <button type="button" onClick={onReevaluate}>
          {t("reevaluate")}
        </button>
      ) : null}
    </div>
  );
}
