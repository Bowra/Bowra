/**
 * Shared Plugin picker and resource summary for Agent editing (Capability Center brief §4.10,
 * mockup "5 智能体选资源" E1 full / E2 compact).
 *
 * Built-in Plugins come first, then team Plugins (the team group is hidden when empty). Each row says
 * when to use it, gives an example and what it adds. Settings Plugins are never listed, and reasons
 * only an administrator can act on are shown only to administrators; a selected item that later
 * becomes unavailable stays selected with its reason so the owner can still clear it. Skills use `ResourceSkillPicker` (selected by exact `skill_ref.id`); built-in Tools use
 * `ResourceToolPicker`, grouped like the Tools page, with Tools a selected Plugin brings locked as included.
 */
"use client";

import { useLocale, useTranslations } from "next-intl";

import { CapabilityLink } from "@/components/capabilities/capability-link";
import { teamSettingsPath } from "@/lib/team-settings-paths";
import type { AgentAuthoringPluginLockedReason, AgentAuthoringPluginSection, AgentAuthoringSkill } from "@/lib/agents-api";
import { authoringSkillDescription, authoringSkillTitle } from "@/lib/skills-meta";
import { toolDisplay } from "@/lib/tool-display";

import styles from "./resource-picker.module.css";

export type PickerPluginKind = "builtin" | "team";

export interface PickerPlugin {
  kind: PickerPluginKind;
  /** `builtin:<name>` for built-in Plugins, `tcap_*` for team Plugins. */
  id: string;
  name: string;
  description: string;
  /** Reviewed "use when" line; team Plugins use their description instead. */
  useWhen: string | null;
  example: string | null;
  version: number | null;
  tools: string[];
  skillCount: number;
  guidance: boolean;
  selectable: boolean;
  lockedReason: AgentAuthoringPluginLockedReason | null;
  /** Tools that are not set up for the team yet; they explain a `not_ready` lock. */
  unavailableTools?: string[];
}

export function pluginsFromCatalog(section: AgentAuthoringPluginSection | undefined, locale: string): PickerPlugin[] {
  if (!section) return [];
  const builtin = section.builtin.map((item): PickerPlugin => {
    const listing = item.listing[locale] ?? item.listing["en-US"];
    return {
      kind: "builtin",
      id: item.id,
      name: listing?.name ?? item.name,
      description: listing?.description ?? "",
      useWhen: listing?.use_when ?? null,
      example: listing?.example ?? null,
      version: item.version,
      tools: item.tools,
      skillCount: item.family === "settings" ? 1 : 0,
      guidance: item.prompt_modules.length > 0,
      selectable: item.selectable,
      lockedReason: item.locked_reason,
      unavailableTools: item.unavailable_tools,
    };
  });
  const team = section.team.map((item): PickerPlugin => ({
    kind: "team",
    id: item.id,
    name: item.name,
    description: item.description,
    useWhen: item.description || null,
    example: null,
    version: item.version,
    tools: item.tools,
    skillCount: item.skill_count,
    guidance: false,
    selectable: item.selectable,
    lockedReason: item.locked_reason,
    unavailableTools: item.unavailable_tools,
  }));
  return [...builtin, ...team];
}

/** Reasons only a team administrator can act on; members never see Plugins locked for them. */
const ADMIN_ONLY_REASONS = new Set<AgentAuthoringPluginLockedReason>(["disabled_by_team", "not_ready"]);

/**
 * The Plugins this person may choose from (design §7.3 item 5). Settings Plugins are never listed;
 * Plugins the team disabled or has not set up are listed only for administrators. A Plugin already
 * selected stays visible with its reason so it can still be removed.
 */
export function visiblePlugins(plugins: PickerPlugin[], { canManage, selected }: { canManage: boolean; selected: ReadonlySet<string> }): PickerPlugin[] {
  return plugins.filter((plugin) => {
    if (selected.has(plugin.id)) return true;
    if (plugin.lockedReason === "settings_assistant_only") return false;
    return canManage || !plugin.lockedReason || !ADMIN_ONLY_REASONS.has(plugin.lockedReason);
  });
}

