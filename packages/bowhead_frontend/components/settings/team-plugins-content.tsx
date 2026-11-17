"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { codedErrorMessage, withHttpFailureReason } from "@/lib/user-facing-error";
import { useLocale, useTranslations } from "next-intl";

import { ConfirmDialog, useConfirmDialog } from "@/components/ui/confirm-dialog";
import { Button } from "@/components/ui/button";
import { SaveBar } from "@/components/ui/save-bar";
import { CanvasToolbarActions, canvasCloseGuardOptions } from "@/components/canvas/object";
import { CapabilityLink } from "@/components/capabilities/capability-link";
import { ResourceGroup, ResourceToolPicker, pluginsFromCatalog, toolGroups, type PickerTool, type ToolGroupKey } from "@/components/ui/resource-picker";
import { getAgentAuthoringCatalog, type AgentAuthoringCatalog, type AgentAuthoringTool, type AgentExactSkillGrant } from "@/lib/agents-api";
import { groupToolConnections } from "@/lib/agent-tool-connections";
import { authoringToolMatches, toolSelectionLockedReasonKey } from "@/lib/agent-tool-selection";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { settingsTabHref } from "@/lib/settings-destinations";
import {
  createTeamPlugin,
  deleteTeamPlugin,
  listTeamPluginTemplates,
  listTeamPlugins,
  setTeamPluginEnabled,
  updateTeamPlugin,
  type TeamPlugin,
  type TeamPluginTemplate,
} from "@/lib/team-plugins-api";
import { localizedToolText, toolDisplay } from "@/lib/tool-display";
import { authoringSkillDescription, authoringSkillMatches, authoringSkillTitle } from "@/lib/skills-meta";
import type { CoachEditorPort, CoachEditorSnapshot } from "@/lib/coach-canvas-target";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import styles from "@/app/(app)/team/settings/plugins/page.module.css";

type HostedPluginEditor = {
  plugin: TeamPlugin;
  readOnly: boolean;
  controllerRef: { current: CoachEditorPort | null };
  onClose: () => void;
  onActivate: () => void;
  onSaved: (plugin: TeamPlugin) => void;
};

/** Read-only view without the authoring catalog: built-in tools by display name, team tools by operation, never a raw reference. */
function readableToolRef(ref: string): string {
  if (ref.startsWith("teammcp.")) return ref.slice(ref.lastIndexOf(".") + 1) || ref;
  return toolDisplay(ref).label;
}

/** Keys of everything a Plugin selects, so the groups holding them start open. */
function selectionKeys(plugin: TeamPlugin | undefined): ReadonlySet<string> {
  return new Set([...(plugin?.tool_refs ?? []), ...(plugin?.skill_grants ?? []).map((grant) => grant.skill_ref.id)]);
}

const NO_PLUGINS: ReadonlySet<string> = new Set();

/** Template kinds with reviewed copy; a kind the frontend does not know is not offered. */
const TEMPLATE_KINDS: ReadonlySet<string> = new Set(["shopify_analysis", "shopify_content"]);

interface FormContents {
  name: string;
  description: string;
  tools: string[];
  skills: AgentExactSkillGrant[];
}

const BLANK_FORM: FormContents = { name: "", description: "", tools: [], skills: [] };

/** Read-only contents without the authoring catalog: Team Tools, built-in Tools and Skills by readable name. */
function ReadOnlyResources({ tools, skills }: { tools: string[]; skills: AgentExactSkillGrant[] }) {
  const t = useTranslations("settings.team_capabilities");
  const picker = useTranslations("resource_picker");
  const teamTools = tools.filter((ref) => ref.startsWith("teammcp."));
  const builtinTools = tools.filter((ref) => !ref.startsWith("teammcp."));
  const groups: Array<[string, string[]]> = [
    [t("readonly_team_tools"), teamTools.map(readableToolRef)],
    [t("readonly_builtin_tools"), builtinTools.map(readableToolRef)],
    [picker("skills"), skills.map((grant) => grant.skill_ref.name)],
  ];
  return <>{groups.filter(([, names]) => names.length).map(([title, names]) => <div key={title} className={styles.readOnlyGroup}>
    <h4>{title}</h4>
    <ul>{names.map((name, index) => <li key={`${name}:${index}`}>{name}</li>)}</ul>
  </div>)}</>;
}

