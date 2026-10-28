"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { AgentReleaseManager } from "@/components/agents/agent-release-manager";
import { TeamPluginsContent } from "@/components/settings/team-plugins-content";
import { SkillDrawer } from "@/components/skill-drawer";
import { ApiError } from "@/lib/api-client";
import { useAppRouter } from "@/lib/use-app-router";
import { Bot, Check, ExternalLink, Lock, Plug, RefreshCw, RotateCcw, Sparkles } from "@/components/icons";
import { useCoachAfterEdit } from "@/lib/agent-coach-after-edit";
import { Notice } from "@/components/ui/notice";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import type { RowAction } from "@/components/ui/row-actions-menu";
import {
  CanvasObjectHeader,
  CanvasObjectSurface,
  CanvasObjectToolbar,
  ObjectStatusBadge,
  StateBlock,
  type CanvasSaveReport,
} from "@/components/canvas/object";
import { readCoachResourceEditor, type CoachResourceEditor as Projection, type CoachResourceParent } from "@/lib/coach-resource-editors";
import type { CoachResourceObject } from "@/lib/use-coach-resource-editors";
import "@/styles/skills-settings.css";

/**
 * Hosted Agent / Skill / Plugin editors (decision D10): the original editors drop their own headers,
 * titles, close and cancel; the object surface supplies C2 toolbar, C4 header and the shared save bar
 * (C9). Versions, evaluation and publishing stay in the management page (“⋯ › Open in Management”).
 */