/** Whether a Plugin matches a search, by its name, purpose lines or the Tools it brings. */
export function pluginMatches(plugin: PickerPlugin, query: string, locale: string): boolean {
  const needle = query.trim().toLocaleLowerCase(locale);
  if (!needle) return true;
  const toolNames = plugin.tools.flatMap((ref) => [ref, toolDisplay(ref).label]);
  return [plugin.name, plugin.description, plugin.useWhen ?? "", plugin.example ?? "", ...toolNames].join(" ").toLocaleLowerCase(locale).includes(needle);
}

/** Built-in Plugins whose Tools act outside Bowhead without asking each time once selected (§7.4). */
const DIRECT_DELIVERY: Record<string, "consequence_email" | "consequence_google"> = {
  "builtin:email": "consequence_email",
  "builtin:google-delivery": "consequence_google",
};

/** Tool reference → name of the selected Plugin that brings it. */
export function pluginToolOwners(plugins: PickerPlugin[], selected: ReadonlySet<string>): Map<string, string> {
  const owners = new Map<string, string>();
  for (const plugin of plugins) {
    if (!selected.has(plugin.id)) continue;
    for (const tool of plugin.tools) if (!owners.has(tool)) owners.set(tool, plugin.name);
  }
  return owners;
}

export interface ResourcePluginPickerProps {
  plugins: PickerPlugin[];
  selected: ReadonlySet<string>;
  onToggle: (plugin: PickerPlugin) => void;
  disabled?: boolean;
  density?: "full" | "compact";
  /** Team administrators also see Plugins the team disabled or has not set up. */
  canManage?: boolean;
  /** One search over the sectioned list; Plugins that do not match are left out. */
  query?: string;
  /** Plugins the creation draft picked from the request (compact density only). */
  recommended?: ReadonlySet<string>;
}

/**
 * Display name for a picker Plugin. Built-in Plugins without a manifest listing (the settings
 * Plugins) fall back to the Capability Center's localized system Plugin name instead of the raw id.
 */
function usePluginName(): (plugin: PickerPlugin) => string {
  const centers = useTranslations("sidebar.centers");
  return (plugin) => {
    if (plugin.kind !== "builtin") return plugin.name;
    const raw = plugin.id.slice("builtin:".length);
    const key = `system_plugins.${raw}.name`;
    return plugin.name === raw && centers.has(key) ? centers(key) : plugin.name;
  };
}

/** "Adds: Run Python · guidance", or "Adds: Query data and 3 more Tools" for longer lists. */
function usePluginAdds(): (plugin: PickerPlugin) => string {
  const t = useTranslations("resource_picker");
  return (plugin) => {
    const names = plugin.tools.map((ref) => toolDisplay(ref).label);
    const tools = names.length > 2 ? t("adds_many", { first: names[0], count: names.length }) : names.join(t("list_separator"));
    const parts = [tools, plugin.skillCount ? t("skill_count", { count: plugin.skillCount }) : "", plugin.guidance ? t("with_guidance") : ""].filter(Boolean);
    return t("adds", { items: parts.join(" · ") });
  };
}

function PluginReason({ plugin, checked }: { plugin: PickerPlugin; checked: boolean }) {
  const t = useTranslations("resource_picker");
  if (!plugin.lockedReason) return null;
  const action = plugin.lockedReason === "settings_assistant_only" ? null : plugin.lockedReason === "not_ready" ? "action_configure" : "action_enable";
  // Email needs the team's mail service, which is set up in Team settings, not in the Capability Center.
  const needsEmailService = plugin.lockedReason === "not_ready" && Boolean(plugin.unavailableTools?.includes("send_email"));
  const href = needsEmailService
    ? teamSettingsPath("email-service")
    : plugin.lockedReason === "not_ready" ? "/capabilities/?type=tools" : "/capabilities/?type=capabilities";
  return (
    <small className={styles.reason}>
      {needsEmailService ? t("reason_email_service_missing") : t(`reason_${plugin.lockedReason}`)}
      {checked ? ` · ${t("remove_hint")}` : action ? (
        <>
          {" · "}
          <CapabilityLink href={href}>{t(action)}</CapabilityLink>
        </>
      ) : null}
    </small>
  );
}

