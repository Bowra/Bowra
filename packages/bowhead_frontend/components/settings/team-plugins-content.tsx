"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { codedErrorMessage } from "@/lib/user-facing-error";
import { useTranslations } from "next-intl";

import { ConfirmDialog, useConfirmDialog } from "@/components/ui/confirm-dialog";
import { Button } from "@/components/ui/button";
import { SaveBar } from "@/components/ui/save-bar";
import { CanvasToolbarActions, canvasCloseGuardOptions } from "@/components/canvas/object";
import { getAgentAuthoringCatalog, type AgentAuthoringCatalog, type AgentAuthoringTool, type AgentExactSkillGrant } from "@/lib/agents-api";
import {
  createTeamPlugin,
  deleteTeamPlugin,
  listTeamPlugins,
  setTeamPluginEnabled,
  updateTeamPlugin,
  type TeamPlugin,
} from "@/lib/team-plugins-api";
import { localizedToolText, toolDisplay } from "@/lib/tool-display";
import { authoringSkillDescription, authoringSkillTitle } from "@/lib/skills-meta";
import type { CoachEditorPort, CoachEditorSnapshot } from "@/lib/coach-canvas-target";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import styles from "@/app/(app)/team/settings/capabilities/page.module.css";

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

function ToolOption({ tool, selected, onToggle }: { tool: AgentAuthoringTool; selected: boolean; onToggle: () => void }) {
  return <label className={styles.resource}>
    <input type="checkbox" checked={selected} disabled={!tool.selectable && !selected} onChange={onToggle} />
    <span><strong>{localizedToolText(tool).name}</strong><small>{localizedToolText(tool).description}</small>
      {tool.source_display_name ? <small className={styles.resourceSource}>{tool.source_display_name}</small> : null}
    </span>
  </label>;
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
  const common = useTranslations("common");
  const tObject = useTranslations("canvas_object");
  const [saveFailed, setSaveFailed] = useState(false);
  const catalogRequest = useRef(0);
  const [items, setItems] = useState<TeamPlugin[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [catalog, setCatalog] = useState<AgentAuthoringCatalog | null>(null);
  const [selected, setSelected] = useState<TeamPlugin | null>(original.current ?? null);
  const [editing, setEditing] = useState(hostedMode);
  const [name, setName] = useState(original.current?.name ?? "");
  const [description, setDescription] = useState(original.current?.description ?? "");
  const [tools, setTools] = useState<string[]>(original.current?.tool_refs ?? []);
  const [skills, setSkills] = useState<AgentExactSkillGrant[]>(original.current?.skill_grants ?? []);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<TeamPlugin | null>(null);

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

  const reload = useCallback(async () => {
    const request = ++catalogRequest.current;
    try {
      const [plugins, resources] = await Promise.all([hostedMode ? Promise.resolve([original.current!]) : listTeamPlugins(),
        hosted?.readOnly ? Promise.resolve(null) : getAgentAuthoringCatalog()]);
      if (!mounted.current || request !== catalogRequest.current) return;
      setItems(plugins);
      setCatalog(resources);
      setLoaded(true);
      setError("");
    } catch {
      if (mounted.current && request === catalogRequest.current) setError(t("load_failed"));
    }
  }, [hostedMode, hosted?.readOnly, t]);

  useEffect(() => { void reload(); return () => { catalogRequest.current += 1; }; }, [reload]);

  const chooseNow = (item: TeamPlugin | null) => {
    approved.current = null;
    setEditing(true);
    setSelected(item);
    setName(item?.name ?? "");
    setDescription(item?.description ?? "");
    setTools(item?.tool_refs ?? []);
    setSkills(item?.skill_grants ?? []);
    setError("");
  };

  const choose = (item: TeamPlugin | null) => afterLeave(() => chooseNow(item));
  const closeEditor = () => {
    setEditing(false);
    setSelected(null);
    setError("");
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
  const unavailableSkills = skills.filter((grant) => !catalog?.skills.some((skill) => skill.grant.skill_ref.id === grant.skill_ref.id));
  const addedTools = catalog?.tools.filter((tool) => tool.source_kind === "team_mcp") ?? [];
  const systemTools = catalog?.tools.filter((tool) => tool.source_kind === "native") ?? [];
  const unavailableTools = catalog ? tools.filter((ref) => !catalog.tools.some((tool) => tool.stable_ref === ref)) : [];

  return <>
    {navigationDialog}
    <section className={styles.page}>
      {/* Hosted in Canvas the object header (C4) and toolbar (C2) replace the page header. */}
      {!hosted ? <header className={styles.header}>
        <div><h1>{t("title")}</h1><p>{t("intro")}</p></div>
        <Button variant="primary" disabled={busy} onClick={() => void choose(null)}>{t("create")}</Button>
      </header> : selected && !hosted.readOnly ? <CanvasToolbarActions>
        <Button disabled={busy} onClick={() => void changeStatus(selected)}>{selected.status === "active" ? t("disable") : t("enable")}</Button>
      </CanvasToolbarActions> : null}
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      {error && !catalog && !hosted?.readOnly ? <Button onClick={() => void reload()}>{common("retry")}</Button> : null}
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
          <label className={styles.field}>{t("name")}<input required maxLength={80} value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label className={styles.field}>{t("description")}<textarea maxLength={1000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
          {hosted?.readOnly ? <fieldset className={styles.fieldset}><legend>{t("selected_resources")}</legend>
            <ul>{tools.map((ref) => <li key={ref}>{readableToolRef(ref)}</li>)}{skills.map((grant) => <li key={grant.skill_ref.id}>{grant.skill_ref.name}</li>)}</ul>
          </fieldset> : <>
          <fieldset className={styles.fieldset}><legend>{t("added_tools")}</legend>
            <div className={styles.resources}>{addedTools.length ? addedTools.map((tool) => <ToolOption key={tool.stable_ref} tool={tool} selected={tools.includes(tool.stable_ref)} onToggle={() => toggle(tools, setTools, tool.stable_ref)} />) : <p className={styles.emptyResources}>{t("no_added_tools")}</p>}</div>
          </fieldset>
          <fieldset className={styles.fieldset}><legend>{t("system_tools")}</legend>
            <div className={styles.resources}>{systemTools.map((tool) => <ToolOption key={tool.stable_ref} tool={tool} selected={tools.includes(tool.stable_ref)} onToggle={() => toggle(tools, setTools, tool.stable_ref)} />)}</div>
          </fieldset>
          {unavailableTools.length ? <fieldset className={styles.fieldset}><legend>{t("unavailable_resource")}</legend>
            <div className={styles.resources}>{unavailableTools.map((ref) => <label className={styles.resource} key={ref}>
              <input type="checkbox" checked onChange={() => toggle(tools, setTools, ref)} />
              <span><strong>{t("unavailable_resource")}</strong></span>
            </label>)}</div>
          </fieldset>
          : null}
          <fieldset className={styles.fieldset}><legend>{t("skills")}</legend>
            <div className={styles.resources}>{catalog?.skills.filter((skill) => skill.source_kind !== "user").map((skill) => <label className={styles.resource} key={skill.grant.skill_ref.id}>
              <input type="checkbox" checked={skills.some((grant) => grant.skill_ref.id === skill.grant.skill_ref.id)} onChange={() => toggleSkill(skill.grant)} />
              <span><strong>{authoringSkillTitle(skill)}</strong><small>{authoringSkillDescription(skill)}</small></span>
            </label>)}</div>
          </fieldset>
          {changedSkills.length ? <p>{t("changed_skills", { names: changedSkills.map((grant) => grant.skill_ref.name).join(", ") })}</p> : null}
          {unavailableSkills.length ? <fieldset className={styles.fieldset}><legend>{t("unavailable_resource")}</legend>
            {unavailableSkills.map((grant) => <label className={styles.resource} key={grant.skill_ref.id}>
              <input type="checkbox" checked onChange={() => toggleSkill(grant)} /><span>{grant.skill_ref.name}</span>
            </label>)}
          </fieldset> : null}
          </>}
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
            <div className={styles.actions}>
              <Button disabled={busy} onClick={() => afterLeave(() => closeEditor())}>{t("cancel")}</Button>
              <Button variant="primary" type="submit" disabled={!catalog || busy || selected?.status === "disabled" || !name.trim() || tools.length + skills.length === 0}>{t("save")}</Button>
            </div>
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