// Coded plugin failures carry localized catalog copy from the gateway.
const PLUGIN_ERROR_CODES = ["capability_"] as const;

export function TeamPluginsContent({ hosted }: { hosted?: HostedPluginEditor }) {
  const hostedMode = hosted !== undefined;
  const original = useRef(hosted?.plugin);
  const mounted = useRef(true);
  const intent = useRef(0);
  const approved = useRef<number | null>(null);
  const { confirm, dialog: navigationDialog } = useConfirmDialog();
  const nav = useTranslations("sidebar.centers");
  const t = useTranslations("settings.team_capabilities");
  const picker = useTranslations("resource_picker");
  const common = useTranslations("common");
  const tObject = useTranslations("canvas_object");
  const [saveFailed, setSaveFailed] = useState(false);
  const catalogRequest = useRef(0);
  const listRequest = useRef(0);
  const [items, setItems] = useState<TeamPlugin[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [catalog, setCatalog] = useState<AgentAuthoringCatalog | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [selected, setSelected] = useState<TeamPlugin | null>(original.current ?? null);
  const [editing, setEditing] = useState(hostedMode);
  const [name, setName] = useState(original.current?.name ?? "");
  const [description, setDescription] = useState(original.current?.description ?? "");
  const [tools, setTools] = useState<string[]>(original.current?.tool_refs ?? []);
  const [skills, setSkills] = useState<AgentExactSkillGrant[]>(original.current?.skill_grants ?? []);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<TeamPlugin | null>(null);
  const [query, setQuery] = useState("");
  // Groups open when the form opens if they hold a selection; afterwards people open and close them.
  const [openBaseline, setOpenBaseline] = useState<ReadonlySet<string>>(() => selectionKeys(original.current));
  const [groupOpen, setGroupOpen] = useState<Record<string, boolean>>({});
  const [templates, setTemplates] = useState<TeamPluginTemplate[] | null>(null);
  const [templateError, setTemplateError] = useState("");
  const [templateId, setTemplateId] = useState("");
  // The form as the last template (or Blank) left it; choosing another asks first only if people changed it since.
  const templateBaseline = useRef(JSON.stringify(BLANK_FORM));
  const locale = useLocale();
  const toolT = useTranslations("settings.subagents");
  const canSetCategory = useOptionalAuthSession()?.me?.capabilities?.team_tool_confirmations === true;

  const signature = JSON.stringify({ editing, id: selected?.id, name, description, tools, skills });
  const editVersion = useRef({ signature, value: 0 });
  if (editVersion.current.signature !== signature) editVersion.current = { signature, value: editVersion.current.value + 1 };
  const generation = editVersion.current.value;
  const dirty = editing && (name !== (selected?.name ?? "") || description !== (selected?.description ?? "")
    || JSON.stringify(tools) !== JSON.stringify(selected?.tool_refs ?? [])
    || JSON.stringify(skills) !== JSON.stringify(selected?.skill_grants ?? []));
  const live = useRef({ generation, dirty, busy, hosted, confirm, nav, selected, tObject });
  live.current = { generation, dirty, busy, hosted, confirm, nav, selected, tObject };
  const allowLeave = useCallback(async (action?: () => void) => {
    const attempt = ++intent.current;
    const state = live.current;
    if (!mounted.current || state.busy) return false;
    // Hosted in Canvas: the shared close-guard copy (C14).
    if (state.dirty && !await state.confirm(state.hosted
      ? canvasCloseGuardOptions(state.tObject, state.selected?.name ?? "")
      : { title: state.nav("discard_title"), description: state.nav("discard_description"),
        confirmLabel: state.nav("discard_action"), cancelLabel: state.nav("discard_keep") })) return false;
    if (!mounted.current || attempt !== intent.current || live.current.busy || live.current.generation !== state.generation) return false;
    approved.current = state.generation;
    action?.();
    return true;
  }, []);
  const afterLeave = (action: () => void) => {
    if (!mounted.current || live.current.busy) return;
    if (!live.current.dirty) { intent.current += 1; action(); }
    else void allowLeave(action);
  };
  useEffect(() => {
    mounted.current = true;
    const hasUnsaved = () => live.current.busy || live.current.dirty && approved.current !== live.current.generation;
    const port: CoachEditorPort = { requestClose: (afterClose) => { void allowLeave(afterClose ?? (() => live.current.hosted?.onClose())); },
      capture: (): CoachEditorSnapshot | null => {
        const state = live.current; const plugin = state.selected;
        if (!mounted.current || !plugin) return null;
        return { target: { kind: "plugin", id: plugin.id, version: plugin.row_version, revision: plugin.revision, content_hash: plugin.content_hash },
          dirty: state.dirty, pending_write: state.busy };
      } };
    if (hosted?.controllerRef) hosted.controllerRef.current = port;
    const unregister = registerSubagentDraftNavigationGuard({ hasUnsaved,
      confirmLeave: async () => { if (!hasUnsaved()) return true; live.current.hosted?.onActivate(); return allowLeave(); },
      cancelLeave: () => { intent.current += 1; approved.current = null; },
    });
    const warn = (event: BeforeUnloadEvent) => { if (live.current.dirty || live.current.busy) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => { mounted.current = false; intent.current += 1; unregister(); window.removeEventListener("beforeunload", warn);
      if (hosted?.controllerRef.current === port) hosted.controllerRef.current = null; };
  }, [hosted?.controllerRef, allowLeave]);

  // The list and the authoring catalog load separately: the catalog can take several seconds
  // (9.8 s once on 116) and only the form needs it, so the list never waits for it.
  const loadCatalog = useCallback(async () => {
    if (hosted?.readOnly) return;
    const request = ++catalogRequest.current;
    setCatalogError("");
    try {
      const resources = await getAgentAuthoringCatalog();
      if (mounted.current && request === catalogRequest.current) setCatalog(resources);
    } catch (cause) {
      // Shown only while there is no catalog: a failed refresh keeps the one already loaded.
      if (mounted.current && request === catalogRequest.current) setCatalogError(withHttpFailureReason(t("resources_load_failed"), cause, { read: true }));
    }
  }, [hosted?.readOnly, t]);
  const loadList = useCallback(async () => {
    const request = ++listRequest.current;
    try {
      const plugins = hostedMode ? [original.current!] : await listTeamPlugins();
      if (!mounted.current || request !== listRequest.current) return;
      setItems(plugins);
      setLoaded(true);
      setError("");
    } catch (cause) {
      if (mounted.current && request === listRequest.current) {
        setError(withHttpFailureReason(t("load_failed"), cause, { read: true }));
      }
    }
  }, [hostedMode, t]);
  /** Refresh after a change: the list before continuing, the catalog in the background. */
  const reload = useCallback(async () => {
    void loadCatalog();
    await loadList();
  }, [loadCatalog, loadList]);

  useEffect(() => { void loadCatalog(); return () => { catalogRequest.current += 1; }; }, [loadCatalog]);
  useEffect(() => { void loadList(); return () => { listRequest.current += 1; }; }, [loadList]);

  const chooseNow = (item: TeamPlugin | null) => {
    approved.current = null;
    setEditing(true);
    setSelected(item);
    setName(item?.name ?? "");
    setDescription(item?.description ?? "");
    setTools(item?.tool_refs ?? []);
    setSkills(item?.skill_grants ?? []);
    setQuery("");
    setOpenBaseline(selectionKeys(item ?? undefined));
    setGroupOpen({});
    setTemplateId("");
    templateBaseline.current = JSON.stringify(BLANK_FORM);
    setError("");
  };

  const choose = (item: TeamPlugin | null) => afterLeave(() => chooseNow(item));
  const closeEditor = () => {
    setEditing(false);
    setSelected(null);
    setError("");
  };

  const creatingOnPage = !hostedMode && editing && selected === null;
  // One request at a time; Retry only clears the error and this effect loads again.
  const templateLoading = useRef(false);
  useEffect(() => {
    if (!creatingOnPage || templates !== null || templateError || templateLoading.current) return;
    templateLoading.current = true;
    void listTeamPluginTemplates()
      .then((list) => { if (mounted.current) setTemplates(list.filter((item) => TEMPLATE_KINDS.has(item.kind))); })
      .catch((cause: unknown) => { if (mounted.current) setTemplateError(withHttpFailureReason(t("template_load_failed"), cause, { read: true })); })
      .finally(() => { templateLoading.current = false; });
  }, [creatingOnPage, templates, templateError, t]);
  const applyTemplate = async (id: string) => {
    const template = templates?.find((item) => item.id === id);
    const next: FormContents = template ? {
      name: t(`templates.${template.kind}.name`, { connection: template.connection_name }),
      description: t(`templates.${template.kind}.description`),
      tools: template.tool_refs,
      skills: template.skill_grants,
    } : BLANK_FORM;
    const edited = JSON.stringify({ name, description, tools, skills }) !== templateBaseline.current;
    if (edited && !await confirm({ title: t("template_replace_title"), description: t("template_replace_description"), confirmLabel: t("template_replace_confirm") })) return;
    if (!mounted.current) return;
    setName(next.name);
    setDescription(next.description);
    setTools(next.tools);
    setSkills(next.skills);
    setOpenBaseline(new Set([...next.tools, ...next.skills.map((grant) => grant.skill_ref.id)]));
    setGroupOpen({});
    templateBaseline.current = JSON.stringify(next);
    setTemplateId(id);
  };

  const save = async () => {
    if (!catalog || live.current.busy || live.current.hosted?.readOnly) return;
    live.current.busy = true;
    setBusy(true);
    setError("");
    setSaveFailed(false);
    try {
      const input = {
        name: name.trim(),
        description: description.trim(),
        tool_refs: tools,
        skill_grants: skills,
      };
      const saved = selected ? await updateTeamPlugin(selected.id, selected.row_version, input) : await createTeamPlugin(input);
      if (!mounted.current) return;
      if (live.current.hosted) { chooseNow(saved); live.current.hosted.onSaved(saved); }
      else { await reload(); if (mounted.current) closeEditor(); }
    } catch (cause) {
      setError(codedErrorMessage(cause, t("save_failed"), PLUGIN_ERROR_CODES));
      setSaveFailed(true);
    } finally {
      live.current.busy = false;
      if (mounted.current) setBusy(false);
    }
  };

  const changeStatus = async (item: TeamPlugin) => {
    if (live.current.hosted?.readOnly || !await allowLeave()) return;
    if (live.current.hosted?.readOnly) return;
    live.current.busy = true;
    setBusy(true);
    setError("");
    setSaveFailed(false);
    try {
      const saved = await setTeamPluginEnabled(item.id, item.row_version, item.status !== "active");
      if (!mounted.current) return;
      if (live.current.hosted) { chooseNow(saved); live.current.hosted.onSaved(saved); }
      else { await reload(); if (mounted.current && selected?.id === item.id) closeEditor(); }
    } catch (cause) {
      setError(codedErrorMessage(cause, t("save_failed"), PLUGIN_ERROR_CODES));
    } finally {
      live.current.busy = false;
      if (mounted.current) setBusy(false);
    }
  };

  const remove = async () => {
    if (!deleteTarget || live.current.busy) return;
    live.current.busy = true;
    setBusy(true);
    setError("");
    setSaveFailed(false);
    let deleted = false;
    try {
      await deleteTeamPlugin(deleteTarget.id, deleteTarget.row_version);
      deleted = true;
      setDeleteTarget(null);
      if (!mounted.current) return;
      if (selected?.id === deleteTarget.id) closeEditor();
      await reload();
    } catch (cause) {
      setDeleteTarget(null);
      setError(deleted ? t("load_failed") : codedErrorMessage(cause, t("delete_failed"), PLUGIN_ERROR_CODES));
      if (!deleted) await reload().catch(() => undefined);
    } finally {
      live.current.busy = false;
      if (mounted.current) setBusy(false);
    }
  };

  const toggle = (values: string[], setValues: (value: string[]) => void, value: string) => {
    setValues(values.includes(value) ? values.filter((item) => item !== value) : [...values, value]);
  };
  const toggleSkill = (grant: AgentExactSkillGrant) => {
    if (live.current.busy || live.current.hosted?.readOnly) return;
    setSkills((current) => current.some((item) => item.skill_ref.id === grant.skill_ref.id)
      ? current.filter((item) => item.skill_ref.id !== grant.skill_ref.id) : [...current, grant]);
  };
  const changedSkills = skills.filter((grant) => {
    const current = catalog?.skills.find((skill) => skill.grant.skill_ref.id === grant.skill_ref.id);
    return current && current.grant.revision !== grant.revision;
  });
  const unavailableSkills = catalog ? skills.filter((grant) => !catalog.skills.some((skill) => skill.grant.skill_ref.id === grant.skill_ref.id)) : [];
  const unavailableTools = catalog ? tools.filter((ref) => !catalog.tools.some((tool) => tool.stable_ref === ref)) : [];
  const searching = query.trim().length > 0;
  const selectedTools = new Set(tools);
  const selectedSkills = new Set(skills.map((grant) => grant.skill_ref.id));
  const pickerPlugins = pluginsFromCatalog(catalog?.plugins, locale);
  const isOpen = (key: string, refs: readonly string[]) => groupOpen[key] ?? refs.some((ref) => openBaseline.has(ref));
  const setOpen = (key: string) => (open: boolean) => setGroupOpen((current) => (current[key] === open ? current : { ...current, [key]: open }));
  const countLabel = (refs: readonly string[], selectedRefs: ReadonlySet<string>) =>
    searching ? String(refs.length) : t("group_count", { selected: refs.filter((ref) => selectedRefs.has(ref)).length, total: refs.length });
  const toPickerTool = (tool: AgentAuthoringTool): PickerTool => {
    const copy = localizedToolText(tool, locale);
    const settable = canSetCategory && tool.source_kind === "team_mcp" && tool.locked_reason === "risk_metadata_unknown";
    return {
      ref: tool.stable_ref,
      name: copy.name,
      description: copy.description,
      selectable: tool.selectable,
      lockedLabel: tool.selectable ? null : toolT(toolSelectionLockedReasonKey(tool.locked_reason ?? "")),
      lockedAction: !tool.selectable && settable ? <CapabilityLink href={settingsTabHref("tools")}>{t("locked_set_category")}</CapabilityLink> : null,
    };
  };
  // Selectable first, then locked, each by name: what people can act on comes first.
  const byAvailability = (left: PickerTool, right: PickerTool) =>
    Number(!left.selectable) - Number(!right.selectable) || left.name.localeCompare(right.name, locale);
  const matches = (tool: AgentAuthoringTool) => authoringToolMatches(tool, query, locale);
  const connections = groupToolConnections(catalog?.tools ?? []).map((connection) => ({
    key: `connection:${connection.id}`,
    title: connection.name,
    refs: connection.tools.map((tool) => tool.stable_ref),
    items: connection.tools.filter(matches).map(toPickerTool).sort(byAvailability),
  }));
  const builtinTools = (catalog?.tools ?? []).filter((tool) => tool.source_kind === "native");
  const visibleBuiltinTools = builtinTools.filter(matches).map(toPickerTool).sort(byAvailability);
  const builtinGroupRefs = (key: ToolGroupKey) => {
    const groupOf = toolGroups(pickerPlugins);
    return builtinTools.filter((tool) => groupOf(tool.stable_ref) === key).map((tool) => tool.stable_ref);
  };
  const teamSkills = catalog?.skills.filter((skill) => skill.source_kind !== "user") ?? [];
  const skillItems: PickerTool[] = teamSkills
    .filter((skill) => authoringSkillMatches(skill, query, locale))
    .map((skill) => ({
      ref: skill.grant.skill_ref.id,
      name: authoringSkillTitle(skill, locale),
      description: authoringSkillDescription(skill, locale),
      selectable: true,
      riskLabel: skill.source_kind === "team" ? t("skill_source_team") : t("skill_source_builtin"),
    }));
  const skillRefs = teamSkills.map((skill) => skill.grant.skill_ref.id);
  const toggleSkillRef = (id: string) => {
    const grant = teamSkills.find((skill) => skill.grant.skill_ref.id === id)?.grant ?? skills.find((item) => item.skill_ref.id === id);
    if (grant) toggleSkill(grant);
  };
  const unavailableItems: PickerTool[] = [
    ...unavailableTools.map((ref) => ({ ref, name: readableToolRef(ref), description: "", selectable: false, lockedLabel: t("unavailable_reason") })),
    ...unavailableSkills.map((grant) => ({ ref: grant.skill_ref.id, name: grant.skill_ref.name, description: "", selectable: false, lockedLabel: t("unavailable_reason") })),
  ];
  const toggleUnavailable = (ref: string) => {
    if (tools.includes(ref)) toggle(tools, setTools, ref);
    else toggleSkillRef(ref);
  };
  const nothingMatches = searching && !connections.some((group) => group.items.length) && !visibleBuiltinTools.length && !skillItems.length;
  // Canvas hosting keeps its error at the top, next to its own save bar.
  const formError = saveFailed && editing && !hosted ? error : "";

  return <>
    {navigationDialog}
    <section className={styles.page}>
      {/* Hosted in Canvas the object header (C4) and toolbar (C2) replace the page header. */}
      {!hosted ? <header className={styles.header}>
        <div><h1>{t("title")}</h1><p>{t("intro")}</p></div>
        {!editing ? <Button variant="primary" disabled={busy} onClick={() => void choose(null)}>{t("create")}</Button> : null}
      </header> : selected && !hosted.readOnly ? <CanvasToolbarActions>
        <Button disabled={busy} onClick={() => void changeStatus(selected)}>{selected.status === "active" ? t("disable") : t("enable")}</Button>
      </CanvasToolbarActions> : null}
      {error && !formError ? <p className={styles.error} role="alert">{error}</p> : null}
      {error && !loaded ? <Button onClick={() => void loadList()}>{common("retry")}</Button> : null}
      <div className={`${styles.grid} ${hosted || !editing || items.length === 0 ? styles.singleColumn : ""}`}>
        {!hosted && (items.length > 0 || !editing) ? <div className={styles.list} aria-label={t("list_title")}>
          {items.length > 0 ? <h2 className={styles.listTitle}>{t("list_title")}</h2> : loaded ? <div className={styles.empty}>
            <h2>{t("empty_title")}</h2><p>{t("empty_description")}</p>
          </div> : error ? null : <p className={styles.loading}>{t("loading")}</p>}
          {items.map((item) => <article className={`${styles.card} ${editing && selected?.id === item.id ? styles.selectedCard : ""}`} key={item.id}>
            <div className={styles.cardHeader}>
              <button className={styles.selectButton} type="button" disabled={busy} onClick={() => void choose(item)} aria-current={editing && selected?.id === item.id ? "true" : undefined}>{item.name}</button>
              <span className={`${styles.status} ${item.status === "active" ? styles.active : styles.disabled}`}>{item.status === "active" ? t("active") : t("disabled")}</span>
            </div>
            {item.description ? <p className={styles.cardDescription}>{item.description}</p> : null}
            <div className={styles.cardFooter}>
              <small>{t("resource_count", { count: item.tool_refs.length + item.skill_grants.length })}</small>
              <div className={styles.cardActions}>
                <Button size="sm" disabled={busy} onClick={() => void changeStatus(item)}>{item.status === "active" ? t("disable") : t("enable")}</Button>
                <Button size="sm" variant="danger" disabled={busy} onClick={() => setDeleteTarget(item)}>{t("delete")}</Button>
              </div>
            </div>
          </article>)}
        </div> : null}
        {editing ? <form className={`${styles.form} ${hosted ? styles.hostedForm : ""}`} onSubmit={(event) => { event.preventDefault(); void save(); }}>
          {!hosted ? <h2>{selected ? t("edit") : t("create")}</h2> : null}
          <fieldset className={styles.formFields} disabled={busy || hosted?.readOnly}>
          {hosted?.readOnly ? <p>{t("read_only")}</p> : null}
          {creatingOnPage && templates?.length ? <label className={styles.field}>{t("template_label")}
            <select value={templateId} onChange={(event) => void applyTemplate(event.target.value)}>
              <option value="">{t("template_blank")}</option>
              {templates.map((item) => <option key={item.id} value={item.id}>{t(`templates.${item.kind}.option`, { connection: item.connection_name })}</option>)}
            </select>
          </label> : null}
          {creatingOnPage && templateError ? <p className={styles.noMatch} role="status">{templateError} <Button size="sm" onClick={() => setTemplateError("")}>{common("retry")}</Button></p> : null}
          <label className={styles.field}>{t("name")}<input required maxLength={80} value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label className={styles.field}>{t("description")}<textarea maxLength={1000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
          {hosted?.readOnly ? <fieldset className={styles.fieldset}><legend>{t("selected_resources")}</legend>
            <ReadOnlyResources tools={tools} skills={skills} />
          </fieldset> : <section className={styles.resourceSection} aria-labelledby="plugin-resources-title">
            <div className={styles.resourceHeading}>
              <h3 id="plugin-resources-title">{t("resources_title")}</h3>
              <span>{t("selected_summary", { tools: tools.length, skills: skills.length })}</span>
            </div>
            {!catalog ? (catalogError
              ? <p className={styles.noMatch} role="alert">{catalogError} <Button size="sm" onClick={() => void loadCatalog()}>{common("retry")}</Button></p>
              : <p className={styles.noMatch} role="status">{t("resources_loading")}</p>) : null}
            {catalog ? <input className={styles.search} type="search" value={query} aria-label={t("search_label")} placeholder={t("search_placeholder")} onChange={(event) => setQuery(event.target.value)} /> : null}
            {unavailableItems.length ? <ResourceGroup title={t("unavailable_title")} items={unavailableItems} selected={new Set([...selectedTools, ...selectedSkills])} onToggle={toggleUnavailable} inputName="plugin-unavailable" /> : null}
            {nothingMatches ? <p className={styles.noMatch} role="status">{t("no_match", { query: query.trim() })} {picker("no_match_body")}</p> : null}
            {catalog && !connections.length && !searching ? <p className={styles.emptyResources}>{t("no_added_tools")}</p> : null}
            {connections.filter((group) => !searching || group.items.length).map((group) => <ResourceGroup
              key={group.key}
              title={group.title}
              kind={t("group_connection")}
              count={countLabel(group.refs, selectedTools)}
              items={group.items}
              selected={selectedTools}
              onToggle={(ref) => toggle(tools, setTools, ref)}
              inputName="plugin-tool"
              collapsible={!searching}
              open={isOpen(group.key, group.refs)}
              onOpenChange={setOpen(group.key)}
            />)}
            {visibleBuiltinTools.length ? <ResourceToolPicker
              tools={visibleBuiltinTools}
              plugins={pickerPlugins}
              selected={selectedTools}
              selectedPluginIds={NO_PLUGINS}
              onToggle={(ref) => toggle(tools, setTools, ref)}
              kind={t("group_builtin")}
              countLabel={(items) => {
                const key = toolGroups(pickerPlugins)(items[0]?.ref ?? "");
                return countLabel(searching ? items.map((item) => item.ref) : builtinGroupRefs(key), selectedTools);
              }}
              collapsible={!searching}
              isOpen={(key) => isOpen(`builtin:${key}`, builtinGroupRefs(key))}
              onOpenChange={(key, open) => setOpen(`builtin:${key}`)(open)}
            /> : null}
            {skillItems.length ? <ResourceGroup
              title={picker("skills")}
              count={countLabel(searching ? skillItems.map((item) => item.ref) : skillRefs, selectedSkills)}
              items={skillItems}
              selected={selectedSkills}
              onToggle={toggleSkillRef}
              inputName="plugin-skill"
              collapsible={!searching}
              open={isOpen("skills", skillRefs)}
              onOpenChange={setOpen("skills")}
            /> : null}
            {changedSkills.length ? <p>{t("changed_skills", { names: changedSkills.map((grant) => grant.skill_ref.name).join(", ") })}</p> : null}
          </section>}
          </fieldset>
          {hosted ? (
            // Hosted in Canvas: the shared save bar (C9) replaces Cancel / Save; Discard goes through the close guard.
            <SaveBar
              dirty={dirty}
              saving={busy}
              failed={saveFailed}
              guard={false}
              saveDisabled={!catalog || hosted.readOnly || selected?.status === "disabled" || !name.trim() || tools.length + skills.length === 0}
              onSave={() => void save()}
              onDiscard={() => void allowLeave(() => chooseNow(live.current.selected))}
            />
          ) : (
            <>
            {/* A failed save is reported beside Save: the form can be taller than the window. */}
            {formError ? <p className={styles.error} role="alert">{formError}</p> : null}
            <div className={styles.actions}>
              <Button disabled={busy} onClick={() => afterLeave(() => closeEditor())}>{t("cancel")}</Button>
              <Button variant="primary" type="submit" disabled={!catalog || busy || selected?.status === "disabled" || !name.trim() || tools.length + skills.length === 0}>{t("save")}</Button>
            </div>
            </>
          )}
        </form> : null}
      </div>
      {deleteTarget ? <ConfirmDialog
        title={t("delete_title", { name: deleteTarget.name })}
        description={t("delete_description", { count: deleteTarget.agent_count })}
        confirmLabel={t("delete")}
        busy={busy}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => void remove()}
      /> : null}
    </section>
  </>;
}
