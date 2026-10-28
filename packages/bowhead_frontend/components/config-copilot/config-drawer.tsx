"use client";

/** ConfigDrawer — chat-side configuration draft drawer (batch 1-b, tech-design-batch1b §4.3).
 *
 * Three forms (design screens 09/11/13):
 * - overlay: desktop with canvas; absolute inside canvas column, 664px covers canvas leaving 16px edge,
 *   scrim covers only canvas, **never chat**; bottom says “关闭后回到报告”.
 * - column: desktop without canvas; workspace right column (host grid), chat column narrows in document flow.
 * - sheet: <1200 / mobile / overlay available width <520px; full-screen slide-in from right, top-left “返回对话”.
 * overlay/column nonmodal (no focus trap/aria-modal): chat is second editing channel, keyboard must reach composer.
 */

import { ArrowLeft, Sparkles, X } from "@/components/icons";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { RoutineDrawer } from "@/components/settings/scheduled";
import { CreateSubagentCard } from "@/components/canvas/sections/create-subagent-card";
import type { ConfigDraftResult } from "@/lib/config-drafts-api";
import {
  consumeDraft,
  discardDraft,
  draftVersion,
  fetchDraft,
  getDraftPayload,
  markDraftDismissed,
  subscribeDraft,
  type ConfigDraftDto,
} from "@/lib/config-copilot-draft";
import type { ScheduledJobDTO, WorkspaceDTO } from "@/lib/api";
import { getSubagentManager, type TeamSubagentOwnerManagerDetail } from "@/lib/team-subagents-api";
import {
  getAgentAuthoringCatalog,
  getAgentWorkingCopy,
  type AgentAuthoringCatalog,
  type AgentWorkingCopyDetail,
} from "@/lib/agents-api";
import { readSpaces } from "@/lib/use-space";

export type ConfigDrawerLayout = "overlay" | "column" | "sheet";

/** GET DTO / SSE payload → AiFillPanel ConfigDraftResult shape (applyAiDraft input). */
function toDraftResult(source: {
  form_id: string;
  fields: Record<string, unknown>;
  field_meta: ConfigDraftDto["field_meta"];
  notes: Record<string, string>;
}): ConfigDraftResult {
  const issues = Object.entries(source.field_meta)
    .filter(([, meta]) => meta.needs_confirm && meta.note)
    .map(([field, meta]) => ({ field, code: "needs_confirm", message: String(meta.note) }));
  return {
    status: "ok",
    fields: source.fields,
    field_meta: source.field_meta,
    issues,
    clarification: null,
    secret_redacted: false,
    form_id: source.form_id,
  };
}