export function ResourcePluginPicker({
  plugins,
  selected,
  onToggle,
  disabled = false,
  density = "full",
  canManage = false,
  query = "",
  recommended,
}: ResourcePluginPickerProps) {
  const t = useTranslations("resource_picker");
  const locale = useLocale();
  const pluginName = usePluginName();
  const pluginAdds = usePluginAdds();
  const visible = visiblePlugins(plugins, { canManage, selected }).filter((plugin) => pluginMatches(plugin, query, locale));
  if (density === "compact") {
    const picked = visible.filter((plugin) => selected.has(plugin.id));
    const open = visible.filter((plugin) => !selected.has(plugin.id) && plugin.selectable);
    return (
      <div className={styles.compact}>
        {picked.length ? (
          <ul className={styles.compactList} aria-label={t("selected_plugins")}>
            {picked.map((plugin) => (
              <li key={plugin.id} className={styles.compactRow}>
                <span className={styles.body}>
                  <strong>
                    {pluginName(plugin)}
                    {recommended?.has(plugin.id) ? <span className={styles.badge}>{t("recommended")}</span> : null}
                  </strong>
                  {plugin.useWhen ? <small>{t("use_when", { text: plugin.useWhen })}</small> : null}
                  <small>{pluginAdds(plugin)}</small>
                  {DIRECT_DELIVERY[plugin.id] ? <small className={styles.consequence}>{t(DIRECT_DELIVERY[plugin.id])}</small> : null}
                  <PluginReason plugin={plugin} checked />
                </span>
              </li>
            ))}
          </ul>
        ) : null}
        {open.length ? (
          <div className={styles.chips} role="group" aria-label={t("plugins")}>
            {open.map((plugin) => (
              <button
                key={plugin.id}
                type="button"
                className={styles.chip}
                aria-pressed={false}
                data-selected={false}
                disabled={disabled}
                title={plugin.useWhen ?? plugin.description}
                onClick={() => onToggle(plugin)}
              >
                {t("add_named", { name: pluginName(plugin) })}
              </button>
            ))}
          </div>
        ) : null}
      </div>
    );
  }
  const groups: Array<[PickerPluginKind, PickerPlugin[]]> = (
    [
      ["builtin", visible.filter((plugin) => plugin.kind === "builtin")],
      ["team", visible.filter((plugin) => plugin.kind === "team")],
    ] as Array<[PickerPluginKind, PickerPlugin[]]>
  ).filter(([, items]) => items.length > 0);
  if (!groups.length) {
    return (
      <div className={styles.emptyState} role="status">
        <strong>{query.trim() ? t("no_match_title", { query: query.trim() }) : t("none_title")}</strong>
        <span>{query.trim() ? t("no_match_body") : t("none_body")}</span>
      </div>
    );
  }
  return (
    <div className={styles.picker}>
      {groups.map(([kind, items]) => (
        <section key={kind} className={styles.group} aria-label={t(kind === "builtin" ? "builtin_plugins" : "team_plugins")}>
          <h4 className={styles.groupTitle}>{t(kind === "builtin" ? "builtin_plugins" : "team_plugins")}</h4>
          <div className={styles.grid}>
            {items.map((plugin) => {
              const checked = selected.has(plugin.id);
              const locked = !plugin.selectable && !checked;
              return (
                <label key={plugin.id} className={styles.card} data-selected={checked} data-disabled={locked}>
                  <input type="checkbox" name="agent-plugin" checked={checked} disabled={disabled || locked} onChange={() => onToggle(plugin)} />
                  <span className={styles.body}>
                    <strong>{pluginName(plugin)}</strong>
                    {plugin.useWhen ? (
                      <small>{plugin.example ? t("use_when_example", { text: plugin.useWhen, example: plugin.example }) : t("use_when", { text: plugin.useWhen })}</small>
                    ) : null}
                    <small>{pluginAdds(plugin)}</small>
                    {checked && DIRECT_DELIVERY[plugin.id] ? <small className={styles.consequence}>{t(DIRECT_DELIVERY[plugin.id])}</small> : null}
                    <PluginReason plugin={plugin} checked={checked} />
                  </span>
                </label>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}

export interface ResourceSummaryProps {
  tools: number;
  skills: number;
  needsConfirmation: number;
  guidance: string[];
}

export function ResourceSummary({ tools, skills, needsConfirmation, guidance }: ResourceSummaryProps) {
  const t = useTranslations("resource_picker");
  const locale = useLocale();
  const list = new Intl.ListFormat(locale, { style: "short", type: "conjunction" });
  return (
    <aside className={styles.summary} aria-label={t("summary_title")}>
      <h4>{t("summary_title")}</h4>
      <dl>
        <div>
          <dt>{t("summary_tools")}</dt>
          <dd>{tools}</dd>
        </div>
        <div>
          <dt>{t("summary_skills")}</dt>
          <dd>{skills}</dd>
        </div>
        <div data-tone={needsConfirmation ? "warning" : undefined}>
          <dt>{t("summary_confirm")}</dt>
          <dd>{needsConfirmation}</dd>
        </div>
      </dl>
      {guidance.length ? (
        <p className={styles.guidance}>
          <strong>{t("summary_guidance")}</strong>
          <span>{list.format(guidance)}</span>
        </p>
      ) : null}
      <p className={styles.note}>{t("summary_note")}</p>
    </aside>
  );
}

export interface ResourceSkillPickerProps {
  skills: AgentAuthoringSkill[];
  /** Selected exact Skill ids (`skill_ref.id`). */
  selected: ReadonlySet<string>;
  onToggle: (skill: AgentAuthoringSkill) => void;
  disabled?: boolean;
}

export function ResourceSkillPicker({ skills, selected, onToggle, disabled = false }: ResourceSkillPickerProps) {
  const t = useTranslations("resource_picker");
  const locale = useLocale();
  return (
    <section className={styles.group} aria-label={t("skills")}>
      <h4 className={styles.groupTitle}>{t("skills")}</h4>
      {skills.length ? (
        <div className={styles.grid}>
          {skills.map((skill) => {
            const checked = selected.has(skill.grant.skill_ref.id);
            const name = authoringSkillTitle(skill, locale);
            const description = authoringSkillDescription(skill, locale);
            return (
              <label key={`${skill.source_kind}:${skill.grant.skill_ref.id}`} className={styles.card} data-selected={checked}>
                <input type="checkbox" name="agent-skill" checked={checked} disabled={disabled} onChange={() => onToggle(skill)} />
                <span className={styles.body}>
                  <strong>{name}</strong>
                  {description ? <small>{description}</small> : null}
                </span>
              </label>
            );
          })}
        </div>
      ) : (
        <p className={styles.empty}>{t("skills_empty")}</p>
      )}
    </section>
  );
}

/** Tools-page groups in mockup order (brief §4.5). Google delivery and Email share "Delivery". */
export const TOOL_GROUP_ORDER = ["data", "reports", "designs", "knowledge", "web", "compute", "media", "delivery", "skills"] as const;
export type ToolGroupKey = (typeof TOOL_GROUP_ORDER)[number];

export const TOOL_GROUP_OF_PLUGIN: Record<string, ToolGroupKey> = {
  "data-analysis": "data",
  "reports-canvas": "reports",
  designs: "designs",
  "knowledge-base": "knowledge",
  "web-research": "web",
  "code-computation": "compute",
  "image-generation": "media",
  "google-delivery": "delivery",
  email: "delivery",
};

/** Category fallback when the built-in Plugin section is unavailable (switch off or older server). */
const TOOL_GROUP_OF_CATEGORY: Record<string, ToolGroupKey> = {
  bi: "data",
  canvas: "reports",
  kb: "knowledge",
  document: "knowledge",
  web: "web",
  compute: "compute",
  media: "media",
  orchestrate: "delivery",
};

export interface PickerTool {
  ref: string;
  name: string;
  description: string;
  selectable: boolean;
  /** Localized reason shown when the Tool cannot be selected. */
  lockedLabel?: string | null;
  /** Localized risk tag, e.g. "Writes externally". */
  riskLabel?: string | null;
}

/** Tool reference → Tools-page group, from built-in Plugin membership with a category fallback. */
export function toolGroups(plugins: PickerPlugin[]): (ref: string) => ToolGroupKey {
  const byPlugin = new Map<string, ToolGroupKey>();
  for (const plugin of plugins) {
    if (plugin.kind !== "builtin") continue;
    const group = TOOL_GROUP_OF_PLUGIN[plugin.id.replace(/^builtin:/, "")];
    if (group) for (const tool of plugin.tools) byPlugin.set(tool, group);
  }
  return (ref) => byPlugin.get(ref) ?? TOOL_GROUP_OF_CATEGORY[toolDisplay(ref).category] ?? "skills";
}

export interface ResourceToolPickerProps {
  tools: PickerTool[];
  plugins: PickerPlugin[];
  selected: ReadonlySet<string>;
  selectedPluginIds: ReadonlySet<string>;
  onToggle: (ref: string) => void;
  disabled?: boolean;
  /** Also show the Tool identifier (the value written into `tool_refs`), useful for support. */
  showRef?: boolean;
}

/**
 * Built-in Tools grouped like the Tools page. A Tool a selected Plugin brings is shown as included in
 * that Plugin and cannot be removed on its own; a selected Tool that became unavailable stays removable.
 */
export function ResourceToolPicker({
  tools,
  plugins,
  selected,
  selectedPluginIds,
  onToggle,
  disabled = false,
  showRef = false,
}: ResourceToolPickerProps) {
  const t = useTranslations("resource_picker");
  const r = useTranslations("sidebar.centers.resources");
  const owners = pluginToolOwners(plugins, selectedPluginIds);
  const groupOf = toolGroups(plugins);
  const groups = TOOL_GROUP_ORDER.map((key) => ({ key, items: tools.filter((tool) => groupOf(tool.ref) === key) })).filter(
    (group) => group.items.length > 0,
  );
  return (
    <div className={styles.picker}>
      {groups.map((group) => (
        <section key={group.key} className={styles.group} aria-label={r(`groups.${group.key}`)}>
          <h4 className={styles.groupTitle}>
            {r(`groups.${group.key}`)} <span className={styles.version}>{group.items.length}</span>
          </h4>
          <div className={styles.grid}>
            {group.items.map((tool) => {
              const owner = owners.get(tool.ref);
              const checked = selected.has(tool.ref) || owner !== undefined;
              const locked = owner !== undefined || (!tool.selectable && !selected.has(tool.ref));
              return (
                <label key={tool.ref} className={styles.card} data-selected={checked} data-disabled={locked}>
                  <input type="checkbox" name="agent-tool" checked={checked} disabled={disabled || locked} onChange={() => onToggle(tool.ref)} />
                  <span className={styles.body}>
                    <strong>{tool.name}</strong>
                    {showRef ? <code className={styles.ref}>{tool.ref}</code> : null}
                    <small>{tool.description}</small>
                    {tool.riskLabel ? <small>{tool.riskLabel}</small> : null}
                    {owner !== undefined ? (
                      <small className={styles.reason}>{t("included_in", { name: owner })}</small>
                    ) : tool.lockedLabel ? (
                      <small className={styles.reason}>
                        {tool.lockedLabel}
                        {selected.has(tool.ref) ? ` · ${t("remove_hint")}` : ""}
                      </small>
                    ) : null}
                  </span>
                </label>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