export function CoachResourceEditor({ object, active, onClose, onActivate, onTitle, onReturn, onReload, parentRetained = false, parentLabel, onSaveStateChange }: {
  object: CoachResourceObject; active: boolean; onClose: () => void; onActivate: () => void;
  onReload?: () => void;
  parentRetained?: boolean;
  /** Name of the object this editor was opened from (toolbar back button). */
  parentLabel?: string | null;
  onTitle: (title: string) => void; onReturn: (parent: CoachResourceParent) => void;
  onSaveStateChange?: (state: CanvasSaveReport) => void;
}) {
  const t = useTranslations("coach_resource_editor");
  const tObject = useTranslations("canvas_object");
  const router = useAppRouter();
  const titleId = useId();
  const [projection, setProjection] = useState<Projection | null>(null);
  const currentAccess = useRef<Projection | null>(null);
  const [failed, setFailed] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const title = useRef(onTitle);
  title.current = onTitle;
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setFailed(false);
    void readCoachResourceEditor(object.pointer, controller.signal).then((value) => {
      if (!controller.signal.aborted) { currentAccess.current = value; setProjection(value); title.current(value.title); }
    }).catch((error) => {
      if (!controller.signal.aborted) {
        if (error instanceof ApiError && [401, 403, 404].includes(error.status)) { currentAccess.current = null; setProjection(null); title.current(""); }
        setFailed(true);
      }
    });
    return () => controller.abort();
  }, [object.pointer, active, refresh]);

  // The toolbar mirrors the shared save bar (C2): unsaved, saving or a failed save.
  const [saveReport, setSaveReport] = useState<CanvasSaveReport | null>(null);
  const reportSave = useCallback((state: CanvasSaveReport) => { setSaveReport(state); onSaveStateChange?.(state); }, [onSaveStateChange]);
  const saveState = saveReport?.failed ? "failed" as const : saveReport?.saving ? "saving" as const : saveReport?.dirty ? "dirty" as const : undefined;
  const parent = object.pointer.parent;
  const back = parent?.kind === "agent_draft" || parentRetained
    ? { label: parentLabel || t("return_parent"), onClick: () => onReturn(parent!) }
    : parent?.kind === "agent_working_copy"
      ? { label: parentLabel || t("return_parent"), onClick: () => router.push(`/agents/manage/?agent_id=${encodeURIComponent(parent.agent_id)}`) }
      : undefined;
  const kind = projection?.editor_kind ?? (object.pointer.reference.kind === "capability" ? "plugin" : object.pointer.reference.kind === "agent" ? "agent" : "skill");
  const afterEdit = useCoachAfterEdit();
  // "After Coach edits" (C5): the same per-account setting as Profile, one tap away while Coach works.
  const afterEditActions: RowAction[] = projection?.editor_kind === "agent" && !projection.read_only && afterEdit.value
    ? (["check", "ask"] as const).map((value) => ({
      id: `after-edit-${value}`,
      label: t(`after_edit_${value}`),
      icon: afterEdit.value === value ? <Check size={14} aria-hidden /> : <span className="am-menu-blank" aria-hidden />,
      disabled: afterEdit.saving,
      onSelect: () => { if (afterEdit.value !== value) void afterEdit.change(value); },
    }))
    : [];
  const overflow: RowAction[] = [
    { id: "refresh", label: tObject("refresh"), icon: <RefreshCw size={14} aria-hidden />, onSelect: () => setRefresh((value) => value + 1) },
    ...(projection?.editor_kind === "agent" ? [{ id: "manager", label: tObject("open_manager"), icon: <ExternalLink size={14} aria-hidden />,
      onSelect: () => router.push(`/agents/manage/created/?agent_id=${encodeURIComponent(projection.reference.id)}`) }] : []),
    ...(onReload && projection && !projection.read_only ? [{ id: "reload", label: t("reload_saved"), icon: <RotateCcw size={14} aria-hidden />,
      onSelect: () => (object.editorRef.current ? object.editorRef.current.requestClose(onReload) : onReload()) }] : []),
    ...afterEditActions,
  ];
  const status = projection?.read_only ? <ObjectStatusBadge status="read_only" />
    : projection?.editor_kind === "plugin" ? <ObjectStatusBadge status={projection.target.plugin.status === "active" ? "enabled" : "disabled"} /> : null;
  // C4 sub-line: scope · version · verification for a Skill, scope · counts for a Plugin.
  const subtitle = !projection ? undefined
    : projection.editor_kind === "skill" ? [
      t(projection.target.kind === "team" ? "subtitle_skill_team" : "subtitle_skill_personal"),
      projection.target.kind === "skill" && projection.target.skill.version ? t("subtitle_version", { version: projection.target.skill.version }) : null,
      projection.target.kind === "skill" && projection.target.skill.import_status === "cleared" ? t("subtitle_verified") : null,
    ].filter(Boolean).join(" · ")
      : projection.editor_kind === "plugin" ? t("subtitle_plugin", {
        tools: projection.target.plugin.tool_refs.length, skills: projection.target.plugin.skill_grants.length,
      }) : undefined;
  const icon = kind === "agent" ? <Bot size={14} aria-hidden /> : kind === "plugin" ? <Plug size={14} aria-hidden /> : <Sparkles size={14} aria-hidden />;
  return (
    <CanvasObjectSurface
      labelledBy={projection ? titleId : undefined}
      label={projection ? undefined : t("title")}
      onSaveStateChange={reportSave}
      toolbar={(slot) => <CanvasObjectToolbar back={back} status={status} save={saveState} overflow={overflow} actionSlotRef={slot} />}
    >
      {projection ? <>
        <CanvasObjectHeader titleId={titleId} icon={icon} eyebrow={t(`eyebrow_${kind}`)} title={projection.title || t("title")} subtitle={subtitle} />
        {/* A failed revalidation keeps the original editor and its input (only access loss clears it). */}
        {failed ? <Notice tone="error">{t("unavailable")}</Notice> : null}
        {afterEdit.failed ? <Notice tone="error">{t("after_edit_failed")}</Notice> : null}
        {projection.editor_kind === "agent" ? <AgentReleaseManager agentId={projection.target.agent_id} hosted={{
          readOnly: projection.read_only, controllerRef: object.editorRef, onActivate, onClose,
          onSaved: (value) => { if (currentAccess.current?.editor_kind === "agent") title.current(value); },
        }} /> : projection.editor_kind === "skill" ? <SkillDrawer hosted chatReference={projection.reference} readOnly={projection.read_only} target={projection.target} meId={object.pointer.userId}
          controllerRef={object.editorRef} onActivate={onActivate} onClose={onClose} /> : <TeamPluginsContent hosted={{
            plugin: projection.target.plugin, readOnly: projection.read_only, controllerRef: object.editorRef, onActivate, onClose,
            onSaved: (plugin) => { if (currentAccess.current?.editor_kind !== "plugin" || currentAccess.current.reference.id !== plugin.id) return;
              setProjection((current) => current?.editor_kind === "plugin"
              ? { ...current, reference: { ...current.reference, revision: String(plugin.revision) }, target: { kind: "plugin", plugin }, title: plugin.name } : current);
              title.current(plugin.name); },
          }} /> }
        {projection.editor_kind === "skill" ? <Notice tone="muted">{t("binding_separate")}</Notice> : null}
      </> : failed ? (
        <StateBlock icon={<Lock size={20} />} title={t(`unavailable_title_${kind}`)} body={t("unavailable")}
          secondaryActions={[{ label: tObject("refresh"), onClick: () => setRefresh((value) => value + 1) }]} />
      ) : <LoadingSkeleton variant="form" rows={3} label={t("loading")} />}
    </CanvasObjectSurface>
  );
}