export function ConfigDrawer({
  draftId,
  layout,
  onClose,
}: {
  draftId: string;
  layout: ConfigDrawerLayout;
  onClose: () => void;
}) {
  const t = useTranslations("settings.config_copilot_drawer");
  const tScheduled = useTranslations("settings.scheduled");
  // Subscribe draft store: chat deltas must rerender (R2 review P2-2: previously relied on incidental parent renders).
  useSyncExternalStore(subscribeDraft, draftVersion, draftVersion);
  const [dto, setDto] = useState<ConfigDraftDto | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceDTO[]>([]);
  const [loadError, setLoadError] = useState(false);
  const [manager, setManager] = useState<TeamSubagentOwnerManagerDetail | null>(null);
  const [revisioned, setRevisioned] = useState<{ detail: AgentWorkingCopyDetail; catalog: AgentAuthoringCatalog } | null>(null);
  const [updatedFlash, setUpdatedFlash] = useState(false);
  const applyRef = useRef<((draft: ConfigDraftResult) => unknown) | null>(null);
  const seenFieldsRef = useRef<string | null>(null);
  const [formReady, setFormReady] = useState(false);

  const close = useCallback(() => {
    markDraftDismissed(draftId);  // Never reopen from SSE reconnect replay within this session (review P3-1).
    onClose();
  }, [draftId, onClose]);

  const payload = getDraftPayload(draftId);
  const liveTargetId = payload?.target?.id;
  useEffect(() => {
    let alive = true;
    setDto(null);
    setManager(null);
    setRevisioned(null);
    setLoadError(false);
    void fetchDraft(draftId)
      .then((next) => { if (alive) setDto(next); })
      .catch(() => { if (alive) setLoadError(true); });
    void readSpaces()
      .then((result) => { if (alive) setWorkspaces(result.items); })
      .catch(() => { /* Unavailable space list never blocks manual input (failure-must-not-block rule). */ });
    return () => { alive = false; };
  }, [draftId, liveTargetId]);

  const activeDto = dto && (!liveTargetId || dto.target?.id === liveTargetId) ? dto : null;
  const targetId = activeDto?.form_id === "team_subagent" ? activeDto.target?.id : undefined;
  useEffect(() => {
    if (!targetId) return;
    let alive = true;
    setManager(null);
    setRevisioned(null);
    setLoadError(false);
    void getSubagentManager(targetId)
      .then(async (detail) => {
        if (!detail.actions.edit) throw new Error("subagent not editable");
        if (detail.definition_mode !== "revisioned") {
          if (alive) setManager(detail);
          return;
        }
        const [working, catalog] = await Promise.all([getAgentWorkingCopy(targetId), getAgentAuthoringCatalog()]);
        if (!working.actions.edit) throw new Error("working copy not editable");
        if (!alive) return;
        const source = working.working_copy ?? working.live;
        const definition = source.definition;
        const listing = source.listing;
        const refs = definition.tool_refs ?? definition.tool_refs_json;
        const grants = definition.skill_grants;
        const schema = definition.output_schema ?? definition.output_schema_json;
        const parsedRefs = typeof refs === "string" ? JSON.parse(refs) as unknown : refs;
        const parsedSchema = typeof schema === "string" ? JSON.parse(schema) as unknown : schema;
        const legacySkills = definition.skills ?? definition.skills_json;
        const parsedSkills = typeof legacySkills === "string" ? JSON.parse(legacySkills) as unknown : legacySkills;
        setManager({
          ...detail,
          display_name: typeof listing.display_name === "string" ? listing.display_name : detail.display_name,
          description: typeof listing.description === "string" ? listing.description : detail.description,
          system_prompt: typeof definition.system_prompt === "string" ? definition.system_prompt : detail.system_prompt,
          tool_refs: Array.isArray(parsedRefs) ? parsedRefs.filter((ref): ref is string => typeof ref === "string") : detail.tool_refs,
          max_iterations: typeof definition.max_iterations === "number" ? definition.max_iterations : detail.max_iterations,
          skills: Array.isArray(grants) ? grants.flatMap((grant) => {
            if (!grant || typeof grant !== "object") return [];
            const ref = (grant as { skill_ref?: { name?: unknown } }).skill_ref;
            return typeof ref?.name === "string" ? [ref.name] : [];
          }) : Array.isArray(parsedSkills) ? parsedSkills.filter((name): name is string => typeof name === "string") : detail.skills,
          output_schema: parsedSchema && typeof parsedSchema === "object" && !Array.isArray(parsedSchema) ? parsedSchema as Record<string, unknown> : null,
        });
        setRevisioned({ detail: working, catalog });
      })
      .catch(() => { if (alive) setLoadError(true); });
    return () => { alive = false; };
  }, [targetId]);

  // Shell handles Esc uniformly across three forms; embedded RoutineDrawer handler disabled.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  // Chat deltas: forward dispatcher-written payload to form (kept protection applies naturally).
  const draftSubscribe = useCallback(
    (apply: (draft: ConfigDraftResult) => unknown) => {
      applyRef.current = apply;
      setFormReady(true);
      return () => { applyRef.current = null; setFormReady(false); };
    },
    [],
  );
  // Record baseline stamp once authoritative GET arrives; initialDraft fills store payload present at mount;
  // **any** later fields count as deltas (R2 review P1-B: old early return before form readiness omitted stamp,
  // discarding real first chat update as initial frame).
  useEffect(() => {
    if (activeDto) seenFieldsRef.current = JSON.stringify([activeDto.target?.id, activeDto.fields]);
  }, [activeDto]);
  useEffect(() => {
    if (!payload || !formReady || !applyRef.current || seenFieldsRef.current === null) return;
    const stamp = JSON.stringify([payload.target?.id, payload.fields]);
    if (seenFieldsRef.current === stamp) return;
    seenFieldsRef.current = stamp;
    const receipt = applyRef.current(toDraftResult(payload)) as { filled?: number } | undefined;
    if (!receipt || (receipt.filled ?? 1) <= 0) return;  // Entirely kept-protected updates → no Updated from chat indicator.
    setUpdatedFlash(true);
    const timer = window.setTimeout(() => setUpdatedFlash(false), 2400);
    return () => window.clearTimeout(timer);
  }, [payload, formReady]);

  const initialDraft = useMemo(() => (activeDto ? toDraftResult(activeDto) : null), [activeDto]);

  const onSaved = useCallback(
    (saved: ScheduledJobDTO) => {
      void consumeDraft(draftId, {
        job_id: saved.id,
        name: saved.name,
        next_run_at: saved.next_run_at ?? null,
      })
        .catch(() => { /* Save already succeeded; failed receipt never rolls back (draft converges through 24h expiry). */ })
        .finally(() => { markDraftDismissed(draftId); onClose(); });
    },
    [draftId, onClose],
  );

  const onDiscard = useCallback(() => {
    discardDraft(draftId);
    close();
  }, [draftId, close]);

  const onSubagentSaved = useCallback((name: string, _toolCount: number, displayName?: string) => {
    if (!manager) return;
    const savedTargetId = manager.id;
    const targetStillCurrent = () => {
      const latestTargetId = getDraftPayload(draftId)?.target?.id;
      return !latestTargetId || latestTargetId === savedTargetId;
    };
    // Chat may have switched this draft to another agent while the old save was in flight.
    // The server rejects a mismatched consume; keep the new editor open as well.
    if (!targetStillCurrent()) return;
    void consumeDraft(draftId, {
      kind: "team_subagent", ref_id: savedTargetId, label: displayName || manager.display_name || name,
      ...(revisioned ? { stage: "working_copy" as const } : {}),
    })
      .catch(() => { /* The configuration save already succeeded; keep the existing draft expiry fallback. */ })
      .finally(() => {
        if (!targetStillCurrent()) return;
        markDraftDismissed(draftId);
        onClose();
      });
  }, [draftId, manager, revisioned, onClose]);

  const isSubagent = activeDto?.form_id === "team_subagent" || payload?.form_id === "team_subagent";
  const body = loadError ? (
    <div className="dr-load-err">{t(isSubagent ? "subagent_load_failed" : "load_failed")}</div>
  ) : initialDraft && activeDto?.form_id === "team_subagent" ? (
    manager ? (
      <CreateSubagentCard
        key={manager.id}
        embedded
        hideClose
        editing={manager}
        revisioned={revisioned}
        initialDraft={initialDraft}
        draftSubscribe={draftSubscribe}
        onClose={close}
        onSaved={onSubagentSaved}
      />
    ) : <div className="dr-load-err" aria-busy>{t("loading")}</div>
  ) : initialDraft ? (
    <RoutineDrawer
      mode="new"
      job={null}
      workspaces={workspaces}
      t={tScheduled as never}
      variant="embedded"
      initialDraft={initialDraft}
      initialWorkspaceId={dto?.workspace_id ?? null}
      draftSubscribe={draftSubscribe}
      footerLabels={{ discard: t("discard") }}
      onDiscard={onDiscard}
      onClose={close}
      onSaved={onSaved}
    />
  ) : (
    <div className="dr-load-err" aria-busy>{t("loading")}</div>
  );

  const isSheet = layout === "sheet";
  return (
    <>
      {layout === "overlay" ? <div className="cc-drawer-scrim" onClick={close} /> : null}
      <aside
        className={`ccp cc-drawer cc-drawer-${layout}`}
        data-testid="config-drawer"
        aria-label={t(isSubagent ? "subagent_title" : "title")}
        // overlay/column nonmodal: chat is second editing channel; keyboard must Tab back to composer.
        // sheet covers chat fullscreen → restore modal semantics (R2 review P2-4).
        {...(isSheet ? { role: "dialog" as const, "aria-modal": true } : {})}
      >
        <div className="dr-head">
          {isSheet ? (
            <button type="button" className="icon-btn" onClick={close} aria-label={t("back")}>
              <ArrowLeft size={18} />
            </button>
          ) : null}
          <div className="ti">
            <div className="e">{t(isSubagent ? "subagent_eyebrow" : "eyebrow")}</div>
            <div className="t">
              {t(isSubagent ? "subagent_title" : "title")}
              <span className="badge-src"><Sparkles size={10} />{t("from_chat")}</span>
            </div>
          </div>
          {isSheet ? null : (
            <button type="button" className="icon-btn" onClick={close} aria-label={t("close")}>
              <X size={18} />
            </button>
          )}
        </div>
        <div className="dr-hint">
          <span className="ic"><Sparkles size={13} /></span>
          {t("hint")}
          <span className="sp" />
          {updatedFlash ? <span className="badge-upd" data-testid="drawer-updated">{t("updated")}</span> : null}
        </div>
        <div className="dr-body">{body}</div>
        {isSubagent && activeDto?.status === "draft" ? (
          <button type="button" className="lnk" onClick={onDiscard} style={{ alignSelf: "flex-start", margin: "8px 18px 12px" }}>
            {t("discard")}
          </button>
        ) : null}
        {layout === "overlay" && !isSubagent ? (
          <div className="dr-back-hint"><ArrowLeft size={13} />{t("close_returns_report")}</div>
        ) : null}
      </aside>
    </>
  );
}
